"""framescan.py [--min 90]: unfinished functions whose STACK FRAME SIZE differs from the target.

A different `stwu r1,-N(r1)` means a real source difference (wrong array size, a missing/extra local, a by-value
temp, nested-vs-flat inline helpers), never just register allocation. Reads our objects from build/<VER>/src/
(the normal ninja build) and the target asm from build/<VER>/asm/. Output sorted by match %:
  pct  size  unit  function  target_frame  our_frame  (diff)
Writes build/scratch/framescan.md as well.
"""
from dtkproj import *   # REPO, VER, EXE, HERE; chdir to the project root
import json, os, re, subprocess, sys

REPO = REPO
OBJDUMP = REPO + '/build/binutils/powerpc-eabi-objdump' + EXE
os.chdir(REPO)
mn = float(sys.argv[sys.argv.index('--min') + 1]) if '--min' in sys.argv else 90.0

rep = json.load(open(S('build/@V/report.json')))
want = {}
for u in rep['units']:
    for f in u.get('functions', []):
        p = f.get('fuzzy_match_percent', 0)
        if mn <= p < 100:
            want.setdefault(u['name'].replace('main/', ''), {})[f['name']] = (p, int(f.get('size', 0)))


def target_frames(unit):
    path = S('build/@V/asm/%s.s') % unit
    if not os.path.exists(path):
        return {}
    res, cur = {}, None
    for l in open(path, encoding='utf-8', errors='replace'):
        m = re.match(r'\.fn (\S+),', l)
        if m:
            cur = m.group(1)
            continue
        if cur and cur not in res:
            m = re.search(r'stwu r1, -0x([0-9a-f]+)\(r1\)', l)
            if m:
                res[cur] = int(m.group(1), 16)
        if l.startswith('.endfn'):
            res.setdefault(cur, 0)
            cur = None
    return res


def our_frames(unit):
    obj = None
    for ext in ('.cpp', '.c'):
        if os.path.exists('src/%s%s' % (unit, ext)):
            obj = S('build/@V/src/%s.o') % unit
    if not obj or not os.path.exists(obj):
        return {}
    d = subprocess.run([OBJDUMP, '-d', obj], capture_output=True, text=True).stdout
    res, cur = {}, None
    for l in d.splitlines():
        m = re.match(r'[0-9a-f]+ <(.+)>:', l)
        if m:
            cur = m.group(1)
            continue
        if cur and cur not in res:
            m = re.search(r'stwu\s+r1,-(\d+)\(r1\)', l)
            if m:
                res[cur] = int(m.group(1))
            elif re.search(r'\sblr\b', l):
                res[cur] = 0
    return res


rows = []
for unit, fns in want.items():
    t, o = target_frames(unit), our_frames(unit)
    for fn, (p, sz) in fns.items():
        if fn in t and fn in o and t[fn] != o[fn]:
            rows.append((p, sz, unit, fn, t[fn], o[fn]))
rows.sort(key=lambda r: -r[0])
out = ['%6.2f %5d  %s  %s  target 0x%x ours 0x%x (%+d)' % (r[0], r[1], r[2], r[3], r[4], r[5], r[5] - r[4]) for r in rows]
print('\n'.join(out))
print('%d functions with a different frame size' % len(rows))
open('build/scratch/framescan.md', 'w', encoding='utf-8').write('\n'.join(out) + '\n')
