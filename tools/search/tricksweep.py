#!/usr/bin/env python3
"""tricksweep.py - apply the trick library (tricks.py) to ONE function and hill-climb on the strict diff.

For the function it generates every variant of every trick (round-robin, so a time budget samples all tricks),
compiles each with fcheck (the unit's real compile command, private scratch folders, in parallel), keeps the best
improving variant, stacks the other improving ones onto it when their changes do not overlap, and repeats from the new
text up to --depth levels. src/ is never touched.

usage (project root, or set DECOMP_REPO):
  python tools/search/tricksweep.py UNIT FUNC [--src FILE] [--timeout SEC] [-j 6] [--depth 3]
                                    [--tricks t_commute,t_types,...] [--plugins my_tricks.py,...] [--write OUT]
  python tools/search/tricksweep.py --list            # the tricks and what they do
  UNIT = path under src/ without extension; FUNC = asm symbol name (e.g. update__5PlayerFv)
  --src FILE   start from this file instead of src/UNIT.c(pp)
Score = number of differing instruction lines of FUNC against the target (registers and relocations included;
lower is better, 0 = exact). Last output line (machine readable):
  RESULT <unit> <func> <base_score> <best_score> <path-to-best-variant-or-NONE>
Environment: SEARCH_SCRATCH (default build/scratch/search), SEARCH_TAG (prefix for fcheck tag folders).
Winners are text rewrites guarded against obvious semantic traps, not proofs: read the diff before installing one.
"""
import difflib, os, re, subprocess, sys, threading, queue, time
from concurrent.futures import ThreadPoolExecutor

H = os.path.dirname(os.path.abspath(__file__)).replace('\\', '/')
sys.path.insert(0, os.path.dirname(H))
sys.path.insert(0, H)
import tricks

# --plugins a.py,b.py : your project's own tricks (also SEARCH_TRICK_PLUGINS); see guide/11-customizing-for-your-game.md
if '--plugins' in sys.argv[1:]:
    _i = sys.argv.index('--plugins')
    tricks.load_plugins(sys.argv[_i + 1].split(','))
    del sys.argv[_i:_i + 2]


def list_tricks():
    for t in tricks.TRICKS:
        print('%-26s %s' % (t.name, t.description))


if '--list' in sys.argv[1:]:
    list_tricks()
    sys.exit(0)
if any(a in ('-h', '--help') for a in sys.argv[1:]) or len(sys.argv) < 3:
    print(__doc__)
    sys.exit(0 if len(sys.argv) > 1 else 2)

from dtkproj import *   # REPO, VER, EXE; chdir to the project root

FCHECK = os.path.join(os.path.dirname(H), 'fcheck.py')
SCRATCH = os.environ.get('SEARCH_SCRATCH', 'build/scratch/search').rstrip('/') + '/tricksweep'
TAGP = os.environ.get('SEARCH_TAG', '')
T0 = time.time()
args = sys.argv[1:]


def opt(name, default=None):
    return args[args.index(name) + 1] if name in args else default


unit, func = args[0], args[1]
J = int(opt('-j', 6))
DEPTH = int(opt('--depth', 3))
TIMEOUT = float(opt('--timeout', 0) or 0)
DEADLINE = (T0 + TIMEOUT) if TIMEOUT else None
src_orig = next((p for p in ('src/%s.cpp' % unit, 'src/%s.c' % unit, 'src/%s.cp' % unit) if os.path.exists(p)), None)
if not src_orig:
    sys.exit('no source file src/%s.c|cpp' % unit)
src_path = opt('--src') or src_orig
ext = os.path.splitext(src_orig)[1]
raw = open(src_path, encoding='latin-1', newline='').read()
CRLF = '\r\n' in raw
SRC = raw.replace('\r\n', '\n')
chosen = tricks.TRICKS
if opt('--tricks'):
    want = set(opt('--tricks').split(','))
    chosen = [t for t in tricks.TRICKS if t.name in want]
    if not chosen:
        sys.exit('no such trick; see --list')

NAME = next((n for n in tricks.source_names(func) if tricks.find_body(SRC, n)), None)


def fail(msg):
    print(msg)
    print('RESULT %s %s -1 -1 NONE' % (unit, func))
    sys.exit(2)


if not NAME:
    fail('definition of %s not found in %s (tried %s)' % (func, src_path, ', '.join(tricks.source_names(func))))

os.makedirs(SCRATCH, exist_ok=True)


# ------------------------------------------------------------------ scoring
def norm(n):
    return re.sub(r'@[0-9]+', '@N', n)


def body_asm(path, name):
    out, on = [], False
    for ln in open(path, encoding='utf-8', errors='replace'):
        ln = ln.rstrip('\n')
        if ln.startswith('.fn '):
            on = norm(ln.split()[1].rstrip(',').strip('"')) == norm(name)
            continue
        if on:
            if ln.startswith('.endfn'):
                on = False
                continue
            ln = re.sub(r'^/\* \S+ \S+\s+(\S\S ){4}\*/\s*', '', ln).strip()
            ln = re.sub(r'\.L_[0-9A-F]+', '.L', ln)
            ln = re.sub(r'"?@[0-9]+"?', '@N', ln)
            if ln and not ln.startswith('.L'):
                out.append(ln)
    return out


def diff_count(t, m):
    sm = difflib.SequenceMatcher(None, t, m, autojunk=False)
    return sum(max(i2 - i1, j2 - j1) for op, i1, i2, j1, j2 in sm.get_opcodes() if op != 'equal')


slots = queue.Queue()
for i in range(J):
    slots.put(i)
cache = {}
lock = threading.Lock()
TIMED_OUT = [False]


def score(text):
    """differing instruction lines of FUNC for the full source text (None = does not compile / out of time)"""
    with lock:
        if text in cache:
            return cache[text]
    if DEADLINE and time.time() > DEADLINE:
        TIMED_OUT[0] = True
        return None
    slot = slots.get()
    try:
        sd = '%s/s%d_%d' % (SCRATCH, os.getpid(), slot)
        os.makedirs(sd, exist_ok=True)
        path = '%s/%s' % (sd, os.path.basename(src_orig))   # keep the file name (anonymous-namespace mangling)
        open(path, 'w', encoding='latin-1', newline='').write(text.replace('\n', '\r\n') if CRLF else text)
        tag = '%stricksweep%d_%d' % (TAGP, os.getpid(), slot)
        r = subprocess.run([sys.executable, FCHECK, unit, '--tag', tag, '--src', path, '--diff', func],
                           capture_output=True, text=True)
        o = r.stdout + r.stderr
        d = os.path.join('build/scratch/fcheck', tag)
        res = None
        if 'COMPILE FAILED' not in o and os.path.exists(os.path.join(d, 'M.s')) and os.path.exists(os.path.join(d, 'T.s')):
            t = body_asm(os.path.join(d, 'T.s'), func)
            m = body_asm(os.path.join(d, 'M.s'), func)
            res = diff_count(t, m) if m else None
        for f_ in ('T.s', 'M.s'):
            try:
                os.remove(os.path.join(d, f_))
            except OSError:
                pass
    finally:
        slots.put(slot)
    with lock:
        cache[text] = res
    return res


# ------------------------------------------------------------------ guards + stacking
def refused(prev, new):
    """why a variant must not be used as a base (hack shapes the library never produces on purpose)"""
    diff = list(difflib.unified_diff(prev.split('\n'), new.split('\n'), lineterm='', n=0))
    plus = [l for l in diff if l.startswith('+') and not l.startswith('+++')]
    minus = [l for l in diff if l.startswith('-') and not l.startswith('---')]
    if sum('volatile' in l for l in plus) > sum('volatile' in l for l in minus):
        return 'volatile'
    if any(re.search(r'\(\s*0\s*,', l) for l in plus):
        return 'comma hack'
    return None


def merge_change(base_lines, cur_text, vtext):
    """apply vtext's single changed line region (relative to the base) to cur_text if the regions do not overlap"""
    v = vtext.split('\n')
    n, m = len(base_lines), len(v)
    p = 0
    while p < n and p < m and base_lines[p] == v[p]:
        p += 1
    q = 0
    while q < n - p and q < m - p and base_lines[n - 1 - q] == v[m - 1 - q]:
        q += 1
    st, en, new = p, n - q, v[p:m - q]
    c = cur_text.split('\n')
    k = len(c)
    a = 0
    while a < n and a < k and base_lines[a] == c[a]:
        a += 1
    b = 0
    while b < n - a and b < k - a and base_lines[n - 1 - b] == c[k - 1 - b]:
        b += 1
    cst, cen = a, n - b
    if not (en <= cst or st >= cen):
        return None
    if en <= cst:
        return '\n'.join(c[:st] + new + c[en:])
    shift = k - n
    return '\n'.join(c[:st + shift] + new + c[en + shift:])


COMBINE = 6   # improving variants of one level that are tried stacked onto its winner

# ------------------------------------------------------------------ search
base = score(SRC)
print('%s %s: base %s differing lines (function written as %s, %d tricks)' % (unit, func, base, NAME, len(chosen)))
if base is None:
    fail('base does not compile / function not found in the object')
best, best_text, steps = base, SRC, []
tried = 0
for level in range(DEPTH):
    if best == 0 or (DEADLINE and time.time() > DEADLINE):
        break
    cands = list(tricks.variants(best_text, NAME, chosen))
    if not cands:
        break
    with ThreadPoolExecutor(J) as ex:
        res = list(ex.map(lambda c: (c[0], c[1], score(c[1])), cands))
    tried += sum(1 for r in res if r[2] is not None)
    ok = sorted([r for r in res if r[2] is not None], key=lambda r: r[2])
    print('level %d: %d variants, %d compiled%s' % (level + 1, len(cands), len(ok),
                                                   ' (stopped by --timeout)' if TIMED_OUT[0] else ''))
    for desc, _, s in ok[:5]:
        print('  %4d  %s' % (s, desc[:110]))
    good = []
    for desc, vt, s in ok:
        if s < best:
            why = refused(best_text, vt)
            if why:
                print('  (skipped %s: %s)' % (why, desc[:60]))
                continue
            good.append((desc, vt, s))
    if not good:
        break
    lvl_best, lvl_text, lvl_desc = good[0][2], good[0][1], [good[0][0]]
    base_lines = best_text.split('\n')
    for desc, vt, _ in good[1:COMBINE + 1]:
        if DEADLINE and time.time() > DEADLINE:
            break
        merged = merge_change(base_lines, lvl_text, vt)
        if not merged or refused(best_text, merged):
            continue
        sc = score(merged)
        if sc is not None and sc < lvl_best:
            lvl_best, lvl_text = sc, merged
            lvl_desc.append(desc)
    best, best_text = lvl_best, lvl_text
    steps.append(' + '.join(lvl_desc))
    print('  -> %d  %s' % (best, steps[-1][:150]))

print('\nBEST %s -> %s after %d compiles: %s' % (base, best, tried, ' ; '.join(steps) or '(no improvement)'))
out = 'NONE'
if best < base:
    out = opt('--write') or '%s/best_%s%s' % (SCRATCH, re.sub(r'\W', '_', func)[:40], ext)
    open(out, 'w', encoding='latin-1', newline='').write(best_text.replace('\n', '\r\n') if CRLF else best_text)
    print('\n'.join(list(difflib.unified_diff(SRC.split('\n'), best_text.split('\n'), lineterm='', n=1))[2:80]))
    print('written: %s' % out)
print('RESULT %s %s %s %s %s' % (unit, func, base, best, out.replace('\\', '/')))
