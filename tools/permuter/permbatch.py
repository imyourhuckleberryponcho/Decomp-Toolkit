"""permbatch.py - run the permuter over a LIST of functions, several lanes in parallel, without collisions.

usage: python tools/permuter/permbatch.py (--list FILE | --funcs unit:symbol,unit:symbol ...)
                                          [--seconds 300] [--idle 240] [--lanes 2] [-j 4] [--tandem] [--regs]
                                          [--skip-done] [--stopfile F] [--permuter DIR] [--runs DIR]

  --list FILE    one function per line: `unit symbol` or `unit:symbol` (blank lines and # comments ignored)
  --lanes N      functions worked on at the same time (each lane runs its own permuter with -j threads)
  --tandem       use permtandem.py (focus + whole lanes re-seeding each other, -j is then the total) instead of one
                 plain permuter
  --skip-done    skip functions already recorded in <runs>/batch_done.json with a budget >= --seconds
  Claims: every function is claimed with a file in <runs>/_claims while it is worked on, so another batch (or a
  runner lane) never takes the same function; a claim of a dead process goes stale after 4 hours.

Per function: permsetup -> baseline check (a base that does not compile, or already scores 0, is skipped) ->
permrun / permtandem. Prints and appends to <runs>/batch_results.tsv one line per function:
    RESULT <unit> <symbol> <base> <best or NONE> <path of the best source.c or NONE>
Score = permuter penalty (0 = exact code). Afterwards run permcollect.py to get a review folder.
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pcommon as P   # noqa: E402

LOCK = threading.Lock()


def read_list(path):
    items = []
    for line in open(path, encoding='utf-8'):
        line = line.split('#', 1)[0].strip()
        if not line:
            continue
        parts = line.replace(':', ' ', 1).split() if ' ' not in line else line.split()
        if len(parts) >= 2:
            items.append((parts[0], parts[1]))
    return items


def work(unit, sym, a, permuter, rdir, results):
    import permrun
    import parsefix
    parsefix.PERMUTER_SRC = permuter
    cp = P.claim(os.path.join(rdir, '_claims'), sym)
    if not cp:
        return dict(unit=unit, sym=sym, base='NONE', best='NONE', path='claimed')
    try:
        if a.tandem:
            import permtandem
            r = permtandem.run(unit, sym, a.seconds, a.j, 15, a.idle, None, a.stopfile, False, permuter, a.runs, quiet=True)
            base, best, path = r.get('base'), r.get('best'), r.get('best_path')
        else:
            p = subprocess.run([sys.executable, os.path.join(P.HERE, 'permsetup.py'), unit, sym, '--runs', rdir]
                               + (['--regs'] if a.regs else []), capture_output=True, text=True, cwd=P.REPO)
            if p.returncode:
                return dict(unit=unit, sym=sym, base='NONE', best='NONE', path='setup failed: ' + (p.stderr or p.stdout).strip()[-150:])
            run_dir = os.path.join(rdir, P.safe(sym))
            parsefix.fix(run_dir)
            r = permrun.run(run_dir, permuter, a.seconds, a.j, a.idle, a.stopfile, None, True, cp)
            base, best, path = r.get('base'), r.get('best'), r.get('best_path')
            if r.get('error') and base is None:
                return dict(unit=unit, sym=sym, base='NONE', best='NONE', path=r['error'][:150])
        return dict(unit=unit, sym=sym, base='NONE' if base is None else base, best='NONE' if best is None else best,
                    path=path or 'NONE')
    except Exception as e:     # one bad function must not stop the batch
        return dict(unit=unit, sym=sym, base='NONE', best='NONE', path='crashed: %s' % e)
    finally:
        P.release(cp)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--list')
    ap.add_argument('--funcs')
    ap.add_argument('--seconds', type=int, default=300)
    ap.add_argument('--idle', type=int, default=240)
    ap.add_argument('--lanes', type=int, default=2)
    ap.add_argument('-j', type=int, default=4)
    ap.add_argument('--tandem', action='store_true')
    ap.add_argument('--regs', action='store_true')
    ap.add_argument('--skip-done', action='store_true')
    ap.add_argument('--stopfile')
    P.add_common_args(ap)
    a = ap.parse_args()
    permuter = P.permuter_dir(a.permuter)
    rdir = P.runs_dir(a.runs)
    items = read_list(a.list) if a.list else []
    if a.funcs:
        items += [tuple(x.split(':', 1)) for x in a.funcs.split(',') if ':' in x]
    if not items:
        sys.exit('nothing to do: give --list or --funcs')
    done_path = os.path.join(rdir, 'batch_done.json')
    try:
        done = json.load(open(done_path))
    except (OSError, ValueError):
        done = {}
    todo = [(u, s) for u, s in items if not (a.skip_done and done.get(s, 0) >= a.seconds)]
    queue = list(todo)
    tsv = os.path.join(rdir, 'batch_results.tsv')

    def lane():
        while True:
            with LOCK:
                if not queue or P.stop_requested(a.stopfile):
                    return
                unit, sym = queue.pop(0)
            r = work(unit, sym, a, permuter, rdir, None)
            line = 'RESULT %s %s %s %s %s' % (r['unit'], r['sym'], r['base'], r['best'], r['path'])
            with LOCK:
                print(line, flush=True)
                open(tsv, 'a', encoding='utf-8').write(time.strftime('%Y-%m-%d %H:%M:%S ') + line + '\n')
                if r['base'] != 'NONE':
                    done[sym] = a.seconds
                    P.write_json(done_path, done)

    threads = [threading.Thread(target=lane) for _ in range(max(1, a.lanes))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


if __name__ == '__main__':
    main()
