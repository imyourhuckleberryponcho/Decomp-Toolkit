"""callorder.py <unit> [func ...] : target vs our call sequence (bl/b to symbols) per function, as a diff.
Without function names it lists every function of the unit whose call SEQUENCE differs (same calls, other order,
or different calls). Uses fcheck's compiled object (one compile)."""
from dtkproj import *   # REPO, VER, EXE, HERE; chdir to the project root
import sys, os, re, subprocess, difflib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import reldiff

unit = sys.argv[1]
want = sys.argv[2:]
tag = 'callorder_' + re.sub(r'\W', '_', unit)[-40:]
ours = reldiff.compile_unit(unit, tag)
tf = reldiff.disasm(S('build/@V/obj/%s.o') % unit, 'build/scratch/fcheck/%s/t.s' % tag)
of = reldiff.disasm(ours, 'build/scratch/fcheck/%s/o.s' % tag)


def calls(lines):
    out = []
    for l in lines:
        m = re.search(r'\*/\s+(bl|b)\s+([A-Za-z_"][^\s]*)\s*$', l)
        if m and not m.group(2).startswith('.L'):
            out.append(m.group(2).strip('"'))
    return out


for f in (want or sorted(tf)):
    if f not in tf or f not in of:
        continue
    t, o = calls(tf[f]), calls(of[f])
    if t == o:
        if want:
            print('%s: same %d calls' % (f, len(t)))
        continue
    kind = 'ORDER' if sorted(t) == sorted(o) else 'CALLS'
    print('== %s  [%s]  target %d calls, ours %d' % (f, kind, len(t), len(o)))
    for l in difflib.unified_diff(t, o, 'target', 'ours', lineterm='', n=1):
        if not l.startswith(('---', '+++')):
            print('   ' + l)
