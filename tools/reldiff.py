"""reldiff.py: strict per-instruction diff with branch targets as offsets from the function start.
fcheck/objdiff normalise local branch labels, so a function whose only difference is WHERE a branch goes
(break vs continue vs return, a statement inside/outside a loop) shows as 99.9% with an empty diff.

  reldiff.py <unit> <func> [--src f]          diff one function
  reldiff.py --scan [--lo 97] [-j 6] [--only U] list near misses whose diff is branch-target-only
                                               (or tiny) -> build/scratch/reldiff_scan.md
"""
from dtkproj import *   # REPO, VER, EXE, HERE; chdir to the project root
import sys, subprocess, re, os, difflib, json, argparse
from concurrent.futures import ThreadPoolExecutor

DTK = os.path.join('build', 'tools', 'dtk' + EXE)
MAXLINES = 2


def compile_unit(unit, tag, src=None):
    cmd = [sys.executable, HERE + '/fcheck.py', unit, '--tag', tag]
    if src:
        cmd += ['--src', src]
    subprocess.run(cmd, capture_output=True, stdin=subprocess.DEVNULL)
    return f'build/scratch/fcheck/{tag}/{os.path.basename(unit)}.o'


def disasm(o, out):
    subprocess.run([DTK, 'elf', 'disasm', o, out], capture_output=True, stdin=subprocess.DEVNULL)
    text = open(out, encoding='utf8', errors='replace').read()
    funcs, cur, name = {}, None, None
    for l in text.splitlines():
        m = re.match(r'\.fn\s+"?([^",\s]+)"?', l)
        if m:
            name, cur = m.group(1), []
            continue
        if cur is not None and l.startswith('.endfn'):
            funcs[name] = cur
            cur = None
            continue
        if cur is not None:
            cur.append(l)
    return funcs


def norm(ls):
    start = None
    for l in ls:
        m = re.match(r'/\* ([0-9A-F]{8}) ', l)
        if m:
            start = int(m.group(1), 16)
            break
    out = []
    for l in ls:
        if l.startswith('.L_'):
            out.append('L+%X:' % (int(l[3:11], 16) - start))
            continue
        m = re.match(r'/\* ([0-9A-F]{8}) [0-9A-F]+\s+([0-9A-F ]{11}) \*/\s*(.*)', l)
        if not m:
            continue
        ins = m.group(3)
        ins = re.sub(r'\.L_([0-9A-F]{8})', lambda x: 'L+%X' % (int(x.group(1), 16) - start), ins)
        ins = re.sub(r'@\d+', '@N', ins)
        out.append('%04X %s' % (int(m.group(1), 16) - start, ins))
    return out


def fdiff(t, o):
    return list(difflib.unified_diff(t, o, 'target', 'mine', lineterm='', n=2))


def classify(t, o):
    """'branch' if same length and every differing line differs only in branch targets/label lines."""
    if len(t) != len(o):
        return 'size'
    strip = lambda s: re.sub(r'L\+[0-9A-F]+', 'L', s)
    bad = 0
    for a, b in zip(t, o):
        if a != b and strip(a) != strip(b):
            bad += 1
    return 'branch' if bad == 0 else 'other%d' % bad


def one(unit, func, src):
    tag = 'reldiff_' + re.sub(r'\W', '_', func)[:40]
    ours = compile_unit(unit, tag, src)
    tf = disasm(f'build/{VER}/obj/{unit}.o', f'build/scratch/{tag}_t.s')
    of = disasm(ours, f'build/scratch/{tag}_o.s')
    t, o = norm(tf.get(func, [])), norm(of.get(func, []))
    d = fdiff(t, o)
    print('\n'.join(d) if d else 'IDENTICAL (%d lines)' % len(t))


def scan_unit(unit, funcs):
    tag = 'reldiffscan_' + re.sub(r'\W', '_', unit)[-40:]
    ours = compile_unit(unit, tag)
    if not os.path.exists(ours):
        return []
    tf = disasm(f'build/{VER}/obj/{unit}.o', f'build/scratch/fcheck/{tag}/t.s')
    of = disasm(ours, f'build/scratch/fcheck/{tag}/o.s')
    res = []
    for f, pct, size in funcs:
        if f not in tf or f not in of:
            continue
        t, o = norm(tf[f]), norm(of[f])
        if t == o:
            res.append((unit, f, pct, size, 'IDENTICAL-code'))
            continue
        c = classify(t, o)
        if c == 'size':
            n = sum(1 for l in fdiff(t, o) if l[:1] in '+-' and not l.startswith(('+++', '---')))
            if abs(len(t) - len(o)) <= 2 and n <= MAXLINES * 2:
                res.append((unit, f, pct, size, 'size%d' % n))
        elif c == 'branch' or int(c[5:]) <= MAXLINES:
            res.append((unit, f, pct, size, c))
    return res


def scan(lo, jobs, only):
    r = json.load(open(S('build/@V/report.json')))
    work = {}
    for u in r['units']:
        name = u['name']
        if name.startswith('main/'):
            name = name[5:]
        if only and only not in name:
            continue
        if not os.path.exists(f'build/{VER}/obj/{name}.o'):
            continue
        for fn in u.get('functions', []):
            p = fn.get('fuzzy_match_percent', 0)
            if lo <= p < 100:
                work.setdefault(name, []).append((fn['name'], p, int(fn['size'])))
    out = []
    with ThreadPoolExecutor(jobs) as ex:
        for res in ex.map(lambda kv: scan_unit(*kv), work.items()):
            out += res
            for x in res:
                print('%-50s %-60s %6.2f %5d %s' % x, flush=True)
    out.sort(key=lambda x: (x[4] != 'branch', -x[3]))
    with open('build/scratch/reldiff_scan.md', 'w') as fh:
        for x in out:
            fh.write('%s\t%s\t%.2f\t%d\t%s\n' % x)
    print('%d candidates -> build/scratch/reldiff_scan.md' % len(out))


if __name__ == '__main__':
    if '--scan' in sys.argv:
        ap = argparse.ArgumentParser()
        ap.add_argument('--scan', action='store_true')
        ap.add_argument('--lo', type=float, default=97)
        ap.add_argument('-j', type=int, default=6)
        ap.add_argument('--only')
        ap.add_argument('--maxlines', type=int, default=2)
        a = ap.parse_args()
        MAXLINES = a.maxlines
        scan(a.lo, a.j, a.only)
    else:
        unit, func = sys.argv[1], sys.argv[2]
        src = sys.argv[sys.argv.index('--src') + 1] if '--src' in sys.argv else None
        one(unit, func, src)
