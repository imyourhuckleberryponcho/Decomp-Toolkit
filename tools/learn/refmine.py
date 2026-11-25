"""refmine.py - mine matching edits from OTHER projects' git histories (trickminer.py on every mirror).

usage (project root): python tools/learn/refmine.py [--fetch] [-j 6] [--max-commits 30000]

A young project (or one whose history is automatic commits) has little to learn from. Other projects built with the same
compiler family hold tens of thousands of small human edits that made a function match.
For every bare mirror in $DECOMP_REFS/history/*.git (the same mirrors refcorpus.py uses; they must be FULL clones, a
shallow clone has no history) this runs trickminer.py --matching-from-configure -> build/scratch/learn/ref_<name>.json.
A mirror is mined again only when its HEAD moved. --fetch updates the mirrors first. Then run fixrules.py build.
"""
import argparse
import glob
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'refs'))
from refcommon import HIST, U   # noqa: E402

OUT = 'build/scratch/learn'
HEADS = OUT + '/mined_heads.json'   # must not be named ref_*.json: that pattern is the catalogues
HERE = os.path.dirname(os.path.abspath(__file__))
VERSION = 1   # bump to mine everything again after a trickminer change


def git(repo, *args, timeout=600):
    try:
        return subprocess.run(['git', '-C', repo] + list(args), capture_output=True, text=True, encoding='utf-8',
                              errors='replace', timeout=timeout).stdout.strip()
    except subprocess.TimeoutExpired:
        return ''


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--fetch', action='store_true')
    ap.add_argument('--max-commits', type=int, default=30000)
    ap.add_argument('-j', type=int, default=6)
    a = ap.parse_args()
    heads = U.load_json(HEADS, {})
    repos = sorted(glob.glob(os.path.join(HIST, '*.git')))
    if not repos:
        sys.exit('no mirrors in %s (git clone --bare ...)' % HIST)
    if a.fetch:
        with ThreadPoolExecutor(a.j) as ex:
            list(ex.map(lambda r: git(r, 'fetch', '-q', 'origin', '+refs/heads/*:refs/heads/*', timeout=900), repos))

    def one(repo):
        name = os.path.basename(repo)[:-4]
        tag = 'ref_' + name
        head = git(repo, 'rev-parse', 'HEAD')
        stamp = '%s:%d' % (head, VERSION)
        if not head:
            return name, 'no HEAD'
        if heads.get(name) == stamp and os.path.exists('%s/%s.json' % (OUT, tag)):
            return name, 'unchanged'
        try:
            r = subprocess.run([sys.executable, os.path.join(HERE, 'trickminer.py'), '--repo', repo, '--tag', tag,
                                '--matching-from-configure', '--max-commits', str(a.max_commits)],
                               capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=3600)
        except subprocess.TimeoutExpired:
            return name, 'timeout'
        if r.returncode == 0:
            heads[name] = stamp
        return name, (r.stdout.strip().splitlines() or [(r.stderr.strip().splitlines() or ['?'])[-1]])[0]

    with ThreadPoolExecutor(a.j) as ex:
        res = list(ex.map(one, repos))
    U.save_json(HEADS, heads, indent=1)
    total = 0
    for name, msg in res:
        n = len(U.load_json('%s/ref_%s.json' % (OUT, name), []))
        total += n
        print('  %-45s %6d edits  %s' % (name, n, msg[:90]))
    print('refmine: %d projects, %d matching edits; now: fixrules.py build' % (len(repos), total))


if __name__ == '__main__':
    main()
