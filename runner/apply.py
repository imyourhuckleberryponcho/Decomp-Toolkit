"""apply.py - apply ONE reviewed result from a runner package (or permcollect review folder) and re-verify it.

usage (project root): python runner/apply.py <result folder> [--dry] [--force]
  result folder   package_*/ready/<unit>__<func>,  package_*/improved/...,  package_*/permuter_review/ready/<func> ...

Two kinds of result folder are understood:
  * unit-source:  new_source.<ext> + old_source.<ext> + info.json (runner stages): the project source must still be
                  byte-identical to old_source (otherwise someone edited it since: merge by hand using diff.txt);
  * function:     new_function.txt + old_function.txt + info.json (permcollect): old_function.txt must occur exactly once
                  in the current source; it is replaced by new_function.txt.
Then tools/fcheck.py compares the unit: the function must reach the verified percentage and no other function of the unit
may get worse. On any failure the original source is restored (a backup copy <src>.before_apply is kept on success).
--dry only prints what would happen; --force skips the staleness checks (not the final verification).
Nothing is committed; look at `git diff` and build with your normal ninja before committing.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__)).replace('\\', '/')
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'tools'))
try:
    from dtkproj import REPO
except SystemExit:
    if not any(a in ('-h', '--help') for a in sys.argv):
        raise
    REPO = '.'
PY = sys.executable


def fcheck_rows(unit):
    r = subprocess.run([PY, os.path.join(os.path.dirname(HERE), 'tools', 'fcheck.py'), unit, '--tag', 'apply_check'],
                       capture_output=True, text=True, encoding='utf-8', errors='replace', cwd=REPO)
    text = r.stdout + r.stderr
    if 'COMPILE FAILED' in text or r.returncode:
        return None, text
    return {m.group(3): float(m.group(1)) for l in text.splitlines()
            for m in [re.match(r'^\s*(\d+\.\d+)%\s+(\d+)\s+(\S+)', l)] if m}, text


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('folder')
    ap.add_argument('--dry', action='store_true')
    ap.add_argument('--force', action='store_true')
    a = ap.parse_args()
    d = a.folder
    info = json.load(open(os.path.join(d, 'info.json'), encoding='utf-8'))
    src, unit = info['src'], info['unit']
    sym = info.get('func') or info.get('symbol')
    if not os.path.isabs(src):
        src = os.path.join(REPO, src)
    cur = open(src, encoding='utf-8', errors='replace', newline='').read()
    newf = [f for f in os.listdir(d) if f.startswith('new_source')]
    if newf:
        old = open(os.path.join(d, 'old_source' + os.path.splitext(newf[0])[1]), encoding='utf-8', errors='replace', newline='').read()
        if cur != old and not a.force:
            sys.exit('%s changed since this result was made; merge by hand (see %s/diff.txt) or use --force' % (src, d))
        new = open(os.path.join(d, newf[0]), encoding='utf-8', errors='replace', newline='').read()
    else:
        of = open(os.path.join(d, 'old_function.txt'), encoding='utf-8', newline='').read()
        nf = open(os.path.join(d, 'new_function.txt'), encoding='utf-8', newline='').read()
        if cur.count(of) != 1:
            sys.exit('the old function text occurs %d times in %s (expected 1): the source changed; merge by hand' % (cur.count(of), src))
        new = cur.replace(of, nf)
    print('apply to %s (%s %s)' % (src, unit, sym))
    if a.dry:
        print('dry run: nothing written')
        return
    before_rows, _ = fcheck_rows(unit)
    shutil.copyfile(src, src + '.before_apply')
    open(src, 'w', encoding='utf-8', newline='').write(new)
    rows, text = fcheck_rows(unit)

    def restore(why):
        shutil.copyfile(src + '.before_apply', src)
        os.remove(src + '.before_apply')
        sys.exit('NOT APPLIED, source restored: ' + why)
    if rows is None:
        restore('does not compile\n' + text[-500:])
    b, n = (before_rows or {}).get(sym, 100.0), rows.get(sym, 100.0)
    worse = [k for k, p in rows.items() if k != sym and p < (before_rows or {}).get(k, 100.0)]
    if worse:
        restore('other functions got worse: ' + ', '.join(worse[:5]))
    if n <= b:
        restore('%s did not improve (%.2f%% -> %.2f%%)' % (sym, b, n))
    print('VERIFIED: %s %.2f%% -> %.2f%%' % (sym, b, n))
    for line in text.splitlines():
        if line.startswith(('LAYOUT', 'CONST', 'CALLS', 'DATA', '==')):
            print('  ' + line)
    print('backup: %s.before_apply  (delete it when you are happy; then run your normal build)' % src)


if __name__ == '__main__':
    main()
