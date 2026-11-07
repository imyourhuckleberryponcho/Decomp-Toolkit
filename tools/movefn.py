"""Move a function definition inside a source file (keeps its wrappers).

    python movefn.py <src file> <name> --before <other name> [--out f]
    python movefn.py <src file> --asm <unit asm .s> [--reverse] [--out f]   # put every definition in asm order
                                                                  # (--reverse: deferred units)

<name> is the qualified source name (Class::method) or a plain name.
A definition's span includes the wrappers autodraft/hand fixes put around it: a preceding
`extern "C++" {` / `extern "C" {` line (and its closing `}`), and `#pragma push` +
`#pragma dont_inline on` ... `#pragma pop`.
--asm mode moves only definitions whose function appears in the asm (by demangled name),
one at a time, so helpers without a symbol stay where they are.
"""
from dtkproj import *   # REPO, VER, EXE, HERE; chdir to the project root
import re
import subprocess
import sys
import os

HERE = os.path.dirname(os.path.abspath(__file__))
DTK = 'build/tools/dtk' + EXE


def replace_def(text, name, new):
    """Replace only the function itself (header + body), keeping any extern/pragma wrappers around it.
    Use this, not def_span, when swapping in a new body (def_span includes the wrappers: dropping an
    `extern "C" {` wrapper silently changes the linkage -> the function scores 0%)."""
    sp = def_span(text, name, bare=True)
    if not sp:
        return None
    if not new.endswith('\n'):
        new += '\n'
    return text[:sp[0]] + new + text[sp[1]:]


def def_span(text, name, bare=False):
    """(start, end) of the single definition of name, wrappers included (bare=True: without them);
    None if missing/ambiguous."""
    pat = re.compile(r'^[^\n;{}#(=]*?(?<![\w:])%s\s*\([^;{}]*\)\s*(?:const\s*)?\{' % re.escape(name), re.M)
    ms = [m for m in pat.finditer(text)]
    if len(ms) != 1:
        return None
    m = ms[0]
    j, depth = m.end() - 1, 0
    while j < len(text):
        if text[j] == '{':
            depth += 1
        elif text[j] == '}':
            depth -= 1
            if depth == 0:
                break
        j += 1
    if depth:
        return None
    s = m.start()
    e = text.find('\n', j)
    e = len(text) if e < 0 else e + 1
    if bare:
        return s, e
    before = text[:s]
    ext = False
    for pre in ('#pragma push\n#pragma dont_inline on\n', 'extern "C++" {\n', 'extern "C" {\n',
                '#pragma push\n#pragma dont_inline on\n'):
        if before.endswith(pre):
            if pre.startswith('extern'):
                # only when this extern block holds just this function
                rest = re.match(r'[ \t]*\}[ \t]*\n', text[e:])
                if not rest:
                    continue
                ext = True
                e += rest.end()
            s -= len(pre)
            before = text[:s]
    mp = re.match(r'(?:[ \t]*//[^\n]*\n)?#pragma pop\n', text[e:])
    if mp and text[s:].startswith('#pragma push'):
        e += mp.end()
    return s, e


def linkage_at(text, pos):
    """'C', 'C++' or None: the innermost extern "..." { } block around pos (comments/strings ignored roughly)."""
    stack = []
    i = 0
    for m in re.finditer(r'extern\s*"(C\+\+|C)"\s*\{|\{|\}', text[:pos]):
        t = m.group(0)
        if t == '}':
            if stack:
                stack.pop()
        elif t == '{':
            stack.append(None)
        else:
            stack.append(m.group(1))
    for x in reversed(stack):
        if x:
            return x
    return None


def place(text, at, blk, link):
    """insert blk at offset at, wrapped in extern "link" { } when the linkage there differs."""
    here = linkage_at(text, at)
    if (here or 'C++') != (link or 'C++'):
        body = blk if blk.endswith('\n') else blk + '\n'
        blk = 'extern "%s" {\n%s}\n' % (link or 'C++', body)
    return text[:at] + blk + text[at:]


def link_of(text, span):
    """linkage of a definition span (looked up just inside a leading extern wrapper, if any)"""
    blk = text[span[0]:span[1]]
    return linkage_at(text, span[0] + blk.index('{') + 1 if blk.startswith('extern') else span[0])


def move(text, name, before):
    sp = def_span(text, name)
    if not sp:
        raise SystemExit('cannot locate a single definition of %s' % name)
    blk = text[sp[0]:sp[1]]
    link = link_of(text, sp)
    text = text[:sp[0]] + text[sp[1]:]
    bp = def_span(text, before)
    if not bp:
        raise SystemExit('cannot locate a single definition of %s' % before)
    return place(text, bp[0], blk + '\n', link)


def demangle(sym):
    try:
        out = subprocess.run([os.path.abspath(DTK), 'demangle', sym], capture_output=True, text=True).stdout.strip()
    except OSError:
        return sym
    return out or sym


def qual(sym):
    d = demangle(sym) if '__' in sym else sym
    if '(' in d:
        d = d[:d.index('(')]
    return '::'.join(x for x in d.split('::') if not x.startswith('@unnamed@'))


def main():
    a = sys.argv[1:]
    out = None
    if '--out' in a:
        i = a.index('--out')
        out = a[i + 1]
        del a[i:i + 2]
    src = a[0]
    text = open(src, encoding='utf-8').read()
    if '--asm' in a:
        asm = a[a.index('--asm') + 1]
        syms = [m.group(1).strip('"') for m in re.finditer(r'^\.fn (\S+?),', open(asm, encoding='utf-8').read(), re.M)]
        names = []
        for s in syms:
            q = qual(s)
            if def_span(text, q) and q not in names:
                names.append(q)
        if '--reverse' in a:   # -inline deferred units: source order is the reverse of the asm order
            names.reverse()
        # insertion pass: place each one right after the previous one in asm order
        moved = 0
        for k in range(1, len(names)):
            p, n = def_span(text, names[k - 1]), def_span(text, names[k])
            # move unless n already follows p with no other located definition in between
            between = p and n and n[0] > p[0] and not any(
                sp and p[1] <= sp[0] < n[0] for sp in (def_span(text, x) for x in names if x not in (names[k - 1], names[k])))
            if p and n and not between:
                blk = text[n[0]:n[1]]
                link = link_of(text, n)
                text = text[:n[0]] + text[n[1]:]
                p = def_span(text, names[k - 1])
                text = place(text, p[1], '\n' + blk, link)
                moved += 1
        print('%d definitions located, %d moved' % (len(names), moved))
    else:
        name = a[1]
        before = a[a.index('--before') + 1]
        text = move(text, name, before)
    open(out or src, 'w', encoding='utf-8').write(text)


if __name__ == '__main__':
    main()
