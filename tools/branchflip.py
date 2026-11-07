"""branchflip.py - find draft bugs where a condition is INVERTED.

For every non-exact function in report.json it aligns target vs ours (registers ignored, like regonly.py) and lists
1:1 replacements where the target has a conditional branch and ours has the OPPOSITE one at the same place
(bgt<->ble, blt<->bge, beq<->bne). With the rest of the code identical this usually means the draft tests the wrong
way round (`> 0` written where the original has `<= 0`). It can also be a compare whose operands
are swapped (`cmpw a,b; bgt` vs `cmpw b,a; blt`): the compare line before the branch is printed with registers so
you can tell (same register order + flipped branch = inverted condition).

Usage (from the project root): python branchflip.py [--min 80] [--unit-filter S]
Uses build/<VER>/src/<unit>.o (last full build), so confirm hits with fcheck --diff.
"""
from dtkproj import *   # REPO, VER, EXE, HERE; chdir to the project root
import argparse, difflib, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from regonly import disasm, REG, ROOT

OPP = {'bgt': 'ble', 'ble': 'bgt', 'blt': 'bge', 'bge': 'blt', 'beq': 'bne', 'bne': 'beq'}


def base_op(op):
    return op.rstrip('+-').rstrip('l') if op.endswith(('l+', 'l-')) else op.rstrip('+-')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--min', type=float, default=80)
    ap.add_argument('--unit-filter', default='')
    a = ap.parse_args()
    os.chdir(ROOT)
    rep = json.load(open(S('build/@V/report.json')))
    hits = 0
    for u in rep['units']:
        name = u['name'].split('/', 1)[1]
        if a.unit_filter and a.unit_filter not in name:
            continue
        fns = [f for f in u.get('functions', []) if a.min <= f.get('fuzzy_match_percent', 0) < 100]
        if not fns:
            continue
        tobj, oobj = S('build/@V/obj/%s.o') % name, S('build/@V/src/%s.o') % name
        if not (os.path.exists(tobj) and os.path.exists(oobj)):
            continue
        T, O = disasm(tobj), disasm(oobj)
        for f in fns:
            t, o = T.get(f['name']), O.get(f['name'])
            if not t or not o:
                continue
            na = [x + ' ' + REG.sub('r', y) for x, y in t]
            nb = [x + ' ' + REG.sub('r', y) for x, y in o]
            sm = difflib.SequenceMatcher(None, na, nb, autojunk=False)
            other = 0
            flips = []
            for tag, i1, i2, j1, j2 in sm.get_opcodes():
                if tag == 'equal':
                    continue
                if tag == 'replace' and i2 - i1 == j2 - j1:
                    for k in range(i2 - i1):
                        ta, oa = t[i1 + k][0], o[j1 + k][0]
                        if OPP.get(base_op(ta)) == base_op(oa):
                            prev_t = ' '.join(t[i1 + k - 1]) if i1 + k else ''
                            prev_o = ' '.join(o[j1 + k - 1]) if j1 + k else ''
                            flips.append((i1 + k, ta, oa, prev_t, prev_o))
                        else:
                            other += 1
                else:
                    other += max(i2 - i1, j2 - j1)
            if flips:
                hits += 1
                print('%s  %s  %.2f%%  flips=%d other=%d' % (name, f['name'], f['fuzzy_match_percent'], len(flips), other))
                for idx, ta, oa, pt, po in flips[:4]:
                    print('    @%d target %-5s after [%s]   ours %-5s after [%s]' % (idx, ta, pt, oa, po))
    print('%d functions with flipped branches' % hits)


if __name__ == '__main__':
    main()
