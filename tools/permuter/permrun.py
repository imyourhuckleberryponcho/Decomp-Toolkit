"""permrun.py - run decomp-permuter on a prepared run folder for a limited time, then kill it and list the best scores.

usage: python tools/permuter/permrun.py (UNIT SYMBOL | RUN) [--seconds 300] [-j 4] [--idle SEC]
                                        [--stopfile F] [--permuter DIR] [--runs DIR] [-- extra permuter args]

  --idle SEC    give up when no new best output appeared for SEC seconds (0 = use the full time)
  --stopfile F  stop early when this file exists (a runner uses it for a clean stop)
  extra args    replace the default `--stop-on-zero` (everything after `--` goes to permuter.py)

parsefix.py runs first (hides C++ statements the permuter's parser cannot read behind PERM_IGNORE). Prints one line:
    RESULT <unit> <symbol> <base score> <best score or NONE> <path of the best source.c or NONE>
Score = differing-ness as the permuter counts it (0 = exact code). Outputs are in <run>/output-<score>-<n>/source.c.
Treat every output as a HINT: rewrite it as natural source, then verify with fcheck (permcollect.py does the checks).
"""
import argparse
import glob
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pcommon as P   # noqa: E402


def run(run_dir, permuter, seconds=300, jobs=4, idle=0, stopfile=None, extra=None, quiet=False, claim_path=None):
    """-> dict(base, best, best_path, seconds). Does not claim; callers that need exclusivity use pcommon.claim."""
    base, text = P.base_score(run_dir, permuter)
    if base is None:
        return dict(base=None, best=None, best_path=None, error='base.c does not compile or parse: ' + text[-300:].strip())
    if base == 0:
        return dict(base=0, best=0, best_path=None, error='already exact (base score 0)')
    t0 = time.time()
    proc = P.start_permuter(run_dir, permuter, jobs, extra)
    last_best, t_best, seen = base, t0, set()
    try:
        while proc.poll() is None and time.time() - t0 < seconds and not P.stop_requested(stopfile):
            time.sleep(2)
            if claim_path:
                P.touch(claim_path)
            outs = P.outputs(run_dir)
            if outs and outs[0][0] < last_best:
                last_best, t_best = outs[0][0], time.time()
                if not quiet:
                    print('  best %d after %d s' % (last_best, time.time() - t0), flush=True)
            if idle and time.time() - t_best > idle:
                break
    finally:
        P.kill_tree(proc)
    outs = P.outputs(run_dir)
    best = outs[0] if outs else None
    return dict(base=base, best=best[0] if best else None, best_path=os.path.join(best[1], 'source.c') if best else None,
                seconds=round(time.time() - t0))


def main():
    argv = sys.argv[1:]
    extra = None
    if '--' in argv:
        i = argv.index('--')
        extra, argv = argv[i + 1:], argv[:i]
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('target', nargs='+', metavar='UNIT SYMBOL | RUN',
                    help='UNIT SYMBOL = set up fresh from the current source, then run; RUN = an existing run folder')
    ap.add_argument('--seconds', type=int, default=300)
    ap.add_argument('-j', type=int, default=4)
    ap.add_argument('--idle', type=int, default=0)
    ap.add_argument('--stopfile')
    ap.add_argument('--regs', action='store_true', help='register-biased weights (with UNIT SYMBOL)')
    ap.add_argument('--src', help='source file instead of src/<unit> (with UNIT SYMBOL)')
    P.add_common_args(ap)
    a = ap.parse_args(argv)
    permuter = P.permuter_dir(a.permuter)
    unit = sym = claim_path = None
    if len(a.target) >= 2:
        unit, sym = a.target[0], a.target[1]
        claim_path = P.claim(os.path.join(P.runs_dir(a.runs), '_claims'), sym)
        if not claim_path:
            print('RESULT %s %s NONE NONE NONE' % (unit, sym))
            print('claimed by another lane')
            return
        import permsetup
        run_dir = permsetup.setup(unit, sym, a.src, a.regs, False, '', 'auto', a.runs)
        import shutil
        for d in glob.glob(os.path.join(run_dir, 'output-*')):
            shutil.rmtree(d, ignore_errors=True)
    else:
        run_dir = a.target[0] if os.path.isdir(a.target[0]) else os.path.join(P.runs_dir(a.runs), P.safe(a.target[0]))
        meta = json.load(open(os.path.join(run_dir, 'perm_meta.json'), encoding='utf-8'))
        unit, sym = meta['unit'], meta['symbol']
    try:
        try:
            import parsefix
            parsefix.PERMUTER_SRC = permuter
            print('parsefix:', parsefix.fix(run_dir))
        except Exception as e:   # the run may still work without it
            print('parsefix crashed:', e)
        r = run(run_dir, permuter, a.seconds, a.j, a.idle, a.stopfile, extra, False, claim_path)
    finally:
        if claim_path:
            P.release(claim_path)
    if r.get('error') and r.get('base') is None:
        print(r['error'])
    print('RESULT %s %s %s %s %s' % (unit, sym, r['base'] if r['base'] is not None else 'NONE',
                                     r['best'] if r.get('best') is not None else 'NONE', r.get('best_path') or 'NONE'))


if __name__ == '__main__':
    main()
