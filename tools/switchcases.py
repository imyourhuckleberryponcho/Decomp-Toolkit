"""switchcases.py [--min 85] [--all] - non-exact functions whose compare constants differ from the target.

Collects every `cmpwi/cmplwi rX, IMM` that is followed by a conditional branch (switch dispatch trees and plain
ifs) in the target and in our object, and lists the constants found only on one side. Typical finds:
- a switch case missing in the draft: target `cmpwi 2; bge` vs ours `cmpwi 1; beq; bge` (add `case 1:` to the right group - decompilers drop cases that share a body with `default`);
- `if (x < 0x19F && x >= 0x19D)` that was really `switch (x) { case 0x19D: case 0x19E: ... }`;
- signed/unsigned compare kind (cmpwi vs cmplwi) = wrong variable type.
By default only functions with at least one switch-like tree (>= 3 compares on one side) are listed; --all lists all.
Uses the objects ninja built. usage (from the project root): python switchcases.py [--min 85]
"""
from dtkproj import *   # REPO, VER, EXE, HERE; chdir to the project root
import argparse, json, os, re, sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from argextra import disasm, ROOT

BR = ('beq', 'bne', 'blt', 'bgt', 'bge', 'ble')


def consts(ins):
    c = Counter()
    for k in range(len(ins) - 1):
        op, args = ins[k][0], ins[k][1]
        if op not in ('cmpwi', 'cmplwi') or ins[k + 1][0] not in BR:
            continue
        m = re.search(r',\s*(-?0x[0-9a-fA-F]+|-?\d+)\s*$', args)
        if m:
            c[(op, int(m.group(1), 0))] += 1
    return c


def fmt(c):
    return ' '.join('%s%s%s' % ('u' if op == 'cmplwi' else '', hex(v) if abs(v) > 9 else v,
                                '' if n == 1 else 'x%d' % n) for (op, v), n in sorted(c.items(), key=lambda t: t[0][1]))


def check(unit, fnames, show_all):
    tobj = f'{ROOT}/build/{VER}/obj/{unit}.o'
    mobj = f'{ROOT}/build/{VER}/src/{unit}.o'
    if not (os.path.exists(tobj) and os.path.exists(mobj)):
        return []
    try:
        T, M = disasm(tobj), disasm(mobj)
    except Exception:
        return []
    out = []
    for fn, pct in fnames:
        if fn not in T or fn not in M:
            continue
        a, b = consts(T[fn]), consts(M[fn])
        if a == b:
            continue
        if not show_all and max(sum(a.values()), sum(b.values())) < 3:
            continue
        out.append((pct, unit, fn, a - b, b - a))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--min', type=float, default=85)
    ap.add_argument('--all', action='store_true')
    a = ap.parse_args()
    rep = json.load(open(f'{ROOT}/build/{VER}/report.json'))
    todo = {}
    for u in rep['units']:
        name = u['name'].split('/', 1)[1] if '/' in u['name'] else u['name']
        fs = [(f['name'], f.get('fuzzy_match_percent', 0)) for f in u.get('functions', [])
              if a.min <= f.get('fuzzy_match_percent', 0) < 100]
        if fs:
            todo[name] = fs
    with ThreadPoolExecutor(8) as ex:
        res = sorted((r for rs in ex.map(lambda kv: check(kv[0], kv[1], a.all), todo.items()) for r in rs),
                     reverse=True)
    for pct, unit, fn, ta, ma in res:
        print(f'{pct:6.2f}  {unit}  {fn}\n        target only: {fmt(ta) or "-"}   ours only: {fmt(ma) or "-"}')
    print(f'{len(res)} functions (u = unsigned compare)')


if __name__ == '__main__':
    main()
