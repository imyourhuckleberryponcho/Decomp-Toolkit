"""poolscan.py: unfinished units whose anonymous constants (.sdata2/.rodata @N) have the right VALUES in
the wrong ORDER (fcheck's POOL ORDER hint, see fcheck.pool_order_hint).

  - used by ONE function  -> expression order inside it (constants pool in source text order; split/reorder the
                             expression: e.g. `a = sin(2pi*t); a = 1+a; a = 150*a`)
  - REVERSED across functions -> deferred file (-inline noauto,deferred + revfuncs.py)
  - permuted across functions -> function order, a dead-stripped earlier user (placeholder function), or expressions

usage (from the project root): python poolscan.py [-j 8]   -> build/scratch/poolscan.md
"""
from dtkproj import *   # REPO, VER, EXE, HERE; chdir to the project root
import os, re, subprocess, sys
from concurrent.futures import ThreadPoolExecutor

os.chdir(REPO)
H = os.path.dirname(os.path.abspath(__file__))
J = int(sys.argv[sys.argv.index('-j') + 1]) if '-j' in sys.argv else 8
cfg = open('configure.py', encoding='utf-8').read()
units = [m.group(1) for m in re.finditer(r'Object\(NonMatching,\s*"([^"]+)\.(?:cpp|c|cp)"', cfg)]


def scan(u):
    tag = 'pool_' + re.sub(r'\W', '_', u)[-60:]
    try:
        r = subprocess.run([sys.executable, os.path.join(H, 'fcheck.py'), u, '--tag', tag], capture_output=True,
                           text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return u, None
    lines = r.stdout.splitlines()
    out = []
    for i, l in enumerate(lines):
        if 'POOL ORDER' in l:
            out.append(l.strip())
            out += [x.strip() for x in lines[i + 1:i + 3]]
    return u, out


with ThreadPoolExecutor(J) as ex:
    res = [x for x in ex.map(scan, units) if x[1]]
one = [x for x in res if any('ONE function' in l for l in x[1])]
rev = [x for x in res if any('REVERSED' in l for l in x[1]) and x not in one]
rest = [x for x in res if x not in one and x not in rev]
md = ['# Constant pool order differs (same values) in %d unfinished units' % len(res), '']
for title, xs in (('Expression order inside one function (quick fixes)', one), ('Reversed across functions (deferred file?)', rev),
                  ('Permuted across functions', rest)):
    md += ['## %s: %d' % (title, len(xs)), '']
    for u, ls in xs:
        md.append('- **%s**' % u)
        md += ['    ' + l[:300] for l in ls]
    md.append('')
open('build/scratch/poolscan.md', 'w', encoding='utf-8').write('\n'.join(md) + '\n')
print('pool order: %d units (one-function %d, reversed %d, permuted %d) -> build/scratch/poolscan.md' % (len(res), len(one), len(rev), len(rest)))
for u, ls in one + rev:
    print('  ' + u + ': ' + ls[0][:160])
