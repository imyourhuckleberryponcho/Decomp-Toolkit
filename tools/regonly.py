"""regonly.py - sort unfinished functions by WHAT is wrong.

For every non-exact function in report.json (default: >= --min percent) it disassembles the target object
(build/<VER>/obj/<unit>.o) and ours (build/<VER>/src/<unit>.o), and compares them:
  reg      same instructions, only register numbers differ   -> permuter territory
  regimm   same opcodes, registers + immediates/offsets differ -> often a stack/struct-offset or constant issue
  order    same multiset of normalised instructions, different order -> scheduling / statement order
  struct   different instruction count or opcodes             -> source shape is wrong (hand work)
Last column (tsv/--show) = instructions that differ once registers are ignored (small = cheap hand fix).
Usage: python regonly.py [--min 95] [--max 100] [--unit-filter S] [--tsv out.tsv] [--show N]
"""
from dtkproj import *   # REPO, VER, EXE, HERE; chdir to the project root
import argparse, difflib, json, os, re, subprocess, sys
from collections import Counter

ROOT = REPO
OBJDUMP = ROOT + '/build/binutils/powerpc-eabi-objdump' + EXE
REG = re.compile(r'\b(?:r|f|cr)\d+\b')
IMM = re.compile(r'-?\b(?:0x[0-9a-fA-F]+|\d+)\b')


def disasm(obj):
    out = subprocess.run([OBJDUMP, '-d', '-r', '--no-show-raw-insn', obj], capture_output=True, text=True).stdout
    funcs, cur = {}, None
    for line in out.splitlines():
        m = re.match(r'^[0-9a-f]+ <(.+)>:$', line)
        if m:
            cur = funcs.setdefault(m.group(1), [])
            continue
        if cur is None:
            continue
        m = re.match(r'^\s+[0-9a-f]+:\s+(\S+)\s*(.*)$', line)
        if m:
            op, args = m.group(1), m.group(2)
            if 'R_PPC' in op:
                continue
            args = re.sub(r'\s*<.*>$', '', args)          # branch target names
            if op.startswith('b') and re.match(r'^[0-9a-f]+$', args.strip()):
                args = 'L'
            cur.append((op, args))
    return funcs


def ndiff(t, o):
    """number of normalised instructions that differ (registers ignored)"""
    a = [x + ' ' + REG.sub('r', y) for x, y in t]
    b = [x + ' ' + REG.sub('r', y) for x, y in o]
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    return sum(max(i2 - i1, j2 - j1) for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag != 'equal')


def kind(t, o):
    if t == o:
        return 'exact'
    if len(t) != len(o) or [a for a, _ in t] != [a for a, _ in o]:
        nt = Counter((a, REG.sub('r', b)) for a, b in t)
        no = Counter((a, REG.sub('r', b)) for a, b in o)
        return 'order' if nt == no else 'struct'
    if all(REG.sub('r', a[1]) == REG.sub('r', b[1]) for a, b in zip(t, o)):
        return 'reg'
    if all(IMM.sub('#', REG.sub('r', a[1])) == IMM.sub('#', REG.sub('r', b[1])) for a, b in zip(t, o)):
        return 'regimm'
    return 'struct'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--min', type=float, default=95)
    ap.add_argument('--max', type=float, default=100)
    ap.add_argument('--unit-filter', default='')
    ap.add_argument('--tsv')
    ap.add_argument('--show', type=int, default=0)
    a = ap.parse_args()
    rep = json.load(open(ROOT + S('/build/@V/report.json')))
    rows = []
    for u in rep['units']:
        name = u['name']
        if a.unit_filter not in name:
            continue
        fs = [f for f in u.get('functions', []) if a.min <= (f.get('fuzzy_match_percent') or 0) < a.max]
        if not fs:
            continue
        unit = name.split('/', 1)[1]
        to, oo = S('%s/build/@V/obj/%s.o') % (ROOT, unit), S('%s/build/@V/src/%s.o') % (ROOT, unit)
        if not (os.path.exists(to) and os.path.exists(oo)):
            continue
        T, O = disasm(to), disasm(oo)
        for f in fs:
            n = f['name']
            if n in T and n in O:
                k, d = kind(T[n], O[n]), ndiff(T[n], O[n])
            else:
                k, d = 'missing', -1
            rows.append((k, unit, n, int(f.get('size', 0)), f.get('fuzzy_match_percent') or 0, d))
    c = Counter(r[0] for r in rows)
    b = Counter()
    for r in rows:
        b[r[0]] += r[3]
    for k in sorted(c, key=lambda k: -b[k]):
        print('%-8s %5d funcs %7.0f KB' % (k, c[k], b[k] / 1024))
    if a.tsv:
        with open(a.tsv, 'w') as fh:
            for r in sorted(rows):
                fh.write('%s\t%s\t%s\t%d\t%.2f\t%d\n' % r)
    if a.show:
        for r in sorted(rows, key=lambda r: (r[0], -r[3]))[:a.show]:
            print('%-7s %-45s %-45s %6d %6.2f %4d' % (r[0], r[1][:45], r[2][:45], r[3], r[4], r[5]))


if __name__ == '__main__':
    main()
