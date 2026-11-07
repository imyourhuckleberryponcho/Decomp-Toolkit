"""signscan.py - signed/unsigned and width mismatches in unfinished functions.

usage (from the project root): python signscan.py [-j 6] [--min 0] [--unit SUBSTR]
  For every non-exact function (report.json) it takes the register-normalised instruction difference (like
  structdist.py) and flags PAIRS where the target does the signed form and we the unsigned one (or the other way):
    extsb vs clrlwi 24 / extsh      srawi vs srwi       cmpw/cmpwi vs cmplw/cmplwi
    divw vs divwu                   mulhw vs mulhwu     lha vs lhz      extsh vs clrlwi 16
  Such pairs are VALUE differences (wrong variable/field/parameter signedness), not code shape: a glyph
  y-offset (extsb vs extsh) once drew glyphs 256 px off. Output: build/scratch/signscan.md (worst first).
"""
from dtkproj import *   # REPO, VER, EXE, HERE; chdir to the project root
import json, os, re, subprocess, sys, shutil
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

os.chdir(REPO)
a = sys.argv[1:]
opt = lambda n, d=None: a[a.index(n) + 1] if n in a else d
J = int(opt('-j', 6))
MINP = float(opt('--min', 0))
USUB = opt('--unit')
H = HERE + '/'

PAIRS = [
    (r'^extsb ', r'^(clrlwi R, R, 24|extsh )', 'extsb vs zero/half extend'),
    (r'^(clrlwi R, R, 24)', r'^extsb ', 'u8 vs s8'),
    (r'^srawi ', r'^srwi ', 'signed vs unsigned shift'),
    (r'^srwi ', r'^srawi ', 'unsigned vs signed shift'),
    (r'^cmpwi? ', r'^cmplwi? ', 'signed vs unsigned compare'),
    (r'^cmplwi? ', r'^cmpwi? ', 'unsigned vs signed compare'),
    (r'^divw ', r'^divwu ', 'signed vs unsigned divide'),
    (r'^divwu ', r'^divw ', 'unsigned vs signed divide'),
    (r'^mulhw ', r'^mulhwu ', 'signed vs unsigned mulhi'),
    (r'^mulhwu ', r'^mulhw ', 'unsigned vs signed mulhi'),
    (r'^lha ', r'^lhz ', 's16 vs u16 load'),
    (r'^lhz ', r'^lha ', 'u16 vs s16 load'),
    (r'^extsh ', r'^clrlwi R, R, 16', 's16 vs u16'),
    (r'^clrlwi R, R, 16', r'^extsh ', 'u16 vs s16'),
]


def norm(l):
    return re.sub(r'\bf\d+\b', 'F', re.sub(r'\br\d+\b', 'R', l[1:].strip()))


def check(job):
    unit, fn, pct = job
    tag = 'sign_%d_%s' % (os.getpid(), re.sub(r'\W', '_', fn)[:40])
    out = subprocess.run([sys.executable, H + 'fcheck.py', unit, '--tag', tag, '--diff', fn],
                         capture_output=True, text=True).stdout
    shutil.rmtree('build/scratch/fcheck/' + tag, ignore_errors=True)
    L = [l for l in out.splitlines() if l[:1] in '+-' and not l.startswith(('---', '+++'))]
    m = Counter(norm(l) for l in L if l[0] == '-')
    p = Counter(norm(l) for l in L if l[0] == '+')
    tgt, ours = m - p, p - m
    hits = []
    for t_re, o_re, label in PAIRS:
        nt = sum(v for k, v in tgt.items() if re.search(t_re, k))
        no = sum(v for k, v in ours.items() if re.search(o_re, k))
        if nt and no:
            hits.append((min(nt, no), label))
    return unit, fn, pct, hits


r = json.load(open(S('build/@V/report.json')))
jobs = []
for u in r['units']:
    name = u['name'].replace('main/', '', 1)
    if USUB and USUB not in name:
        continue
    for f in u.get('functions', []):
        pct = f.get('fuzzy_match_percent', 0)
        if pct < 100 and pct >= MINP:
            jobs.append((name, f['name'], pct))
print('%d functions' % len(jobs), flush=True)
res = []
with ThreadPoolExecutor(J) as ex:
    for i, x in enumerate(ex.map(check, jobs)):
        if x[3]:
            res.append(x)
        if i % 50 == 0:
            print('  %d/%d' % (i, len(jobs)), flush=True)
res.sort(key=lambda x: -sum(n for n, _ in x[3]))
os.makedirs('build/scratch', exist_ok=True)
with open('build/scratch/signscan.md', 'w') as f:
    f.write('# signscan: signed/unsigned pairs in unfinished functions\n\n')
    for unit, fn, pct, hits in res:
        f.write('- %6.2f %s %s: %s\n' % (pct, unit, fn, ', '.join('%s x%d' % (l, n) for n, l in hits)))
print('%d functions with pairs -> build/scratch/signscan.md' % len(res))
