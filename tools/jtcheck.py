"""jtcheck.py [--only U] [-j 6] [--all]: jump-table check.

A switch's jump table is DATA: one entry per case number, pointing at the code for that case. objdiff's fuzzy score
compares code only, and fcheck's DATA check masks relocated words, i.e. exactly these entries. So a draft with a
wrong case label (`case 0x575` where the original has 0x57B) can score 100%
and still be wrong (it breaks the DOL once the unit is linked).

For every NonMatching unit (or --all units): compiles once (fcheck), disassembles ours and the original, collects
each function's jump tables in order, turns every entry into the RANK of its target block (so code-size differences
do not matter) and compares. Reports:
  CASES   same table length, but some case numbers lead to other blocks (wrong/missing case labels, case order)
  RANGE   different table length (lowest/highest case differs)
  COUNT   different number of jump tables in the function
Output: build/scratch/jtcheck.md
"""
from dtkproj import *   # REPO, VER, EXE, HERE; chdir to the project root
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

H = os.path.dirname(os.path.abspath(__file__))
FCHECK = os.path.join(H, 'fcheck.py')
DTK = os.path.join('build', 'tools', 'dtk' + EXE)
a = sys.argv[1:]
ONLY = a[a.index('--only') + 1] if '--only' in a else None
J = int(a[a.index('-j') + 1]) if '-j' in a else 6


def tables(obj, out, run=True):
    """{function: [[offset of entry target, ...] per table in order]}; run=False reuses an existing disassembly"""
    if run or not os.path.exists(out):
        subprocess.run([DTK, 'elf', 'disasm', obj, out], capture_output=True, stdin=subprocess.DEVNULL)
    try:
        txt = open(out, encoding='utf-8', errors='replace').read()
    except OSError:
        return {}
    start = {}
    cur = None
    for l in txt.split('\n'):
        m = re.match(r'\.fn "?([^",]+)"?,', l)
        if m:
            cur = m.group(1)
            continue
        if cur and cur not in start:
            mm = re.match(r'/\* ([0-9A-F]{8}) ', l)
            if mm:
                start[cur] = int(mm.group(1), 16)
    res = {}
    obj_name, rows = None, []

    def flush():
        if rows:
            fn = rows[0][0]
            if all(r[0] == fn for r in rows) and fn in start:
                res.setdefault(fn, []).append([r[1] - start[fn] for r in rows])
    for l in txt.split('\n'):
        if l.startswith('.obj '):
            flush()
            obj_name, rows = l, []
            continue
        m = re.match(r'\s+\.rel "?([^",]+)"?, \.L_([0-9A-F]{8})$', l)
        if m and obj_name:
            rows.append((m.group(1), int(m.group(2), 16)))
        elif l.startswith('.endobj'):
            flush()
            obj_name, rows = None, []
    flush()
    return res


def mismatch(target_tables, our_tables):
    """number of jump-table entries that differ (by target-block rank); table count/length differences count fully"""
    tt, ot = target_tables or [], our_tables or []
    n = abs(len(tt) - len(ot)) * 4
    for x, y in zip(tt, ot):
        if len(x) != len(y):
            n += abs(len(x) - len(y)) + 2
        else:
            n += sum(1 for p, q in zip(ranks(x), ranks(y)) if p != q)
    return n


def ranks(t):
    order = {v: i for i, v in enumerate(sorted(set(t)))}
    return [order[v] for v in t]


def check_unit(unit):
    tag = 'jt_' + re.sub(r'\W', '_', unit)[-50:]
    o = subprocess.run([sys.executable, FCHECK, unit, '--tag', tag], capture_output=True, text=True,
                       encoding='utf-8', errors='replace', stdin=subprocess.DEVNULL).stdout
    m = re.search(r'obj=(\S+)', o)
    out = []
    if not m:
        return unit, None
    ours = tables(m.group(1).replace('\\', '/'), 'build/scratch/fcheck/%s/ours_jt.s' % tag)
    targ = tables(S('build/@V/obj/%s.o') % unit, 'build/scratch/fcheck/%s/target_jt.s' % tag)
    for fn, tt in targ.items():
        ot = ours.get(fn)
        if ot is None:
            continue   # function not written / not paired: other tools cover that
        if len(ot) != len(tt):
            out.append(('COUNT', fn, '%d tables vs original %d' % (len(ot), len(tt))))
            continue
        for k, (x, y) in enumerate(zip(tt, ot)):
            if len(x) != len(y):
                out.append(('RANGE', fn, 'table %d: %d entries vs original %d' % (k, len(y), len(x))))
            elif ranks(x) != ranks(y):
                bad = [i for i, (p, q) in enumerate(zip(ranks(x), ranks(y))) if p != q]
                out.append(('CASES', fn, 'table %d: %d of %d entries lead elsewhere (indexes %s)' % (
                    k, len(bad), len(x), ', '.join(map(str, bad[:12])) + (' ...' if len(bad) > 12 else ''))))
    import shutil
    shutil.rmtree('build/scratch/fcheck/' + tag, ignore_errors=True)
    return unit, out


def main():
    cfg = open('configure.py', encoding='utf-8').read()
    pat = r'Object\((?:NonMatching|MatchingFor\([^)]*\)|Equivalent)?,?\s*"([^"]+)\.(?:cpp|c)"' if '--all' in a \
        else r'Object\(NonMatching, "([^"]+)\.(?:cpp|c)"'
    units = sorted(u for u in set(re.findall(pat, cfg)) if os.path.exists('src/%s.cpp' % u) or os.path.exists('src/%s.c' % u))
    if ONLY:
        units = [u for u in units if ONLY in u]
    rows = []
    with ThreadPoolExecutor(J) as ex:
        for unit, res in ex.map(check_unit, units):
            for r in res or []:
                rows.append((unit,) + r)
                print('%-6s %-40s %-50s %s' % (r[0], unit.split('/')[-1], r[1][:50], r[2]), flush=True)
    with open('build/scratch/jtcheck.md', 'w', encoding='utf-8') as f:
        f.write('# Jump-table check (%d units)\n\n' % len(units))
        for r in rows:
            f.write('- %s %s %s: %s\n' % (r[1], r[0], r[2], r[3]))
    print('%d units checked, %d jump-table problems (build/scratch/jtcheck.md)' % (len(units), len(rows)))


if __name__ == '__main__':
    main()
