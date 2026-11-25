"""autoinstall.py - install every verified result of a runner package that still holds, and refuse the rest.

usage (project root): python runner/autoinstall.py <package folder> | --latest [--output build/scratch/runner]
                                                   [--dry] [--improved] [--watch MINUTES] [--build]

The runner never edits src/. If nobody reads its package, the results are lost and the next run starts from the same
source. This applies them with the checks a careful person would make; everything refused stays in the package as a hint.
  ready/       exact results: installed only if the function is REALLY identical (unitlib.strict_exact) in the CURRENT
               source, and no other function gets worse or starts calling / loading something else
  improved/    only with --improved: better but not exact. A higher score can come from wrong code, so the diff must also
               pass semguard.py (changed constants, swapped arguments, another variable, control flow ...)
  permuter_review/ready|improved   the same, from permcollect
Never installed: added volatile / goto / (int)this, comma expressions, `if (1)`, empty ifs, permuter temps (new_var).
Results whose source changed meanwhile are merged when the changed lines still occur exactly once, else skipped.
--watch M repeats every M minutes (Ctrl+C stops) on the newest package, for an unattended run.
--build runs ninja at the end and reports its exit code. Nothing is committed: commit yourself, so that `git diff` / `git
revert` stay your undo. Handled items are remembered in <package>/applied.json. Log: <output>/autoinstall.log.
"""
import argparse
import difflib
import glob
import hashlib
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.join(os.path.dirname(HERE), 'tools')
sys.path.insert(0, os.path.join(TOOLS, 'permuter'))
sys.path.insert(0, TOOLS)
import unitlib as U    # noqa: E402
import semguard        # noqa: E402

IDIOMS = (re.compile(r'(?:^|[^\w\s\]])\s*\(\s*0\s*,'), re.compile(r'\bnew_var\d*\b'), re.compile(r'\bif\s*\(\s*[01]\s*\)'),
          re.compile(r'\bif\s*\([^;{}]*\)\s*\{\s*\}'))


def hack(plus, minus):
    """why the added lines are not source anybody should keep, or None"""
    if sum('volatile' in l for l in plus) > sum('volatile' in l for l in minus):
        return 'adds volatile'
    if sum(bool(re.search(r'\bgoto\b', l)) for l in plus) > sum(bool(re.search(r'\bgoto\b', l)) for l in minus):
        return 'adds goto'
    for l in plus:
        if re.search(r'\((?:u8|u16|u32|s32|int)\)\s*this\b', l):
            return '(int)this'
        if any(p.search(l) for p in IDIOMS):
            return 'permuter idiom: ' + l.strip()[:60]
    return None


def merge(cur, old, new):
    """`new` carried onto `cur`: every changed block of old -> new must occur exactly once in cur (with context)"""
    if cur == old:
        return new
    o, n = old.split('\n'), new.split('\n')
    lines = cur.split('\n')
    for group in difflib.SequenceMatcher(None, o, n, autojunk=False).get_grouped_opcodes(3):
        before = o[group[0][1]:group[-1][2]]
        after = n[group[0][3]:group[-1][4]]
        hits = [i for i in range(len(lines) - len(before) + 1) if lines[i:i + len(before)] == before]
        if len(hits) != 1:
            return None
        lines[hits[0]:hits[0] + len(before)] = after
    return '\n'.join(lines)


def function_only(cur, new, fn):
    """`cur` with just the definition of fn taken from `new` (a result made before an earlier install in the same file
    still holds the OLD text of the other functions), or None"""
    a, b = U.definition(cur, fn), U.definition(new, fn)
    return cur[:a[0]] + new[b[0]:b[1]] + cur[a[1]:] if a and b else None


def text_of(path):
    with open(path, encoding='utf-8', errors='surrogateescape', newline='') as f:
        return f.read().replace('\r\n', '\n')


def items(pkg, improved):
    """[(exact?, info, old text, new text, whole file?, folder)]: exact results first"""
    out = []
    for sub in ['ready', 'permuter_review/ready'] + (['improved', 'permuter_review/improved'] if improved else []):
        for d in sorted(glob.glob(os.path.join(pkg, sub, '*'))):
            info = U.load_json(os.path.join(d, 'info.json'), None)
            if not info or not info.get('unit'):
                continue
            ns = glob.glob(os.path.join(d, 'new_source.*'))
            os_ = glob.glob(os.path.join(d, 'old_source.*'))
            of, nf = os.path.join(d, 'old_function.txt'), os.path.join(d, 'new_function.txt')
            if os.path.exists(of) and os.path.exists(nf):
                out.append((sub.endswith('ready'), info, text_of(of), text_of(nf), False, d))
            elif ns and os_:
                out.append((sub.endswith('ready'), info, text_of(os_[0]), text_of(ns[0]), True, d))
    return out


def install(pkg, a, log):
    done_f = os.path.join(pkg, 'applied.json')
    done = U.load_json(done_f, {})
    rows, base = items(pkg, a.improved), {}
    kept = skipped = 0
    for exact, info, old, new, whole, d in rows:
        fn, unit = info.get('func') or info.get('symbol'), info['unit']
        key = '%s:%s:%s' % (unit, fn, hashlib.md5((old + '\0' + new).encode('utf-8', 'surrogateescape')).hexdigest()[:12])
        src = U.source_of(unit)
        if key in done or not src:
            continue
        cur = U.read_source(src)

        def skip(why, final=True):
            nonlocal skipped
            skipped += 1
            log('  skip   %s %s: %s' % (unit, fn, why))
            if final and not a.dry:
                done[key] = why
                U.save_json(done_f, done, indent=0)
        if whole:
            texts = [function_only(cur, new, fn), merge(cur, old, new)]
        else:
            texts = [cur.replace(old, new) if cur.count(old) == 1 else None]
        texts = [x for i, x in enumerate(texts) if x is not None and x not in texts[:i]]
        if not texts:
            skip('the source changed since; merge by hand')
            continue
        if cur in texts and len(texts) == 1:
            skip('already in the source')
            continue
        texts = [x for x in texts if x != cur]
        tag = 'autoinst_' + re.sub(r'\W', '_', unit)[-60:]
        if unit not in base:
            base[unit] = U.scores(unit, None, tag)[:2]
        before, badb = base[unit]
        tmp = 'build/scratch/autoinstall/%s/%s' % (tag, os.path.basename(src))   # same basename: __sinit_<file> must match
        os.makedirs(os.path.dirname(tmp), exist_ok=True)
        why = None
        for text in texts:
            diff = list(difflib.unified_diff(cur.split('\n'), text.split('\n'), lineterm='', n=1))
            plus = [l[1:] for l in diff if l.startswith('+') and not l.startswith('+++')]
            minus = [l[1:] for l in diff if l.startswith('-') and not l.startswith('---')]
            why = hack(plus, minus)
            if why:
                why += ' - rewrite naturally by hand'
                continue
            risks = semguard.risks('\n'.join(diff), cur)
            if risks and not exact:
                why = 'may change behaviour (%s) - hint only' % risks[0][:90]
                continue
            U.write_source(tmp, text)
            after, bada, obj = U.scores(unit, tmp, tag)
            if not before or not after:
                why = 'does not compile now'
                continue
            identical = after.get(fn, 0) >= 100 - 1e-9 and U.strict_exact(unit, fn, obj)
            worse = U.no_worse(before, after, fn)
            if exact and not identical:
                why = 'not identical in the current source (%.2f)' % after.get(fn, 0)
            elif worse:
                why = 'others got worse: ' + ', '.join(worse[:3])
            elif bada - badb:
                why = 'new differences: ' + ', '.join(sorted(bada - badb)[:3])
            elif after.get(fn, 0) <= before.get(fn, 0):
                why = 'no gain now'
            else:
                why = None
                kept += 1
                log('  kept   %s %s: %.2f -> %.2f%s' % (unit, fn, before.get(fn, 0), after[fn], ' (identical)' if identical else ''))
                if not a.dry:
                    base[unit] = (after, bada)
                    U.write_source(src, text)
                    done[key] = 'kept'
                    U.save_json(done_f, done, indent=0)
                break
        if why:
            skip(why)
    log('autoinstall: %s: kept %d, skipped %d, %d handled before' % (os.path.basename(pkg), kept, skipped, len(rows) - kept - skipped))
    return kept


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('package', nargs='?')
    ap.add_argument('--latest', action='store_true')
    ap.add_argument('--output', default='build/scratch/runner', help="the runner's output folder (for --latest)")
    ap.add_argument('--dry', action='store_true')
    ap.add_argument('--improved', action='store_true')
    ap.add_argument('--watch', type=float, default=0, metavar='MINUTES')
    ap.add_argument('--build', action='store_true')
    a = ap.parse_args()
    if not a.package and not a.latest:
        ap.error('give a package folder or --latest')
    log = U.Log(a.output + '/autoinstall.log')
    while True:
        pkg = a.package
        if not pkg:
            found = glob.glob(os.path.join(a.output, 'package_*'))
            pkg = max(found, key=os.path.getmtime) if found else None
        if pkg:
            kept = install(pkg, a, log)
            if a.build and kept and not a.dry:
                r = subprocess.run(['ninja'], capture_output=True, text=True, encoding='utf-8', errors='replace')
                log('ninja exit code %d%s' % (r.returncode, '' if r.returncode == 0 else ' - CHECK THE LAST INSTALLS: git diff'))
        else:
            log('no package in ' + a.output)
        if not a.watch:
            break
        time.sleep(a.watch * 60)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
