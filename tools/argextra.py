"""argextra.py - find calls where OUR code sets an argument register that the TARGET leaves alone.

Typical case: the game calls
getThing() WITHOUT an argument although its definition takes a pointer (old-style
`T f();` declaration in the original header), so r3 still holds whatever was there. Our drafts pass 0, which adds
an `li r3,0` before the call and shifts registers. Fix: declare the callee `T f();` in the caller's file and call it
with no arguments (the definition elsewhere keeps its parameter).

For every non-exact function in report.json (>= --min percent) both objects are disassembled; calls are matched per
callee in order; for each pair the argument registers (r3..r10, f1..f8) written since the previous call/branch/label
are compared. Printed: unit, function, callee, registers only WE write (with our instruction).
usage (from the project root): python argextra.py [--min 80] [--unit-filter S] [--strict] [--consts] [--widths]

--widths: argument registers narrowed to another width before a call than in the target (clrlwi/extsb/extsh) =
this file's prototype of the callee, or the local passed, has another width than the original's.
--consts: instead, list argument registers where WE load a constant (`li`) but the target loads a
DIFFERENT constant or a non-constant value (mr/lwz/...). Typical find: the draft
called f(type, 0) where the game calls f(id, type) - a wrong argument list that no score hint points at.
"""
from dtkproj import *   # REPO, VER, EXE, HERE; chdir to the project root
import argparse, json, os, re, subprocess, sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

ROOT = REPO
OBJDUMP = ROOT + '/build/binutils/powerpc-eabi-objdump' + EXE
ARGREG = re.compile(r'^(r(?:[3-9]|10)|f[1-8])$')
STRICT = False
CONSTS = False
WIDTHS = False


def disasm(obj):
    """{func: [(op, args, reloc_symbol_or_None, is_label_target)]}"""
    out = subprocess.run([OBJDUMP, '-d', '-r', '--no-show-raw-insn', obj], capture_output=True, text=True).stdout
    funcs, cur, last = {}, None, None
    for line in out.splitlines():
        m = re.match(r'^[0-9a-f]+ <(.+)>:$', line)
        if m:
            cur = funcs.setdefault(m.group(1), [])
            continue
        if cur is None:
            continue
        m = re.match(r'^\s+[0-9a-f]+:\s+R_PPC_\w+\s+(\S+)', line)
        if m and cur:
            op, args, _, lab = cur[-1]
            cur[-1] = (op, args, m.group(1), lab)
            continue
        m = re.match(r'^\s+([0-9a-f]+):\s+(\S+)\s*(.*)$', line)
        if m:
            cur.append((m.group(2), re.sub(r'\s*<.*>$', '', m.group(3)), None, int(m.group(1), 16)))
    return funcs


def calls(ins):
    """[(callee, {argreg: instruction text})] - arg registers written since the previous call/branch target."""
    targets = set()
    for op, args, rel, addr in ins:
        if op.startswith('b') and op != 'bl' and re.match(r'^[0-9a-f]+$', args.strip()):
            targets.add(int(args.strip(), 16))
    res, written = [], {}
    for op, args, rel, addr in ins:
        if addr in targets:
            written = {}
        if op in ('bl',):
            callee = rel or args
            res.append((callee, dict(written)))
            written = {}
            continue
        if op.startswith('b'):
            written = {}
            continue
        parts = [p.strip() for p in args.split(',')]
        if parts and ARGREG.match(parts[0]) and not op.startswith(('st', 'cmp', 'fcmp', 'mt')):
            written[parts[0]] = f'{op} {args}'
    return res


def check(unit, fnames, mobj=None, mode=None):
    """mobj: another compiled object (a scratch build) instead of build/<VER>/src; mode: 'strict' / 'consts' /
    'widths' / 'plain' instead of the module globals (thread-safe, used by argrepair.py)"""
    tobj = f'{ROOT}/build/{VER}/obj/{unit}.o'
    mobj = mobj or f'{ROOT}/build/{VER}/src/{unit}.o'
    strict, consts, widths = (STRICT, CONSTS, WIDTHS) if mode is None else (
        mode == 'strict', mode == 'consts', mode == 'widths')
    if not (os.path.exists(tobj) and os.path.exists(mobj)):
        return []
    T, M = disasm(tobj), disasm(mobj)
    out = []
    for fn in fnames:
        if fn not in T or fn not in M:
            continue
        tc, mc = defaultdict(list), defaultdict(list)
        for c, w in calls(T[fn]):
            tc[c].append(w)
        for c, w in calls(M[fn]):
            mc[c].append(w)
        for callee in tc:
            if len(tc[callee]) != len(mc.get(callee, [])):
                continue
            for i, (tw, mw) in enumerate(zip(tc[callee], mc[callee])):
                if widths:
                    def width(s):
                        m = re.match(r'clrlwi\.?\s+\w+,\s*\w+,\s*(\d+)', s or '')
                        if m:
                            return 'u%d' % (32 - int(m.group(1)))
                        m = re.match(r'(extsb|extsh)\b', s or '')
                        return {'extsb': 's8', 'extsh': 's16'}[m.group(1)] if m else None
                    bad = {}
                    for r in set(tw) | set(mw):
                        tws, mws = width(tw.get(r)), width(mw.get(r))
                        if tws != mws and (tws or mws) and r in tw and r in mw:
                            bad[r] = f'target {tw[r]}  ours {mw[r]}'
                        elif tws and r in tw and r not in mw:
                            bad[r] = f'target {tw[r]}  ours (no narrowing)'
                    if bad:
                        out.append((unit, fn, callee, i, bad))
                    continue
                if consts:
                    bad = {r: f'{s}  (target: {tw[r]})' for r, s in mw.items()
                           if s.startswith('li ') and r in tw and tw[r] != s and not tw[r].startswith('addi ')}
                    if bad:
                        out.append((unit, fn, callee, i, bad))
                    continue
                extra = {r: s for r, s in mw.items() if r not in tw}
                if strict:
                    # strong signals only: in-place narrowing (prototype width) or a constant the target never loads
                    extra = {r: s for r, s in extra.items()
                             if re.match(r'(clrlwi|extsb|extsh|rlwinm)\s+(\w+),\2,', s)
                             or (s.startswith('li ') and not any(v == s for v in tw.values()))}
                if extra:
                    out.append((unit, fn, callee, i, extra))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--min', type=float, default=80)
    ap.add_argument('--unit-filter', default='')
    ap.add_argument('--strict', action='store_true', help='only in-place narrowing or constants the target never loads')
    ap.add_argument('--consts', action='store_true', help='constants we pass where the target passes something else')
    ap.add_argument('--widths', action='store_true', help='argument narrowed to another width than the target before a call')
    a = ap.parse_args()
    global STRICT, CONSTS, WIDTHS
    WIDTHS = a.widths
    STRICT = a.strict
    CONSTS = a.consts
    rep = json.load(open(f'{ROOT}/build/{VER}/report.json'))
    todo = defaultdict(list)
    for u in rep['units']:
        name = u['name'].split('/', 1)[1] if '/' in u['name'] else u['name']
        if a.unit_filter not in name:
            continue
        for f in u.get('functions', []):
            p = f.get('fuzzy_match_percent', 0)
            if a.min <= p < 100:
                todo[name].append(f['name'])
    with ThreadPoolExecutor(8) as ex:
        results = list(ex.map(lambda kv: check(*kv), todo.items()))
    bycallee = defaultdict(list)
    for res in results:
        for unit, fn, callee, i, extra in res:
            bycallee[callee].append((unit, fn, i, extra))
    for callee, rows in sorted(bycallee.items(), key=lambda kv: -len(kv[1])):
        print(f'{callee}  ({len(rows)} call sites)')
        for unit, fn, i, extra in rows:
            print(f'    {unit}  {fn}  call#{i}  ' + ('widths: ' if WIDTHS else 'we/target: ' if CONSTS else 'only we set: ') + '; '.join(f'{r}: {s}' for r, s in sorted(extra.items())))


if __name__ == '__main__':
    main()
