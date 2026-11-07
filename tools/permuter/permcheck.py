"""permcheck.py - is a permuter setup faithful? Scores the unmodified base.c and prints the penalty breakdown.

usage: python tools/permuter/permcheck.py <run folder name or path> [--branch] [--permuter DIR] [--runs DIR]

Rules of thumb:
  * a base score of roughly 5 x (number of differing register lines) is faithful;
  * "Insertions"/"Deletions" far above the real instruction difference = a STALE setup (base.c made from older source
    or a surrogate that does not reproduce the function): re-run permsetup.py after every source change;
  * by default branch-target differences are ignored. With extra/missing instructions every later branch target would
    be off by one; pass --branch only when the instruction COUNT already matches;
  * base score 0 = the function is already exact in the current source: nothing to search.
Exit code: 0 = score printed, 1 = base.c did not compile/parse, 2 = suspicious setup (many insertions/deletions).
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pcommon as P   # noqa: E402


def check(run_dir, permuter, branch=False):
    score, out = P.base_score(run_dir, permuter, branch)
    pen = out[out.find('--------------- Penalty List'):] if '--------------- Penalty List' in out else ''
    m = re.search(r'Insertions:\s+(\d+).*?Deletions:\s+(\d+)', pen, re.S)
    suspicious = bool(m and int(m.group(1)) + int(m.group(2)) > 40)
    return score, (pen.strip() or out[-1500:]), suspicious


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('run')
    ap.add_argument('--branch', action='store_true')
    P.add_common_args(ap)
    a = ap.parse_args()
    run = a.run if os.path.isdir(a.run) else os.path.join(P.runs_dir(a.runs), P.safe(a.run))
    score, text, suspicious = check(run, P.permuter_dir(a.permuter), a.branch)
    print(text)
    if score is None:
        print('base.c did not compile or parse')
        sys.exit(1)
    print('base score = %d' % score)
    if suspicious:
        print('WARNING: many insertions/deletions - is base.c stale? re-run permsetup.py')
        sys.exit(2)


if __name__ == '__main__':
    main()
