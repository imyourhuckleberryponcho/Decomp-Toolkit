"""refmatch.py - find code of YOUR game that other decompilation projects already have, at every granularity.

usage (project root): python tools/refs/refmatch.py [--k 8] [--min-piece 12] [--common 12]
  needs the corpus from refcorpus.py and your original executable (config.yml / orig/<VER>/sys/main.dol / DECOMP_DOL)

Compares COMPILED reference functions with the game's machine code. Words that carry a relocation in the reference object
are masked to opcode + registers on both sides, so addresses and symbol names do not matter:
  exact   same length, every unmasked word identical        -> the reference source compiles to the game's code
  close   same length and >= 90% identical, or length within 15% and >= 85% similar
  piece   a run of >= --min-piece identical instructions inside otherwise different functions (inlined helpers, macros)
  file    >= 2 matched functions of ONE reference object in order at increasing addresses: (part of) the same file
Masking means "exact" does not prove which functions it CALLS; refport.py checks that against the game.
Very short functions match unrelated code: the `trivial` column marks them, never take names from them alone.

Output build/scratch/refmatch/: functions.tsv, pieces.tsv, files.tsv, SUMMARY.md, refmatch.json (for refport.py).
Changes nothing in src/.
"""
import argparse
import bisect
import difflib
import glob
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from refcommon import OBJ, OUT, U, VER, game_funcs, generic_mask, masked, read_obj, words   # noqa: E402

TRIVIAL = 4   # functions of <= 4 instructions match too much unrelated code to say anything about files


def load_corpus():
    """every reference function: dict(name, code, relocs, repo, src, ver, variant, cflags, done)"""
    funcs = []
    for man in sorted(glob.glob(os.path.join(OBJ, '*', 'manifest.json'))):
        repo = os.path.basename(os.path.dirname(man))
        try:
            files = U.load_json(man, {})['files']
        except KeyError:
            continue
        for e in files:
            if not e.get('ok') or not e.get('obj') or not os.path.exists(e['obj']):
                continue
            try:
                fs = read_obj(e['obj'])
            except Exception:
                continue
            for f in fs:
                if len(f['code']) < 8:
                    continue
                f.update(repo=repo, src=e['src'], ver=e['ver'], variant=e['variant'],
                         cflags=e['cflags'], done=e.get('done', False), objpath=e['obj'])
                funcs.append(f)
    return funcs


def game_status():
    """address -> (name, fuzzy %, unit) from report.json"""
    out = {}
    for u in U.load_json('build/%s/report.json' % VER, {'units': []})['units']:
        for f in u.get('functions', []):
            a = (f.get('metadata') or {}).get('virtual_address')
            if a:
                out[int(a)] = (f['name'], float(f.get('fuzzy_match_percent') or 0), U.unit_name(u['name']))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--k', type=int, default=8, help='instruction window for the candidate index')
    ap.add_argument('--min-piece', type=int, default=12, help='shortest reported piece, in instructions')
    ap.add_argument('--common', type=int, default=12, help='ignore windows found in more game functions than this')
    a = ap.parse_args()
    K = a.k

    ref = load_corpus()
    if not ref:
        sys.exit('no reference objects in %s: run refcorpus.py first' % OBJ)
    game = game_funcs()
    status = game_status()
    by_addr = {c['addr']: c for c in game}
    for c in game:
        c['w'] = words(c['code'])
        c['g'] = generic_mask(c['w'])
    print('reference functions: %d (from %d repos); game functions: %d' % (
        len(ref), len({f['repo'] for f in ref}), len(game)), flush=True)

    # window index over the game
    win = defaultdict(set)
    for c in game:
        g = c['g']
        for i in range(0, len(g) - K + 1):
            win[hash(g[i:i + K])].add((c['addr'], i))
    common = {h for h, occ in win.items() if len({x for x, _ in occ}) > a.common}

    # dedupe identical reference functions (same code in several repos/variants): keep all sources, compute once
    groups = defaultdict(list)
    for f in ref:
        f['w'] = words(f['code'])
        f['m'] = tuple(masked(f['w'], f['relocs']))
        groups[(f['m'], tuple(sorted(f['relocs'])))].append(f)
    print('unique reference functions: %d' % len(groups), flush=True)

    fmatch = defaultdict(list)    # address -> [(rank, score, kind, reffunc)]
    pieces = defaultdict(list)    # address -> [(len, offset, ref_off, reffunc)]
    for (mw, _), fs in groups.items():
        f = fs[0]
        g = generic_mask(f['w'])
        n = len(g)
        # candidates: game functions sharing an uncommon window, plus exact generic-mask equals
        hits = defaultdict(list)
        for i in range(0, n - K + 1):
            h = hash(g[i:i + K])
            if h in common:
                continue
            for addr, j in win.get(h, ()):
                hits[addr].append((i, j))
        if n < K:   # tiny functions: same length only
            for c in game:
                if len(c['w']) == n:
                    hits[c['addr']].append((0, 0))
        for addr, pairs in hits.items():
            c = by_addr[addr]
            cw = c['w']
            kind, score = None, 0.0
            if len(cw) == n:
                cm = masked(cw, f['relocs'])
                score = sum(x == y for x, y in zip(mw, cm)) / max(n, 1)
                if score == 1.0:
                    kind = 'exact'
                elif score >= 0.90:
                    kind = 'close'
            if kind is None and abs(len(cw) - n) <= max(2, n * 0.15) and n >= 6:
                sm = difflib.SequenceMatcher(None, g, c['g'], autojunk=False)
                if sm.real_quick_ratio() >= 0.85 and sm.quick_ratio() >= 0.85:
                    s = sm.ratio()
                    if s >= 0.85:
                        kind, score = 'close', s
            if kind:
                for ff in fs:
                    fmatch[addr].append((0 if kind == 'exact' else 1, -score, kind, ff))
                continue
            # pieces: extend window hits into maximal runs of identical generic-masked words
            seen = set()
            for i, j in sorted(pairs):
                if (i - j) in seen:
                    continue
                s0, t0 = i, j
                while s0 > 0 and t0 > 0 and g[s0 - 1] == c['g'][t0 - 1]:
                    s0, t0 = s0 - 1, t0 - 1
                s1, t1 = i + K, j + K
                while s1 < n and t1 < len(cw) and g[s1] == c['g'][t1]:
                    s1, t1 = s1 + 1, t1 + 1
                L = s1 - s0
                if L >= a.min_piece:
                    seen.add(i - j)
                    pieces[addr].append((L, t0, s0, f))

    os.makedirs(OUT, exist_ok=True)
    rank_ver = lambda ff: (0 if ff['done'] else 1, 0 if ff['variant'] == 'own' else 1)
    best = {}
    for addr, lst in fmatch.items():
        lst.sort(key=lambda t: (t[0], t[1], rank_ver(t[3])))
        best[addr] = lst

    def cstat(addr):
        return status.get(addr, (by_addr[addr]['name'], 0.0, '?'))

    # functions.tsv
    rows = []
    for addr in sorted(best):
        lst = best[addr]
        _, negs, kind, ff = lst[0]
        nm, pct, unit = cstat(addr)
        alts = len({(x[3]['repo'], x[3]['src']) for x in lst})
        rows.append((addr, nm, unit, pct, len(by_addr[addr]['code']), kind, -negs, ff, alts))
    with open(os.path.join(OUT, 'functions.tsv'), 'w', encoding='utf-8') as fo:
        fo.write('addr\tname\tunit\tpercent\tsize\tkind\tscore\ttrivial\tref_repo\tref_src\tref_func\tref_ver\tvariant\tn_sources\n')
        for addr, nm, unit, pct, size, kind, sc, ff, alts in rows:
            fo.write('%08X\t%s\t%s\t%.2f\t%d\t%s\t%.3f\t%d\t%s\t%s\t%s\t%s\t%s\t%d\n' % (
                addr, nm, unit, pct, size, kind, sc, size <= TRIVIAL * 4, ff['repo'], ff['src'], ff['name'], ff['ver'],
                ff['variant'], alts))

    # pieces.tsv (only for functions without a whole-function match; longest per function first)
    prow = []
    for addr, lst in pieces.items():
        if addr in best:
            continue
        lst.sort(key=lambda t: -t[0])
        keep, used = [], []
        for L, co, ro, ff in lst:
            if any(co < u1 and co + L > u0 for u0, u1 in used):
                continue
            used.append((co, co + L))
            keep.append((L, co, ro, ff))
        for L, co, ro, ff in keep[:5]:
            nm, pct, unit = cstat(addr)
            prow.append((addr, nm, unit, pct, len(by_addr[addr]['code']) // 4, co, L, ff))
    prow.sort(key=lambda r: (-r[6], r[0]))
    with open(os.path.join(OUT, 'pieces.tsv'), 'w', encoding='utf-8') as fo:
        fo.write('addr\tname\tunit\tpercent\tn_instr\toffset\tpiece_len\tref_repo\tref_src\tref_func\tref_ver\n')
        for addr, nm, unit, pct, ni, co, L, ff in prow:
            fo.write('%08X\t%s\t%s\t%.2f\t%d\t0x%X\t%d\t%s\t%s\t%s\t%s\n' % (
                addr, nm, unit, pct, ni, co * 4, L, ff['repo'], ff['src'], ff['name'], ff['ver']))

    # files.tsv: a reference object whose NON-TRIVIAL matched functions appear in the same order at game addresses
    # (or reversed: C with -inline deferred emits functions in reverse) within a compact range = a shared TU (part)
    def lis(seq):
        """longest strictly increasing subsequence of (key, item) by key -> items"""
        tails, idx, prev = [], [], [None] * len(seq)
        for i, (k, _) in enumerate(seq):
            j = bisect.bisect_left(tails, k)
            if j == len(tails):
                tails.append(k)
                idx.append(i)
            else:
                tails[j] = k
                idx[j] = i
            prev[i] = idx[j - 1] if j else None
        out, i = [], idx[-1] if idx else None
        while i is not None:
            out.append(seq[i][1])
            i = prev[i]
        return out[::-1]

    obj_funcs = defaultdict(list)
    for f in ref:
        obj_funcs[f['objpath']].append(f)
    per_obj = defaultdict(dict)    # objpath -> ref func name -> (address, kind)
    for addr, lst in best.items():
        if len(by_addr[addr]['code']) <= TRIVIAL * 4:
            continue
        for _, negs, kind, ff in lst:
            per_obj[ff['objpath']].setdefault(ff['name'], (addr, kind))
    frows = []
    for objp, hits in per_obj.items():
        if len(hits) < 2:
            continue
        fs = sorted(obj_funcs[objp], key=lambda f: f.get('off', 0))
        order = [(hits[f['name']][0], (f['name'],) + hits[f['name']]) for f in fs if f['name'] in hits]
        fwd = lis(order)
        rev = lis([(-k, v) for k, v in order])
        run, direction = (fwd, 'fwd') if len(fwd) >= len(rev) else (rev, 'rev')
        if len(run) < 2:
            continue
        addrs = sorted(r[1] for r in run)
        span = addrs[-1] + len(by_addr[addrs[-1]]['code']) - addrs[0]
        msize = sum(len(by_addr[x]['code']) for x in addrs)
        if span > 4 * msize + 0x400:    # scattered small hits, not one TU
            continue
        ff = fs[0]
        frows.append((len(run), len(fs), addrs[0], addrs[-1] + len(by_addr[addrs[-1]]['code']), ff,
                      sum(1 for r in run if r[2] == 'exact'), direction, msize, span))
    bestf = {}
    for r in frows:    # the same file in several repos/variants: keep the best
        key = (r[2], r[3])
        if key not in bestf or (r[5], r[0]) > (bestf[key][5], bestf[key][0]):
            bestf[key] = r
    frows = sorted(bestf.values(), key=lambda r: (-r[7], r[2]))
    with open(os.path.join(OUT, 'files.tsv'), 'w', encoding='utf-8') as fo:
        fo.write('\t'.join(['in_order', 'ref_funcs', 'exact', 'order', 'start', 'end', 'matched_bytes',
                            'ref_repo', 'ref_src', 'ref_ver', 'variant']) + '\n')
        for n, tot, lo, hi, ff, ex, d, msize, span in frows:
            fo.write('\t'.join(['%d', '%d', '%d', '%s', '%08X', '%08X', '%d', '%s', '%s', '%s', '%s']) % (
                n, tot, ex, d, lo, hi, msize, ff['repo'], ff['src'], ff['ver'], ff['variant']) + '\n')

    # refmatch.json for refport.py: every game function with a whole-function match, ALL candidate sources
    # (exact and close, every repo/variant/object) so the importer can find runs of neighbours from one object
    port = []
    for addr, nm, unit, pct, size, kind, sc, ff, alts in rows:
        srcs, seen = [], set()
        for _, negs, k2, x in best[addr]:
            key = (x['objpath'], x['name'])
            if key in seen:
                continue
            seen.add(key)
            srcs.append({'repo': x['repo'], 'src': x['src'], 'func': x['name'], 'ver': x['ver'], 'kind': k2,
                         'variant': x['variant'], 'cflags': x['cflags'], 'score': -negs, 'obj': x['objpath'],
                         'done': x['done']})
        port.append({'addr': '%08X' % addr, 'name': nm, 'unit': unit, 'pct': pct, 'size': size, 'kind': kind,
                     'sources': srcs})
    U.save_json(os.path.join(OUT, 'refmatch.json'), port, indent=0)

    # summary
    todo = lambda r: r[3] < 100
    ex = [r for r in rows if r[5] == 'exact']
    cl = [r for r in rows if r[5] == 'close']
    pf = {r[0] for r in prow}
    lines = ['# refmatch: code found in other projects', '',
             'reference: %d functions (%d unique) from %d repos; game: %d functions' % (
                 len(ref), len(groups), len({f['repo'] for f in ref}), len(game)), '',
             '| match | functions | not yet exact here | bytes not yet exact |', '|---|---|---|---|',
             '| exact | %d | %d | %d |' % (len(ex), sum(map(todo, ex)), sum(r[4] for r in ex if todo(r))),
             '| exact, > %d instructions | %d | %d | %d |' % (TRIVIAL, sum(1 for r in ex if r[4] > TRIVIAL * 4),
                                                         sum(1 for r in ex if todo(r) and r[4] > TRIVIAL * 4),
                                                         sum(r[4] for r in ex if todo(r) and r[4] > TRIVIAL * 4)),
             '| close | %d | %d | %d |' % (len(cl), sum(map(todo, cl)), sum(r[4] for r in cl if todo(r))),
             '| pieces only | %d | %d | - |' % (len(pf), sum(1 for x in pf if cstat(x)[1] < 100)),
             '', '## by reference repo (exact, not yet exact here)', '']
    byrepo = defaultdict(int)
    for r in ex:
        if todo(r):
            byrepo[r[7]['repo']] += 1
    for k, v in sorted(byrepo.items(), key=lambda kv: -kv[1]):
        lines.append('- %s: %d' % (k, v))
    lines += ['', '## files (reference objects that map onto an address range; top 40)', '']
    for n, tot, lo, hi, ff, exn, d, msize, span in frows[:40]:
        lines.append('- %s %s: %d/%d functions in order (%d exact, %s, %d B) at %08X-%08X (%s, %s)' % (
            ff['repo'], ff['src'], n, tot, exn, d, msize, lo, hi, ff['ver'], ff['variant']))
    with open(os.path.join(OUT, 'SUMMARY.md'), 'w', encoding='utf-8') as fo:
        fo.write('\n'.join(lines) + '\n')
    print('\n'.join(lines[:12]))
    print('-> %s/' % OUT)


if __name__ == '__main__':
    main()
