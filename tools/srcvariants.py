"""srcvariants.py: try hand-written source variants of one unit and report every function whose score changes.

usage (from the project root): python srcvariants.py <unit> <variants.py> [--func F]
  variants.py defines VARIANTS = [(name, [(old, new), ...]), ...]; each (old, new) is a plain text replacement in
  src/<unit>.cpp|.c (old must occur). The file is restored afterwards (also on Ctrl+C / errors).
  --func F : also print the remaining diff line count of F (strict, registers included) per variant.
  --struct : with --func, also print the register-independent distance (target-only/ours-only instructions).
  SCOPE = "static void foo(int a) {" in variants.py (optional, ): replacements only apply inside the function
  that starts with that line (up to the next line that is exactly "}"), so twin functions are left alone.
Compile failures are reported (fcheck's message goes to stderr; both streams are checked).
"""
from dtkproj import *   # REPO, VER, EXE, HERE; chdir to the project root
import os, re, subprocess, sys

os.chdir(REPO)
H = os.path.dirname(os.path.abspath(__file__)) + '/'
unit, vfile = sys.argv[1], sys.argv[2]
func = sys.argv[sys.argv.index('--func') + 1] if '--func' in sys.argv else None
src = next(p for p in ('src/%s.cpp' % unit, 'src/%s.c' % unit) if os.path.exists(p))
RAW = open(src, 'rb').read()   # restore the exact bytes (LF files used to come back as CRLF)
NEWLINE = '\r\n' if b'\r\n' in RAW else '\n'
orig = RAW.decode('latin-1').replace('\r\n', '\n')
ns = {'SRC': orig}
exec(open(vfile, encoding='utf-8').read(), ns)


TAG = sys.argv[sys.argv.index('--tag') + 1] if '--tag' in sys.argv else 'srcvar'


def score(tag=None):
    tag = tag or TAG
    r = subprocess.run([sys.executable, H + 'fcheck.py', unit, '--all', '--tag', tag], capture_output=True, text=True)
    o = r.stdout + r.stderr
    if 'COMPILE FAILED' in o:
        err = re.findall(r'^#\s+(.*error.*|.*Error.*)$', o, re.M)
        return None, ' '.join(err[:2])
    d = {m.group(3): float(m.group(1)) for m in re.finditer(r'^\s*([\d.]+)%\s+(\d+)\s+(\S+)', o, re.M)}
    extra = ''
    if func:
        r2 = subprocess.run([sys.executable, H + 'fcheck.py', unit, '--tag', tag, '--diff', func], capture_output=True, text=True)
        n = sum(1 for l in r2.stdout.split('\n') if l[:1] in '+-' and not l.startswith(('+++', '---')))
        extra = ' | %s diff lines %d' % (func[:30], n)
        if '--struct' in sys.argv:   # register-independent distance (instructions only one side has)
            from collections import Counter
            nr = lambda s: re.sub(r'\bf\d+\b', 'fX', re.sub(r'\br\d+\b', 'rX', s)).strip()
            L = r2.stdout.split('\n')
            a = Counter(nr(l[1:]) for l in L if l.startswith('-') and not l.startswith('---'))
            b = Counter(nr(l[1:]) for l in L if l.startswith('+') and not l.startswith('+++'))
            extra += ' | struct %d/%d' % (sum((a - b).values()), sum((b - a).values()))
    return d, extra


try:
    base, bx = score()
    print('base', bx)
    for name, reps in ns['VARIANTS']:
        t = orig
        pre, post = '', ''
        if ns.get('SCOPE'):
            i = t.index(ns['SCOPE'])
            j = t.index('\n}\n', i) + 3
            pre, t, post = t[:i], t[i:j], t[j:]
        bad = [o for o, n in reps if o not in t]
        if bad:
            print('%-14s PATTERN NOT FOUND: %r' % (name, bad[0][:70]))
            continue
        for o, n in reps:
            t = t.replace(o, n)
        t = pre + t + post
        open(src, 'w', encoding='latin-1', newline=NEWLINE).write(t)
        d, extra = score()
        if d is None:
            print('%-14s COMPILE FAILED %s' % (name, extra))
            continue
        ch = ['%s %.2f->%.2f' % (k[:28], base.get(k, 100.0), v) for k, v in d.items() if abs(v - base.get(k, 100.0)) > 0.001]
        gone = [k[:28] + ' EXACT' for k in base if k not in d]
        print('%-14s %s%s' % (name, ', '.join(ch + gone) or 'no change', extra))
finally:
    open(src, 'wb').write(RAW)
