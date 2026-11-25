"""regclass.py - what KIND of difference do the near misses really have? And: which "100%" functions are not identical?

usage (project root): python tools/regclass.py [--min 99] [-j 8]      near misses, classified
                      python tools/regclass.py --full100 [-j 8]       functions scored 100% that are NOT identical
Compiles each unit once and compares every selected function with the original instruction by instruction
(unitlib.strict_norm: registers, immediates, call and branch targets included):
  regs-only      same instructions in the same order, only register numbers differ
  order-only     same instructions (registers ignored) in another order
  extra/missing  ours has more / fewer instructions
  other          same count, different instructions (with --full100: often a different call target or immediate)
Writes build/scratch/regclass.tsv (regclass100.tsv): unit, function, size, percent, class, differing instructions.
Run --full100 after every automatic session: functions listed there look finished and are not.
"""
import argparse
import os
import re
import shutil
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import unitlib as U   # noqa: E402

REG = re.compile(r'\b[rf]\d+\b')


def classify(t, o):
    if t == o:
        return 'exact', 0
    n = sum(max(i2 - i1, j2 - j1) for op, i1, i2, j1, j2 in SequenceMatcher(None, t, o, autojunk=False).get_opcodes()
            if op != 'equal')
    if len(t) != len(o):
        return ('extra' if len(o) > len(t) else 'missing'), n
    rt, ro = [REG.sub('R', x) for x in t], [REG.sub('R', x) for x in o]
    if rt == ro:
        return 'regs-only', n
    if sorted(rt) == sorted(ro):
        return 'order-only', n
    return 'other', n


def unit_job(item):
    unit, funcs = item
    tag = 'regclass_' + re.sub(r'\W', '_', unit)[-60:]
    fresh, _, obj = U.scores(unit, None, tag)     # report.json may be older than the source
    res, skipped = [], 0
    if obj:
        ours, targ = U.disasm_funcs(obj, obj + '.s'), U.target_funcs(unit)
        for fn, size, p in funcs:
            now = fresh.get(fn, p)
            if (now >= 100) != (p >= 100):
                continue
            if fn in ours and fn in targ:
                c, n = classify(U.strict_norm(targ[fn]), U.strict_norm(ours[fn]))
                res.append((unit, fn, size, now, c, n))
            else:
                skipped += 1
    else:
        skipped = len(funcs)
    shutil.rmtree('build/scratch/fcheck/' + tag, ignore_errors=True)
    return res, skipped


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--min', type=float, default=99.0)
    ap.add_argument('--full100', action='store_true')
    ap.add_argument('--unit-filter', default='')
    ap.add_argument('-j', type=int, default=8)
    a = ap.parse_args()
    work = {}
    for unit, fs in U.units().items():
        if a.unit_filter not in unit or not U.source_of(unit):
            continue
        for f in fs:
            p = float(f.get('fuzzy_match_percent') or 0)
            if (p >= 100) if a.full100 else (a.min <= p < 100):
                work.setdefault(unit, []).append((f['name'], int(f.get('size', 0)), p))
    rows, skipped = [], 0
    with ThreadPoolExecutor(a.j) as ex:
        for r, s in ex.map(unit_job, sorted(work.items())):
            rows += r
            skipped += s
    checked = len(rows)
    if a.full100:
        rows = [r for r in rows if r[4] != 'exact']
    os.makedirs('build/scratch', exist_ok=True)
    out = 'build/scratch/regclass%s.tsv' % ('100' if a.full100 else '')
    with open(out, 'w', encoding='utf-8') as f:
        for r in sorted(rows, key=lambda r: (r[4], r[5])):
            f.write('%s\t%s\t%d\t%.2f\t%s\t%d\n' % r)
    c, b, small = Counter(r[4] for r in rows), Counter(), Counter()
    for r in rows:
        b[r[4]] += r[2]
        small[r[4]] += r[5] <= 4
    if a.full100:
        print('%d functions at 100%% checked: %d are NOT identical' % (checked, len(rows)))
    else:
        print('%d near misses >= %.1f%% in %d units' % (checked, a.min, len(work)))
    for k, v in c.most_common():
        print('  %-11s %4d functions  %7d bytes  (%d with <= 4 differing instructions)' % (k, v, b[k], small[k]))
    if skipped:
        print('  %d functions could not be checked (unit does not compile, or name differs)' % skipped)
    print('-> ' + out)


if __name__ == '__main__':
    main()
