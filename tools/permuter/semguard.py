"""semguard.py - flag permuter/search diffs that change what the code DOES, not just how it is written.

A "better scoring" candidate is sometimes wrong: a loop that tests one variable and increments another, `n + 1` -> `n + 0`,
`switch (st)` -> `switch (ok)`, `f(x, 0)` -> `(0, x)`.
 must stay equivalent to the original, so such diffs are hints for a person, never source to keep.

risks(diff_text) -> list of reasons (empty = looks like a pure rewrite). Checks:
  1. numeric constants: a number literal appears only on removed or only on added lines (sets, so chained
     assignments `a = b = 0` that drop repeats pass);
  2. variable swap: a changed line differs from its old version only by using another EXISTING identifier;
  3. argument swap: same tokens, different order, and the reordered part spans a comma (call arguments);
  4. local `enum X { ... }` declarations added inside code (type-pun hack).
usage: python semguard.py <diff file> [source file before the diff]   (prints the reasons)
"""
import re
import sys
from collections import Counter

TOK = re.compile(r'0[xX][0-9a-fA-F]+[uUlL]*|\d+\.\d*(?:[eE][-+]?\d+)?[fF]?|\d+[uUlL]*|[A-Za-z_]\w*|->|<<|>>|\S')
TYPES = {'s8', 'u8', 's16', 'u16', 's32', 'u32', 's64', 'u64', 'int', 'long', 'short', 'char', 'f32', 'f64', 'float',
         'double', 'unsigned', 'signed', 'bool', 'BOOL', 'void', 'const', 'volatile', 'register'}
NUM = re.compile(r'^(?:0[xX][0-9a-fA-F]+|\d)')


def toks(line):
    return TOK.findall(line)


def num_value(t):
    t = t.rstrip('uUlLfF') if not t.lower().startswith('0x') else t.rstrip('uUlL')
    try:
        return float(int(t, 0)) if not ('.' in t or 'e' in t.lower() and not t.lower().startswith('0x')) else float(t)
    except ValueError:
        try:
            return float(t)
        except ValueError:
            return t


def blocks(diff):
    """Consecutive (removed lines, added lines) blocks of a unified diff."""
    out, minus, plus = [], [], []
    for l in diff.split('\n'):
        if l.startswith('---') or l.startswith('+++'):
            continue
        if l.startswith('-'):
            if plus:
                out.append((minus, plus)); minus, plus = [], []
            minus.append(l[1:])
        elif l.startswith('+'):
            plus.append(l[1:])
        else:
            if minus or plus:
                out.append((minus, plus)); minus, plus = [], []
    if minus or plus:
        out.append((minus, plus))
    return out


def writes(line):
    """Plain variables and lvalue texts a statement modifies (`x++`, `x = ..`, `p->f += ..`)."""
    out = set()
    for m in re.finditer(r'([A-Za-z_][\w\.\->\[\]]*)\s*(?:\+\+|--)|(?:\+\+|--)\s*([A-Za-z_][\w\.\->\[\]]*)', line):
        out.add((m.group(1) or m.group(2)).replace(' ', ''))
    m = re.match(r'\s*(?:[A-Za-z_][\w:<>]*\s+\**)*?([A-Za-z_][\w\.\->\[\]\s]*?)\s*(?:[-+*/&|^%]|<<|>>)?=(?!=)', line)
    if m:
        out.add(m.group(1).replace(' ', ''))
    return out


def reads(line):
    rhs = line.split('=', 1)[1] if re.search(r'[^=!<>]=[^=]', line) else line
    return set(re.findall(r'[A-Za-z_]\w*', rhs)) | set(re.findall(r'[A-Za-z_]\w*(?=\s*(?:->|\.|\[))', line))


DECL = re.compile(r'^(\s*)(?!(?:return|else|case|goto|delete)\b)(?:const\s+)?(?:unsigned\s+|signed\s+)?'
                  r'[A-Za-z_][\w:]*(?:<[^>]*>)?\s*\**\s+\**\s*(?=[A-Za-z_]\w*\s*=[^=])')


def undecl(text):
    """`u32* hdr = (u32*)p;` -> `hdr = (u32*)p;`: a declaration split into `T x;` + a later `x = ...;` is the same
    statement moved ."""
    return DECL.sub(r'\1', text)


def moved_across(diff):
    """Lines moved inside a hunk past a statement that changes something the moved line uses (or that uses what
    the moved line changes): `fifoList = list;` moved before `list++;` publishes another entry."""
    reasons = []
    for hunk in re.split(r'\n(?=@@)', diff):
        old, new = [], []
        for l in hunk.split('\n'):
            if l.startswith(('---', '+++', '@@')) or not l:
                continue
            tag, text = l[0], undecl(l[1:])
            if tag in ' -':
                old.append((tag, text.strip()))
            if tag in ' +':
                new.append((tag, text.strip()))
        minus = [t for tag, t in old if tag == '-']
        plus = [t for tag, t in new if tag == '+']
        old_seq = [t for tag, t in old if t]
        new_seq = [t for tag, t in new if t]
        common = set(old_seq) & set(new_seq)
        for text in set(minus) & set(plus):
            if not text or text in ('{', '}', 'break;'):
                continue
            # statements that were on one side of the moved line before and are on the other side now
            # (a declaration turned into an assignment in place crosses nothing)
            b_old = set(old_seq[:old_seq.index(text)])
            b_new = set(new_seq[:new_seq.index(text)])
            crossed = [t for t in old_seq if t in (b_old ^ b_new) & common and t != text]
            w_m, r_m = writes(text), reads(text)
            for c in crossed:
                w_c, r_c = writes(c), reads(c)
                hit = {x for x in w_c if x.split('->')[0].split('.')[0].split('[')[0] in r_m and '->' not in x
                       and '.' not in x and '[' not in x} | (w_m & w_c) | \
                      {x for x in w_m if '->' not in x and '.' not in x and '[' not in x and x in r_c}
                if hit:
                    reasons.append('moves "%s" across "%s" (%s)' % (text, c, ', '.join(sorted(hit))))
                    break
    return reasons


def risks(diff, source='', light=False):
    """source = the file's text before the diff (identifiers that already exist there count as existing).
    light=True (for a search loop): only moves across a dependency and narrowing - tricks that legitimately
    change literals (`x < 5` -> `x <= 4`, typed pointer steps) or returns (early return) still run there."""
    if light:
        minus_all = [l[1:] for l in diff.split('\n') if l.startswith('-') and not l.startswith('---')]
        plus_all = [l[1:] for l in diff.split('\n') if l.startswith('+') and not l.startswith('+++')]
        return moved_across(diff) + narrowing(minus_all, plus_all, source)
    reasons = moved_across(diff)
    minus_all = [l[1:] for l in diff.split('\n') if l.startswith('-') and not l.startswith('---')]
    plus_all = [l[1:] for l in diff.split('\n') if l.startswith('+') and not l.startswith('+++')]
    nm = Counter(num_value(t) for l in minus_all for t in toks(l) if NUM.match(t))
    np_ = Counter(num_value(t) for l in plus_all for t in toks(l) if NUM.match(t))
    if set(nm) != set(np_):
        reasons.append('changes constants: %s -> %s' % (sorted(set(nm) - set(np_), key=str), sorted(set(np_) - set(nm), key=str)))
    old_idents = set(t for l in minus_all + source.splitlines() for t in toks(l) if re.match(r'[A-Za-z_]', t))
    moved = set(m.strip() for m in minus_all)
    for minus, plus in blocks(diff):
        if len(minus) != len(plus):
            continue
        for a, b in zip(minus, plus):
            ta, tb = toks(a), toks(b)
            if ta == tb or len(ta) != len(tb) or b.strip() in moved:
                continue
            diffpos = [i for i in range(len(ta)) if ta[i] != tb[i]]
            if Counter(ta) == Counter(tb):
                # same tokens, new order: `a != b` -> `b != a` and `x = y = 0` chains are fine; a reorder that spans
                # a comma swaps call arguments - except in a for-header's increment list (`i++, p++`)
                lo, hi = diffpos[0], diffpos[-1]
                semis = [i for i, t in enumerate(ta) if t == ';']
                in_for_incr = ta[:1] == ['for'] and len(semis) >= 2 and lo > semis[1]
                if ',' in ta[lo:hi + 1] and not in_for_incr:
                    reasons.append('reorders arguments: %s -> %s' % (a.strip(), b.strip()))
            elif all(re.match(r'[A-Za-z_]\w*$', ta[i]) and re.match(r'[A-Za-z_]\w*$', tb[i]) for i in diffpos):
                # cast / declaration type changes (s8 -> u8) are matching tweaks, not other variables
                changed = [(ta[i], tb[i]) for i in diffpos if not (ta[i] in TYPES and tb[i] in TYPES)]
                # only flag uses of identifiers that already existed (a NEW name = copy-variable trick, harmless)
                existed = [(x, y) for x, y in changed if y in old_idents]
                if existed and not re.match(r'\s*(?:[A-Za-z_][\w:<>]*\s+\**)+[A-Za-z_]\w*\s*(?:=|;)', b):
                    reasons.append('uses another variable: %s  (%s -> %s)' % (b.strip(), a.strip(), b.strip()))
    if any(re.search(r'\benum\s+\w+\s*\{', l) for l in plus_all) and \
            not any(re.search(r'\benum\s+\w+\s*\{', l) for l in minus_all):
        reasons.append('adds a local enum (type-pun hack)')
    reasons += control_flow(minus_all, plus_all)
    reasons += narrowing(minus_all, plus_all, source)
    return reasons


# kept diffs that broke code - `return;` inserted inside a loop by a brace reshuffle (loop ran once),
# `x = y;` -> `if (x < y) x = y;`, `(u16)layer` on a pointer, `u32 flags` -> `u8 flags` then `flags >> 9`.
def _count(lines, pat):
    return sum(len(re.findall(pat, re.sub(r'"(?:\\.|[^"\\])*"', '""', l))) for l in lines)


def control_flow(minus_all, plus_all):
    out = []
    for kw in ('return', 'continue', 'goto'):
        a, b = _count(minus_all, r'\b%s\b' % kw), _count(plus_all, r'\b%s\b' % kw)
        if a != b:
            out.append('control flow: %d -> %d `%s`' % (a, b, kw))
    # if <-> switch <-> ?: and for <-> while rewrites keep the number of decisions
    pat = r'\b(?:if|switch|while|for)\b|\?'
    a, b = _count(minus_all, pat), _count(plus_all, pat)
    if a != b:
        out.append('control flow: %d -> %d branches (if/switch/while/for/?:)' % (a, b))
    a, b = _count(minus_all, r'\bbreak\b'), _count(plus_all, r'\bbreak\b')
    sa, sb = _count(minus_all, r'\bswitch\b'), _count(plus_all, r'\bswitch\b')
    if a != b and sa == sb:
        out.append('control flow: %d -> %d `break`' % (a, b))
    return out


WIDTH = {'u8': 8, 's8': 8, 'char': 8, 'u16': 16, 's16': 16, 'short': 16, 'u32': 32, 's32': 32, 'int': 32,
         'long': 32, 'unsigned': 32}


def _param_width(line, pos, text):
    """Width of the parameter that the expression at `pos` is passed to, from a prototype in `text` (None = not a
    call argument or no prototype found)."""
    depth, i, arg = 0, pos - 1, 0
    while i >= 0:
        c = line[i]
        if c == ')':
            depth += 1
        elif c == '(':
            if depth == 0:
                break
            depth -= 1
        elif c == ',' and depth == 0:
            arg += 1
        i -= 1
    if i < 0:
        return None
    fm = re.search(r'([A-Za-z_]\w*)\s*$', line[:i])
    if not fm or fm.group(1) in ('if', 'while', 'for', 'switch', 'return', 'sizeof'):
        return None
    for pm in re.finditer(r'\b%s\s*\(([^()]*)\)\s*[;{]' % re.escape(fm.group(1)), text):
        params = [p.strip() for p in pm.group(1).split(',')]
        if len(params) > arg and not re.search(r'[A-Za-z_]\w*\s*\(', pm.group(1)):
            tm = re.match(r'(?:const\s+)?(?:unsigned\s+|signed\s+)?(\w+)\s*(\**)', params[arg])
            if tm and not tm.group(2) and tm.group(1) in WIDTH:
                return WIDTH[tm.group(1)]
    return None


def narrowing(minus_all, plus_all, source):
    out = []
    text = source + '\n' + '\n'.join(minus_all)
    ptrs = set(re.findall(r'\w[\w:<>]*\s*\*+\s*(?:const\s+)?([A-Za-z_]\w*)\s*[;=,)]', text)) | {'this'}
    for l in plus_all:
        for m in re.finditer(r'\(\s*(u8|s8|u16|s16|char|short)\s*\)\s*([A-Za-z_]\w*)\b(?!\s*(?:->|\.|\[|\())', l):
            if m.group(2) in ptrs and m.group(0) not in ' '.join(minus_all):
                out.append('narrowing cast of a pointer: %s' % l.strip())
    # a new narrowing cast of a WIDER variable: harmless as a call argument whose parameter is that narrow anyway
    # (`f((u8)banme)` with `f(.., u8 bit, ..)`), behaviour-changing elsewhere (`i < (u8)count`, `g((u8)token)` with
    # a u16 parameter)
    decl = {}
    for m in re.finditer(r'\b(?:unsigned\s+|signed\s+)?(u8|s8|u16|s16|u32|s32|int|long|short|char)\s+([A-Za-z_]\w*)\s*[;=,)\[]',
                         text):
        decl.setdefault(m.group(2), WIDTH[m.group(1)])
    minus_text = ' '.join(minus_all)
    for l in plus_all:
        for m in re.finditer(r'\(\s*(u8|s8|u16|s16)\s*\)\s*([A-Za-z_]\w*)\b(?!\s*(?:->|\.|\[|\())', l):
            name, w = m.group(2), WIDTH[m.group(1)]
            if m.group(0) in minus_text or decl.get(name, 0) <= w:
                continue
            pw = _param_width(l, m.start(), text)
            if pw is not None and pw <= w:
                continue
            out.append('narrowing cast of a wider variable: %s' % l.strip())
    # a variable re-declared narrower, while the code still uses bits beyond the new width
    old = {}
    for l in minus_all:
        m = re.match(r'\s*(?:unsigned\s+|signed\s+)?(\w+)\s+([A-Za-z_]\w*)\s*[;=]', l)
        if m and m.group(1) in WIDTH:
            old[m.group(2)] = WIDTH[m.group(1)]
    alltext = text + '\n' + '\n'.join(plus_all)
    for l in plus_all:
        m = re.match(r'\s*(?:unsigned\s+|signed\s+)?(\w+)\s+([A-Za-z_]\w*)\s*[;=]', l)
        if not m or m.group(1) not in WIDTH or m.group(2) not in old or WIDTH[m.group(1)] >= old[m.group(2)]:
            continue
        name, w = m.group(2), WIDTH[m.group(1)]
        shifts = [int(s, 0) for s in re.findall(r'\b%s\s*>>\s*(0x[0-9a-fA-F]+|\d+)' % name, alltext)]
        masks = [int(s, 0) for s in re.findall(r'\b%s\s*&\s*(0x[0-9a-fA-F]+|\d+)' % name, alltext)]
        if any(s >= w for s in shifts) or any(mk >> w for mk in masks):
            out.append('narrows %s to %d bits but uses higher bits: %s' % (name, w, l.strip()))
    return out


if __name__ == '__main__':
    if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help'):
        print(__doc__)
        sys.exit(0)
    src = open(sys.argv[2], encoding='utf-8', errors='replace').read() if len(sys.argv) > 2 else ''
    for r in risks(open(sys.argv[1], encoding='utf-8', errors='replace').read(), src):
        print(r)
