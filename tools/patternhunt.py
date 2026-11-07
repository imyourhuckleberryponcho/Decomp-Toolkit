"""patternhunt.py: find a machine-code PATTERN in reference decomps' matching code and show the source that made it.

Corpora: any folder of dtk-style asm (`.fn name, ... .endfn` blocks, one .s per unit) from a decomp project that
used the same compiler. Pass them with --corpus DIR (repeatable); 'this' = build/<VER>/asm of your own project
(only units marked MatchingFor in configure.py). Optionally give the matching source tree of a corpus as
DIR=SRCDIR so --src can print the C/C++ of each hit.
usage (from the project root):
  python patternhunt.py '<regex>' --corpus DIR[=SRCDIR] [--corpus this] [--max 20] [--src]
The regex runs over one function at a time, one instruction per line as "op args" (addresses stripped, labels
shown as `.L:` lines, relocations kept: `@N` for anonymous symbols). Use (?m) anchors; \\n between instructions.
--src prints the source of each hit (function located by name in the matching source file).
"""
from dtkproj import *   # REPO, VER, EXE, HERE; chdir to the project root
import glob, os, re, subprocess, sys

os.chdir(REPO)
a = sys.argv[1:]
CORPORA = {'this': S('build/@V/asm')}
SRCROOT = {'this': 'src'}


def opt_all(name):
    return [a[i + 1] for i, x in enumerate(a) if x == name]


pattern = next(x for x in a if not x.startswith('--') and a[a.index(x) - 1] not in ('--corpus', '--max'))
MAX = int(a[a.index('--max') + 1]) if '--max' in a else 20
corpora = []
for c in opt_all('--corpus') or ['this']:
    if '=' in c:
        c, s = c.split('=', 1)
        SRCROOT[c] = s
    corpora.append(c)
SHOWSRC = '--src' in a
rx = re.compile(pattern, re.M)

# for the "this" corpus only use linked (matching) units
linked = set()
if 'this' in corpora:
    cfg = open('configure.py', encoding='utf-8').read()
    linked = set(re.findall(r'Object\(MatchingFor\("[^"]+"\),\s*"([^"]+)\.(?:c|cpp)"', cfg))


def functions(path):
    t = open(path, encoding='utf-8', errors='replace').read()
    for m in re.finditer(r'^\.fn (\S+),.*?^\.endfn', t, re.M | re.S):
        lines = []
        for l in m.group(0).split('\n')[1:-1]:
            if re.match(r'^\.L_\w+:', l):
                lines.append('.L:')
                continue
            im = re.match(r'^/\* [0-9A-F]+ [0-9A-F]+  [0-9A-F ]+\*/\t(.*)$', l)
            if im:
                i = im.group(1).strip()
                i = re.sub(r'\.L_\w+', '.L', i)
                i = re.sub(r'"?@\d+"?', '@N', i)
                lines.append(i)
        yield m.group(1), '\n'.join(lines)


def demangled_base(name):
    # foo__3BarFv -> foo ; Bar::foo -> foo
    return re.split(r'__(?=\d|F|C)', name)[0]


def show_source(corpus, asmpath, fn):
    root = CORPORA[corpus] if corpus in CORPORA else corpus
    rel = os.path.relpath(asmpath, root)[:-2]  # strip .s
    srcroot = SRCROOT.get(corpus, os.path.join(os.path.dirname(root.rstrip('/')), '..', 'src'))
    cands = [os.path.join(srcroot, rel)] if corpus != 'this' else [os.path.join('src', rel + e) for e in ('.cpp', '.c')]
    base = demangled_base(fn)
    for c in cands:
        if os.path.exists(c):
            t = open(c, encoding='utf-8', errors='replace').read().split('\n')
            for i, l in enumerate(t):
                if re.search(r'\b%s\s*\(' % re.escape(base), l) and not l.rstrip().endswith(';'):
                    depth, out = 0, []
                    for l2 in t[i:i + 120]:
                        out.append(l2)
                        depth += l2.count('{') - l2.count('}')
                        if depth == 0 and '{' in ''.join(out):
                            break
                    return c, '\n'.join(out)
            return c, '(function not found by name)'
    return None, '(source file not found)'


hits = 0
for corpus in corpora:
    root = CORPORA.get(corpus, corpus)
    for path in sorted(glob.glob(os.path.join(root, '**', '*.s'), recursive=True)):
        if corpus == 'this':
            rel = os.path.relpath(path, root)[:-2].replace(os.sep, '/')
            if rel not in linked:
                continue
        for fn, body in functions(path):
            m = rx.search(body)
            if not m:
                continue
            hits += 1
            print('== [%s] %s  (%s)' % (corpus, fn, os.path.relpath(path, root)))
            s = body[:m.start()].count('\n')
            lines = body.split('\n')
            e = s + m.group(0).count('\n')
            for k in range(max(0, s - 3), min(len(lines), e + 4)):
                print('   %s %s' % ('>' if s <= k <= e else ' ', lines[k]))
            if SHOWSRC:
                c, src = show_source(corpus, path, fn)
                print('   --- source: %s' % c)
                print('\n'.join('   | ' + l for l in src.split('\n')[:60]))
            if hits >= MAX:
                print('(stopped at --max %d)' % MAX)
                sys.exit(0)
print('%d hits' % hits)
