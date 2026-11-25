"""argrepair.py - FIXES what argextra.py --strict / --consts only report: wrong literal call arguments.

usage (project root): python tools/argrepair.py [--only UNIT] [--min 70] [--dry] [--fresh] [-j 6]

A draft passes a literal (`f((void *) 0)`, `f(1)`) where the original passes a value that is ALREADY in the argument
register (nearly always the caller's own parameter in that slot), another constant, or a field. Random search "solves"
these with an uninitialised local, which nobody should install; the real fix is the natural one.

For each flagged call the literal is replaced by candidates in turn: the target's own constant, the parameter that
arrived in that register, the other parameters, the m2c locals and the M2C_FIELD(...) expressions of the function. A
candidate is KEPT (src/ is edited, the previous text is in git) only if the function's score rises, no other function
of the unit drops, no new CALLS / CONST problem appears, AND either
  - the function becomes exact (unitlib.strict_exact: identical instructions, so identical behaviour), or
  - the value is provably right: the constant the target loads, or the parameter that arrived in the register with no
    call before it that could clobber it and a callee that really reads the register; and it must leave FEWER
    flagged argument registers than before (swapping constants between two calls can score +0.01 and fix nothing).
Better-but-unproven candidates of several call sites are tried TOGETHER, kept only if that makes the function exact.
Functions already tried on identical source are skipped (build/scratch/argrepair/tried.json; --fresh ignores it).
Limits: C symbol names (no mangled C++ methods); integer arguments in registers.
Log: build/scratch/argrepair/argrepair.log. --dry writes nothing to src/.
"""
import argparse
import hashlib
import os
import re
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import unitlib as U    # noqa: E402
if not any(x in ('-h', '--help') for x in sys.argv[1:]):
    import argextra    # noqa: E402

OUT = 'build/scratch/argrepair'
CACHE_F = OUT + '/tried.json'
log = U.Log(OUT + '/argrepair.log')
CALL_RE = re.compile(r'\b(?!if\b|while\b|for\b|switch\b|return\b|sizeof\b)[A-Za-z_]\w*\s*\(')
LIT = re.compile(r'^\s*(?:\(\s*([\w\s\*]+?)\s*\)\s*)?\(?\s*(-?(?:0x[0-9a-fA-F]+|\d+))[uUlL]*\s*\)?\s*$')


def mismatches(unit, fn, obj):
    """argument registers argextra still flags in fn for a compiled object"""
    if not obj:
        return 99
    try:
        return sum(len(r[4]) for mode in ('strict', 'consts') for r in argextra.check(unit, [fn], mobj=obj, mode=mode))
    except Exception:
        return 99


def lit_match(arg_text, k):
    """the literal's cast (or None) if arg_text is a literal equal to k as a 16-bit li immediate, else False"""
    m = LIT.match(arg_text)
    if not m:
        return False
    v = int(m.group(2), 0)
    if v != k and (v & 0xFFFF) != (k & 0xFFFF):
        return False
    return m.group(1)


_UNIT_OF, _DIS = {}, {}


def reads_reg(callee, reg):
    """True if the callee (original code) reads reg before writing it, False if it does not, None if unknown.
    Straight-line scan: it stops at the first call or return."""
    with U.LOCK:
        if not _UNIT_OF:
            _UNIT_OF.update({f: v[0] for f, v in U.report().items()})
        unit = _UNIT_OF.get(callee)
        if not unit:
            return None
        if unit not in _DIS:
            _DIS[unit] = argextra.disasm('build/%s/obj/%s.o' % (U.VER, unit))
    ins = _DIS[unit].get(callee)
    if not ins:
        return None
    for op, args, rel, lab in ins:
        ops = [re.sub(r'^.*\((r\d+)\)$', r'\1', x.strip()) for x in args.split(',') if x.strip()]
        if op in ('blr', 'blr+', 'blr-'):
            return False
        if op == 'bl' or op.startswith('bctr'):
            return None
        writes = not op.startswith(('st', 'cmp', 'b', 'mt', 'tw', 'dcb', 'icb', 'sync', 'isync'))
        if reg in (ops[1:] if writes else ops):
            return True
        if writes and ops and ops[0] == reg:
            return False
    return None


def sites_of(text, fn, callee, idx, reg, k):
    """[(call index, argument index, cast)] of a literal k passed to callee in fn, best guess first"""
    sp = U.func_span(text, fn)
    if not sp:
        return []
    body = text[sp[1]:sp[2]]
    cs = U.calls_in(body, callee)
    slot = int(reg[1:]) - 3
    # only the argument in the register's own slot: wandering to other arguments with the same literal makes
    # ping-pong edits (1 -> 0 -> 1) that gain 0.01 by noise
    for ci in ([idx] if idx < len(cs) else []) + [i for i in range(len(cs)) if i != idx]:
        if slot < len(cs[ci]):
            a, b = cs[ci][slot]
            c = lit_match(body[a:b], k)
            if c is not False:
                return [(ci, slot, c)]
    return []


def edit(text, fn, callee, ci, ai, rep):
    """text with argument ai of the ci-th call of callee in fn replaced by rep (or None)"""
    sp = U.func_span(text, fn)
    if not sp:
        return None
    _, b0, b1, _ = sp
    body = text[b0:b1]
    cs = U.calls_in(body, callee)
    if ci >= len(cs) or ai >= len(cs[ci]):
        return None
    a, b = cs[ci][ai]
    return text[:b0] + body[:a] + (' ' if body[a:a + 1] == ' ' else '') + rep + body[b:] + text[b1:]


def repair_unit(unit, rows, dry, cache):
    src = U.source_of(unit)
    if not src:
        return []
    tag = 'argrep_' + re.sub(r'\W', '_', unit)[-60:]
    tmp = '%s/%s/%s' % (OUT, tag, os.path.basename(src))
    os.makedirs(os.path.dirname(tmp), exist_ok=True)
    text = U.read_source(src)
    base, basebad, baseobj = U.scores(unit, None, tag)
    if not base:
        log('  %s: base compile failed, skipped' % unit)
        return []
    shutil.copy(baseobj, tmp + '.base.o')    # fcheck reuses one object path per tag
    baseobj = tmp + '.base.o'
    mis, kept = {}, []

    def fnhash(fn):
        sp = U.func_span(text, fn)
        return hashlib.md5(text[sp[0]:sp[2]].encode('utf-8', 'surrogateescape')).hexdigest()[:12] if sp else ''

    def trial(new, fn):
        U.write_source(tmp, new)
        after, bad, obj = U.scores(unit, tmp, tag)
        if not after:
            return None
        return after, bad, not U.no_worse(base, after, fn) and not (bad - basebad), obj

    def accept(new, after, bad, desc, fn, m=None):
        nonlocal text, base, basebad
        if m is not None:
            mis[fn] = m
        kept.append('%s %s: %s  %.2f -> %.2f' % (unit, fn, desc, base.get(fn, 0), after[fn]))
        log('  kept   ' + kept[-1])
        text, base, basebad = new, after, bad
        if not dry:
            U.write_source(src, text)

    byfn = {}
    for row in rows:
        byfn.setdefault(row[0], []).append(row)
    for fn, frows in byfn.items():
        mis[fn] = mismatches(unit, fn, baseobj)
        key = unit + ':' + fn
        if cache.get(key) == fnhash(fn):
            continue   # tried on this exact source already
        unproven = []
        for _, callee, idx, extra, mode in frows:
            for reg, ins in sorted(extra.items()):
                m = re.match(r'li\s+(r\d+),\s*(-?\d+)', ins)
                if not m or m.group(1) != reg:
                    continue
                k = int(m.group(2))
                tk = re.search(r'target: li\s+%s,\s*(-?\d+)' % reg, ins)
                target_k = int(tk.group(1)) if tk else None
                for ci, ai, cast in sites_of(text, fn, callee, idx, reg, k):
                    sp = U.func_span(text, fn)
                    body, plist = text[sp[1]:sp[2]], sp[3]
                    a = U.calls_in(body, callee)[ci][ai][0]
                    before_call = bool(CALL_RE.search(body[:a].rsplit(callee, 1)[0]))
                    best = None
                    for rep, kind in U.value_pool(body, plist, reg, cast, target_k):
                        if mode == 'consts' and kind == 'slot':
                            kind = 'pool'   # the target sets this register from something else: not the parameter
                        new = edit(text, fn, callee, ci, ai, rep)
                        r = new and trial(new, fn)
                        if not r or not r[2]:
                            continue
                        after, bad, _, obj = r
                        gain = after.get(fn, 0) - base.get(fn, 0)
                        if gain <= 1e-9:
                            continue
                        exact = after.get(fn, 0) >= 100 - 1e-9 and U.strict_exact(unit, fn, obj)
                        safe = kind == 'const' or (kind == 'slot' and not before_call and reads_reg(callee, reg))
                        m_after = None
                        if safe and not exact:
                            m_after = mismatches(unit, fn, obj)
                            safe = m_after < mis[fn]
                        if exact or safe:
                            accept(new, after, bad, '%s(..%s..) %s -> %s' % (callee, reg, k, rep), fn, m_after)
                            best = None
                            break
                        if not best or gain > best[0]:
                            best = (gain, (callee, ci, ai), rep)
                    if best:
                        unproven.append(best)
        # several call sites may have to change TOGETHER to reach exact
        if unproven and base.get(fn, 0) < 100 and len(unproven) <= 8:
            new = text
            for _, (callee, ci, ai), rep in unproven:
                new = new and edit(new, fn, callee, ci, ai, rep)
            r = new and trial(new, fn)
            if r and r[2] and r[0].get(fn, 0) >= 100 - 1e-9 and U.strict_exact(unit, fn, r[3]):
                accept(new, r[0], r[1], ('combined %d sites: %s' % (len(unproven), ', '.join(x[2] for x in unproven)))[:200], fn)
        cache[key] = fnhash(fn)
    return kept


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--only', default='', help='only units whose name contains this')
    ap.add_argument('--min', type=float, default=70)
    ap.add_argument('--dry', action='store_true')
    ap.add_argument('--fresh', action='store_true', help='ignore the cache of functions already tried')
    ap.add_argument('-j', type=int, default=6)
    a = ap.parse_args()
    cache = {} if a.fresh else U.load_json(CACHE_F, {})
    todo = {}
    for name, fs in U.units().items():
        if a.only in name:
            fns = [f['name'] for f in fs if a.min <= (f.get('fuzzy_match_percent') or 0) < 100]
            if fns:
                todo[name] = fns
    byunit = {}
    for mode in ('strict', 'consts'):
        with ThreadPoolExecutor(8) as ex:
            for res in ex.map(lambda kv: argextra.check(kv[0], kv[1], mode=mode), todo.items()):
                for unit, fn, callee, i, extra in res:
                    byunit.setdefault(unit, []).append((fn, callee, i, extra, mode))
    n = sum(len(v) for v in byunit.values())
    log('argrepair: %d flagged call sites in %d units%s' % (n, len(byunit), ' (dry)' if a.dry else ''))
    with ThreadPoolExecutor(a.j) as ex:
        kept = sum(ex.map(lambda kv: repair_unit(kv[0], kv[1], a.dry, cache), byunit.items()), [])
    if not a.dry:
        U.save_json(CACHE_F, cache, indent=0)
    log('argrepair: kept %d fixes (%d functions to 100%%) from %d flagged call sites; now run ninja' % (
        len(kept), sum('-> 100.00' in k for k in kept), n))


if __name__ == '__main__':
    main()
