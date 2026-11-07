"""fnptrscan.py - callback (function-pointer) parameter mismatches that break on x64.

On the GameCube a caller always sets the whole 32-bit register, so a callback declared
`void f(MyEnum)` (enum, 4 bytes) but called through `void (*)(u8, ...)` works.
On x64 only the low byte is set: the callee reads garbage (a real freeze in a PC port).

Checks:
  A  the same function declared in several places with a function-pointer parameter whose
     own parameter WIDTHS differ (notify(u8,..) vs notify(enum,..)).
  B  a function passed by name as an argument where the callee's prototype (in that file)
     expects a function pointer whose parameter widths differ from the passed function's
     definition/declaration (same file first, else any file).
  C  struct members / typedefs of function-pointer type assigned a function by name
     (`x->cb = func;` / `{ func, ...}` not handled) - only `.member = func` / `->member = func`.
Width classes: 1 2 4 8(s64) P(pointer) F(float) D(double) E(enum=4 on GC, but on x64 also 4 -> E==4).
Small widths (1/2) vs 4/E are the dangerous ones (garbage upper bits); P vs 4 is also reported.

usage: fnptrscan.py [--src DIR] [--all]   (--all: also report mismatches of names it cannot size)
"""
from dtkproj import *   # REPO, VER, EXE, HERE; chdir to the project root
import os, re, sys, collections

ROOT = REPO
args = sys.argv[1:]
SRC = os.path.join(ROOT, 'src')
if '--src' in args:
    SRC = args[args.index('--src') + 1]
SHOW_ALL = '--all' in args

W1 = {'u8', 's8', 'char', 'unsigned char', 'signed char', 'bool', 'BOOL8', 'uchar'}
W2 = {'u16', 's16', 'short', 'unsigned short', 'signed short', 'ushort', 'wchar_t'}
W4 = {'u32', 's32', 'int', 'unsigned int', 'unsigned', 'long', 'unsigned long', 'signed int',
      'BOOL', 'uint', 'size_t'}
W8 = {'u64', 's64', 'long long', 'unsigned long long'}
WF = {'f32', 'float'}
WD = {'f64', 'double'}

typedefs = {}       # name -> width class or ('FN', [classes])
enums = set()
files = {}


def strip(s):
    s = re.sub(r'/\*.*?\*/', ' ', s, flags=re.S)
    s = re.sub(r'//[^\n]*', ' ', s)
    s = re.sub(r'"(\\.|[^"\\])*"', '""', s)
    return s


for d, _, fs in os.walk(SRC):
    for f in fs:
        if f.endswith(('.c', '.cpp', '.h', '.hpp')):
            p = os.path.join(d, f)
            files[p] = strip(open(p, encoding='utf-8', errors='replace').read())
for d, _, fs in os.walk(os.path.join(ROOT, 'include')):
    for f in fs:
        if f.endswith(('.h', '.hpp')):
            p = os.path.join(d, f)
            files[p] = strip(open(p, encoding='utf-8', errors='replace').read())


def split_params(s):
    out, depth, cur = [], 0, ''
    for ch in s:
        if ch in '(<[':
            depth += 1
        elif ch in ')>]':
            depth -= 1
        if ch == ',' and depth == 0:
            out.append(cur.strip()); cur = ''
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out


FNPTR = re.compile(r'^(.*?)\(\s*\*\s*(\w*)\s*\)\s*\((.*)\)\s*$', re.S)


def width(p):
    """width class of one parameter declaration (type + optional name)."""
    p = p.strip()
    if p in ('', 'void', '...'):
        return None
    m = FNPTR.match(p)
    if m:
        return ('FN', sig(m.group(3)))
    p = re.sub(r'\[.*?\]', '*', p)
    if '*' in p or '&' in p:
        return 'P'
    p = re.sub(r'\b(const|volatile|register|struct|enum|union|class|signed(?= (char|short|int)))\b', ' ', p)
    toks = p.split()
    if len(toks) > 1 and toks[-1] not in ('int', 'char', 'short', 'long', 'unsigned'):
        toks = toks[:-1]       # drop the parameter name
    t = ' '.join(toks)
    for cls, ws in (('1', W1), ('2', W2), ('4', W4), ('8', W8), ('F', WF), ('D', WD)):
        if t in ws:
            return cls
    if t in enums:
        return 'E'
    if t in typedefs:
        return typedefs[t]
    return '?' + t


def sig(params):
    ps = split_params(params)
    out = [width(x) for x in ps]
    return [x for x in out if x is not None]


# pass 1: enums + typedefs (iterate twice for chained typedefs)
for _ in range(3):
    for p, s in files.items():
        for m in re.finditer(r'\benum\s+(\w+)\s*\{', s):
            enums.add(m.group(1))
        for m in re.finditer(r'typedef\s+enum\s*\w*\s*\{[^}]*\}\s*(\w+)\s*;', s):
            enums.add(m.group(1))
        for m in re.finditer(r'typedef\s+([^;{}()]+?)\s+(\w+)\s*;', s):
            w = width(m.group(1) + ' x')
            if w and not (isinstance(w, str) and w.startswith('?')):
                typedefs.setdefault(m.group(2), w)
        for m in re.finditer(r'typedef\s+[^;{}()]*?\(\s*\*\s*(\w+)\s*\)\s*\(([^;{}]*?)\)\s*;', s):
            typedefs[m.group(1)] = ('FN', sig(m.group(2)))


def small(a, b):
    """is this pair of width classes dangerous on x64?"""
    if a == b:
        return False
    norm = {'E': '4'}
    a2, b2 = norm.get(a, a), norm.get(b, b)
    if isinstance(a, tuple) or isinstance(b, tuple):
        return False
    if a2 == b2:
        return False
    if str(a2).startswith('?') or str(b2).startswith('?'):
        return SHOW_ALL
    return True


def fn_diff(s1, s2):
    if len(s1) != len(s2):
        return 'arg count %d vs %d' % (len(s1), len(s2))
    bad = [i for i, (a, b) in enumerate(zip(s1, s2)) if small(a, b)]
    if bad:
        return 'param %s: %s vs %s' % (','.join(str(i + 1) for i in bad),
                                       [s1[i] for i in bad], [s2[i] for i in bad])
    return None


# pass 2: every function declaration / definition
DECL = re.compile(r'(?<![\w.>])([A-Za-z_]\w*(?:::\w+)*)\s*\(((?:[^;{}()]|\([^;{}()]*\)|\([^;{}()]*\([^;{}()]*\)[^;{}()]*\))*)\)\s*(?:const\s*)?([;{])')
KEYW = {'if', 'while', 'for', 'switch', 'return', 'sizeof', 'case', 'do', 'else'}
decls = collections.defaultdict(list)    # name -> [(file, line, sig, is_def)]
for p, s in files.items():
    for m in DECL.finditer(s):
        name = m.group(1)
        if name in KEYW:
            continue
        pre = s[max(0, m.start() - 80):m.start()]
        line = pre.rsplit('\n', 1)[-1]
        # must look like a declaration: a type before the name on the same line
        if not re.search(r'[\w*&>]\s*$', line) or re.search(r'(=|return|\(|,)\s*[\w*&]*\s*$', line):
            continue
        params = m.group(2)
        if params.strip() and not re.search(r'[A-Za-z_]', params):
            continue
        ln = s.count('\n', 0, m.start()) + 1
        decls[name].append((p, ln, sig(params), m.group(3) == '{', params))

rel = lambda p: os.path.relpath(p, ROOT).replace('\\', '/')
hits = []

# check A
for name, ds in decls.items():
    fnparams = [(d, [(i, w) for i, w in enumerate(d[2]) if isinstance(w, tuple)]) for d in ds]
    fnparams = [x for x in fnparams if x[1]]
    if len(fnparams) < 2:
        continue
    base = None
    for d, fps in fnparams:
        if base is None:
            base = (d, dict(fps)); continue
        for i, w in fps:
            if i in base[1]:
                why = fn_diff(base[1][i][1], w[1])
                if why:
                    hits.append(('A', name, '%s:%d vs %s:%d' % (rel(base[0][0]), base[0][1], rel(d[0]), d[1]),
                                 'callback param %d: %s' % (i + 1, why)))

# check B: function names passed as arguments
CALL = re.compile(r'(?<![\w.>])([A-Za-z_]\w*)\s*\(([^;{}]*)\)\s*;')
for p, s in files.items():
    local = {}
    for m in DECL.finditer(s):
        if m.group(1) not in KEYW:
            local.setdefault(m.group(1), sig(m.group(2)))
    for m in CALL.finditer(s):
        callee = m.group(1)
        if callee in KEYW or callee not in local:
            continue
        cs = local[callee]
        for i, a in enumerate(split_params(m.group(2))):
            a = a.strip().lstrip('&')
            if not re.fullmatch(r'[A-Za-z_]\w*', a) or i >= len(cs) or not isinstance(cs[i], tuple):
                continue
            fsig = local.get(a)
            if fsig is None and decls.get(a):
                fsig = decls[a][0][2]
            if fsig is None:
                continue
            why = fn_diff(cs[i][1], fsig)
            if why:
                ln = s.count('\n', 0, m.start()) + 1
                hits.append(('B', a, '%s:%d -> %s()' % (rel(p), ln, callee), why))

# check C: `.member = func;` / `->member = func;` where the struct member is a function pointer
members = collections.defaultdict(list)
for p, s in files.items():
    for m in re.finditer(r'[;{]\s*([^;{}]*?\(\s*\*\s*(\w+)\s*\)\s*\(([^;{}]*?)\))\s*;', s):
        members[m.group(2)].append(sig(m.group(3)))
for p, s in files.items():
    for m in re.finditer(r'(?:\.|->)(\w+)\s*=\s*&?([A-Za-z_]\w*)\s*;', s):
        mem, fn = m.groups()
        if mem not in members or fn not in decls:
            continue
        fsig = decls[fn][0][2]
        for msig in members[mem]:
            why = fn_diff(msig, fsig)
            if why:
                ln = s.count('\n', 0, m.start()) + 1
                hits.append(('C', fn, '%s:%d .%s' % (rel(p), ln, mem), why))
                break

# check D: the INVOKER's view. For every function DEFINED with a function-pointer parameter, compare that
# pointer type against the DEFINITION of every function passed in that position anywhere (any file, casts ignored).
# Only dangerous direction is reported: the callback reads a wider value than the invoker sets.
defs = {}
for name, ds in decls.items():
    for d in ds:
        if d[3]:
            defs.setdefault(name, d)
CALLANY = re.compile(r'(?<![\w.>])([A-Za-z_]\w*)\s*\(((?:[^;{}()]|\([^;{}()]*\))*)\)')
order = {'1': 1, '2': 2, '4': 4, 'E': 4, '8': 8, 'P': 8}
for p, s in files.items():
    for m in CALLANY.finditer(s):
        callee = m.group(1)
        if callee in KEYW or callee not in defs:
            continue
        isig = defs[callee][2]
        for i, a in enumerate(split_params(m.group(2))):
            a = re.sub(r'^\(\s*[\w\s*()]+\)\s*', '', a.strip()).lstrip('&').strip()
            if i >= len(isig) or not isinstance(isig[i], tuple) or not re.fullmatch(r'[A-Za-z_]\w*', a):
                continue
            if a not in defs:
                continue
            fsig = defs[a][2]
            inv = isig[i][1]
            for j, (x, y) in enumerate(zip(inv, fsig)):
                if x in order and y in order and order[y] > order[x] and not (x == 'P' or y == 'P' and x in '8'):
                    ln = s.count('\n', 0, m.start()) + 1
                    hits.append(('D', a, '%s:%d -> %s() (defined %s:%d)' % (rel(p), ln, callee, rel(defs[callee][0]), defs[callee][1]),
                                 'arg %d: invoker sets %s, callback reads %s' % (j + 1, x, y)))
                    break

seen = set()
for h in sorted(hits):
    if h in seen:
        continue
    seen.add(h)
    print('\t'.join(h))
print('%d hits' % len(seen), file=sys.stderr)
