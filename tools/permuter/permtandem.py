"""permtandem.py - TANDEM permuter: a narrowed "focus" lane and a "whole function" lane that re-seed each other.

Idea: a permuter limited to a few lines (declarations + the statements that were just edited) and one that may edit
the whole function each have blind spots. Run both on one function at the same time. Every --check seconds the best
new, non-junk output of either lane is looked at; when it beats the best so far BOTH lanes restart from it:
  whole lane: the improved function, whole body as one PERM_RANDOMIZE region (CLEAN weights)
  focus lane: the improved function with PERM_RANDOMIZE only around the declaration block and the statements that
              differ from the previous best (= the edit just found); register-biased weights (--regs style)
Outputs that fail the behaviour guard (guard.py: changed constants, swapped arguments, volatile/goto, ...) are logged
and never shared. The search stops on a score of 0, after --idle seconds without a new best, at --seconds, or when
--stopfile appears.

usage: python tools/permuter/permtandem.py <unit> <symbol> [--seconds 600] [-j 8] [--check 15] [--idle 360]
                                           [--src FILE] [--stopfile F] [--no-claim] [--permuter DIR] [--runs DIR]
Output: <runs>/<symbol>__tandem/{log.txt, best/source.c, best/score.txt, result.json}; lane folders <symbol>__tfocus and
<symbol>__twhole. Last stdout line:  RESULT <unit> <symbol> <base> <best or NONE> <path of best source.c or NONE>
"""
import argparse
import difflib
import json
import os
import re
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pcommon as P   # noqa: E402
import permsetup      # noqa: E402
import guard          # noqa: E402

DECL = re.compile(r'^\s*(?:(?:const|static|register|unsigned|signed|struct|volatile)\s+)*[A-Za-z_][\w:<>]*\s*[\*&\s]+'
                  r'[A-Za-z_]\w*\s*(?:\[[^\]]*\]\s*)*(?:=[^;]*)?;\s*$')
NL = chr(10)
RAND_OPEN = 'PERM_RANDOMIZE(' + NL
NOT_DECL = re.compile(r'^\s*(?:return|goto|break|continue|else|delete|case|default|typedef)\b')


def func_span(text, name):
    """(start, end) offsets of the definition of `name` (header at column 0 through the closing brace) or None"""
    for m in re.finditer(r'^[^\n;{}#]*\b%s\s*\([^;{]*\)\s*(?:const\s*)?\{' % re.escape(name), text, re.M):
        if re.match(r'\s*(if|while|for|switch|return|else)\b', m.group(0)):
            continue
        j = permsetup.match_brace(text, m.end() - 1)
        if j > 0:
            return m.start(), j + 1
    return None


def split_base(text):
    """base.c -> (before incl. 'PERM_RANDOMIZE(\\n', function text, after)"""
    k = text.find('PERM_RANDOMIZE(\n')
    if k < 0:
        return None
    k += len('PERM_RANDOMIZE(\n')
    j = text.find('{', k)
    end = permsetup.match_brace(text, j) + 1
    return text[:k], text[k:end], text[end:]


def mark_lines(lines, regions):
    for a, b in sorted(regions, reverse=True):
        lines[a] = 'PERM_RANDOMIZE(' + lines[a]
        lines[b] = lines[b] + ')'
    return lines


def focus_regions(func, prev_func):
    """line ranges to randomize in the focus lane: declaration block + statements new compared with prev_func"""
    lines = func.split('\n')
    regions, open_i = [], next((i for i, l in enumerate(lines) if '{' in l), 0)
    i = open_i + 1
    while i < len(lines) and (DECL.match(lines[i]) and not NOT_DECL.match(lines[i]) or not lines[i].strip()):
        i += 1
    if i - 1 > open_i and any(l.strip() for l in lines[open_i + 1:i]):
        last = max(k for k in range(open_i + 1, i) if lines[k].strip())
        first = min(k for k in range(open_i + 1, i) if lines[k].strip())
        regions.append((first, last))
    old = {l.strip() for l in (prev_func or '').split('\n')}
    k = i
    while k < len(lines):
        t = lines[k].strip()
        if prev_func is not None and t and t not in old and t.endswith(';') and t.count('(') == t.count(')') \
                and not t.startswith(('for', 'while', 'if')):
            s = k
            while k + 1 < len(lines) and (lines[k + 1].strip() not in old and lines[k + 1].strip().endswith(';')
                                          and lines[k + 1].count('(') == lines[k + 1].count(')')):
                k += 1
            regions.append((s, k))
        k += 1
    return regions


class Lane:
    def __init__(self, name, run_dir, jobs):
        self.name, self.dir, self.jobs = name, run_dir, jobs
        self.proc = None
        self.seen = set()

    def start(self, permuter):
        self.proc = P.start_permuter(self.dir, permuter, self.jobs, ['--stop-on-zero', '--best-only'],
                                     os.path.join(self.dir, 'permuter.log'))

    def stop(self):
        if self.proc:
            P.kill_tree(self.proc)

    def new_outputs(self):
        res = []
        for sc, d in P.outputs(self.dir):
            if os.path.basename(d) not in self.seen:
                res.append((sc, d))
        return res

    def archive(self, n):
        for sc, d in P.outputs(self.dir):
            shutil.rmtree(d, ignore_errors=True)
        self.seen = set()


def write_base(lane, before, func, after, regions):
    """lane base.c = before + function (+ PERM_RANDOMIZE marks) + after. Whole body: before's own open
    `PERM_RANDOMIZE(` and the closing `)` that starts `after` wrap it; marked: those two are dropped instead."""
    if regions:
        marked = mark_lines(func.split(NL), regions)
        text = before[:-len(RAND_OPEN)] + NL.join(marked) + after[2:]
    else:
        text = before + func + after
    with open(os.path.join(lane.dir, 'base.c'), 'w', encoding='utf-8') as f:
        f.write(text)


def run(unit, sym, seconds=600, jobs=8, check=15, idle=360, src=None, stopfile=None, claim_func=True,
        permuter=None, runs=None, quiet=False):
    permuter = P.permuter_dir(permuter)
    rdir = P.runs_dir(runs)
    name = P.safe(sym)
    tdir = os.path.join(rdir, name + '__tandem')
    shutil.rmtree(tdir, ignore_errors=True)
    os.makedirs(tdir)
    logf = open(os.path.join(tdir, 'log.txt'), 'w', encoding='utf-8')

    def log(msg):
        line = time.strftime('%H:%M:%S ') + msg
        logf.write(line + '\n')
        logf.flush()
        if not quiet:
            print(line, flush=True)

    claim_p = None
    if claim_func:
        claim_p = P.claim(os.path.join(rdir, '_claims'), sym)
        if not claim_p:
            log('claimed by another lane - skipped')
            return dict(unit=unit, func=sym, base=None, best=None, best_path=None, skipped=True)
    result = dict(unit=unit, func=sym, base=None, best=None, best_path=None)
    lanes = []
    try:
        wdir = permsetup.setup(unit, sym, src, False, False, '__twhole', 'auto', runs)
        fdir = permsetup.setup(unit, sym, src, True, False, '__tfocus', 'auto', runs)
        wtext = P.read(os.path.join(wdir, 'base.c'))
        parts = split_base(wtext)
        if not parts:
            log('SETUP FAILED: no PERM_RANDOMIZE region in base.c')
            return result
        before, func0, after = parts
        open(os.path.join(tdir, 'orig_function.c'), 'w', encoding='utf-8').write(func0)
        fname = P.run_dir_function(wdir)
        import parsefix
        parsefix.PERMUTER_SRC = permuter
        log('parsefix: %s' % parsefix.fix(wdir))
        base, text = P.base_score(wdir, permuter)
        if base is None:
            log('base does not compile: ' + text[-300:].strip())
            return result
        result['base'] = base
        if base == 0:
            log('ALREADY EXACT: base score 0 - skipped')
            result.update(best=0)
            return result
        regions0 = focus_regions(func0, None)
        flane = Lane('focus', fdir, max(1, jobs // 2)) if regions0 else None
        wlane = Lane('whole', wdir, jobs if not flane else max(1, jobs - jobs // 2))
        lanes = [l for l in (flane, wlane) if l]
        if flane:
            write_base(flane, before, func0, after, regions0)
            fb, _ = P.base_score(fdir, permuter)
            if fb is None:
                log('focus lane base does not compile - whole lane only')
                lanes.remove(flane)
                flane = None
                wlane.jobs = jobs
        log('base score %s; lanes: %s; %d s, check every %d s' % (
            base, ', '.join('%s(-j%d)' % (l.name, l.jobs) for l in lanes), seconds, check))
        best, best_func, t_best, finds = base, func0, 0, []
        t0 = time.time()
        end = t0 + seconds
        for l in lanes:
            l.start(permuter)
        while time.time() < end and not P.stop_requested(stopfile):
            time.sleep(check)
            if claim_p:
                P.touch(claim_p)
            if idle and time.time() - (t0 + t_best) > idle:
                log('no new best for %d s - stopping early' % idle)
                break
            cand = []
            for l in lanes:
                for sc, d in l.new_outputs():
                    l.seen.add(os.path.basename(d))
                    cand.append((sc, l.name, d))
            if not cand:
                if all(l.proc.poll() is not None for l in lanes):
                    log('both lanes ended')
                    break
                continue
            pick = None
            for sc, lane, d in sorted(cand):
                if sc >= best:
                    break
                new_text = P.read(os.path.join(d, 'source.c'))
                sp = func_span(new_text, fname)
                if not sp:
                    continue
                nf = new_text[sp[0]:sp[1]]
                bad = guard.reasons(best_func, nf)
                if bad:
                    log('  ignored a %d from the %s lane: %s' % (sc, lane, '; '.join(bad)[:160]))
                    continue
                pick = (sc, lane, d, nf)
                break
            if not pick:
                continue
            sc, lane, d, nf = pick
            prev, best, best_func, t_best = best_func, sc, nf, time.time() - t0
            finds.append(dict(t=round(t_best), lane=lane, score=sc))
            os.makedirs(os.path.join(tdir, 'best'), exist_ok=True)
            shutil.copy(os.path.join(d, 'source.c'), os.path.join(tdir, 'best', 'source.c'))
            open(os.path.join(tdir, 'best', 'score.txt'), 'w').write('%d (%s lane, %d s)\n' % (sc, lane, t_best))
            result['best'], result['best_path'] = sc, os.path.join(tdir, 'best', 'source.c')
            log('NEW BEST %d from the %s lane' % (sc, lane))
            if sc == 0:
                log('EXACT (score 0) - stopping')
                break
            if end - time.time() < 2 * check:
                continue
            for l in lanes:
                l.stop()
            for l in lanes:
                regions = focus_regions(nf, prev) if l is flane else None
                bp = os.path.join(l.dir, 'base.c')
                old = P.read(bp)
                write_base(l, before, nf, after, regions)
                got, _ = P.base_score(l.dir, permuter)
                if got is None and regions:     # focus marking broke it: whole-body marking instead
                    write_base(l, before, nf, after, None)
                    got, _ = P.base_score(l.dir, permuter)
                if got is None:
                    open(bp, 'w', encoding='utf-8').write(old)
                    log('  %s lane: new source does not compile there - keeps its old base' % l.name)
                else:
                    log('  %s lane: re-seeded, checks at score %d' % (l.name, got))
                l.archive(0)
                l.start(permuter)
            log('  both lanes re-seeded from %d (%d s left)' % (sc, end - time.time()))
        result.update(finds=finds, seconds=round(time.time() - t0), seconds_to_best=round(t_best))
        log('DONE: base %s -> best %s' % (base, result['best']))
    finally:
        for l in lanes:
            l.stop()
        P.release(claim_p) if claim_p else None
        json.dump(result, open(os.path.join(tdir, 'result.json'), 'w'), indent=1)
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('unit')
    ap.add_argument('symbol')
    ap.add_argument('--seconds', type=int, default=600)
    ap.add_argument('-j', type=int, default=8)
    ap.add_argument('--check', type=int, default=15)
    ap.add_argument('--idle', type=int, default=360)
    ap.add_argument('--src')
    ap.add_argument('--stopfile')
    ap.add_argument('--no-claim', action='store_true', help='the caller already holds the claim for this function')
    P.add_common_args(ap)
    a = ap.parse_args()
    r = run(a.unit, a.symbol, a.seconds, a.j, a.check, a.idle, a.src, a.stopfile, not a.no_claim, a.permuter, a.runs)
    print('RESULT %s %s %s %s %s' % (a.unit, a.symbol, r['base'] if r.get('base') is not None else 'NONE',
                                      r['best'] if r.get('best') is not None else 'NONE', r.get('best_path') or 'NONE'))


if __name__ == '__main__':
    main()
