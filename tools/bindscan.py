"""bindscan.py: functions whose symbol BINDING differs from the original (local = `static` / anonymous namespace,
global, weak = inline/template emitted in several units). A local-vs-global mismatch is a one-word source fix
(`static`), changes code generation (the original's local functions can get inlined/ordered differently) and blocks
linking. Uses the objects ninja built (build/<VER>/src/...).

usage (from the project root): python bindscan.py [--all-units] [-j 8]
  default: NonMatching units only. Prints unit, function, target binding -> ours.
"""
from dtkproj import *   # REPO, VER, EXE, HERE; chdir to the project root
import os, re, subprocess, sys
from concurrent.futures import ThreadPoolExecutor

os.chdir(REPO)
a = sys.argv[1:]
J = int(a[a.index('-j') + 1]) if '-j' in a else 8
cfg = open('configure.py', encoding='utf-8').read()
units = re.findall(r'Object\((NonMatching|MatchingFor\("[^"]+"\)),\s*"([^"]+)\.(c|cpp)"', cfg)
if '--all-units' not in a:
    units = [u for u in units if u[0] == 'NonMatching']
DTK = os.path.abspath('build/tools/dtk' + EXE)
os.makedirs('build/scratch/bindscan', exist_ok=True)


def binds(text):
    return {m.group(1): m.group(2) for m in re.finditer(r'^\.fn (\S+), (local|global|weak)', text, re.M)}


def one(u):
    _, unit, ext = u
    obj = S('build/@V/src/%s.o') % unit
    tgt = S('build/@V/asm/%s.s') % unit
    if not (os.path.exists(obj) and os.path.exists(tgt)):
        return unit, []
    out = 'build/scratch/bindscan/%s.s' % unit.replace('/', '_')
    subprocess.run([DTK, 'elf', 'disasm', obj, out], capture_output=True)
    if not os.path.exists(out):
        return unit, []
    t = binds(open(tgt, encoding='utf-8', errors='replace').read())
    m = binds(open(out, encoding='utf-8', errors='replace').read())
    return unit, [(fn, t[fn], m[fn]) for fn in t if fn in m and t[fn] != m[fn]]


with ThreadPoolExecutor(J) as ex:
    res = list(ex.map(one, units))
n = 0
for unit, diffs in res:
    for fn, tb, mb in diffs:
        n += 1
        print('%-45s %-60s target %-6s ours %s' % (unit, fn[:60], tb, mb))
print('%d binding differences in %d units' % (n, len(units)))
