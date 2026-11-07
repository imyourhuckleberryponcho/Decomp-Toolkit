"""shapedist.py: the REAL code difference of a near-miss function.

Counts the instructions that still differ after ignoring everything that comes from register allocation:
register names, stack-slot offsets (N(r1)), plain register copies (mr / fmr), saved-register spills and reloads,
stmw/lmw, the frame size, and anonymous labels (@N, section anchors).  What is left is a genuine shape difference
(missing or extra statements, other constants, other branches, other operations).

  0 = only colouring / stack-slot order / copy coalescing differs (the slow "puzzle" kind: use a register search)
  >0 = something in the SOURCE is different: fix that first.

Why it exists: simpler distances (register-independent line counts) still count stack-slot shifts and `mr` copies,
so many "structural" near misses are really colouring puzzles in disguise.  Use this one to triage.

The register/slot rules below are PowerPC (r1 = stack pointer, mr, stmw ...).  For another architecture edit the
two regular expressions in norm() and ALLOC.

usage: python shapedist.py UNIT FUNC [--show] [--src FILE]
       python shapedist.py --batch QUEUE.tsv [-j 6] [--out OUT.tsv]    (QUEUE: unit<TAB>function per line)
"""
from dtkproj import *   # REPO, VER, EXE, HERE; chdir to the project root
import os, re, shutil, subprocess, sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor


def norm(l):
    l = l.strip()
    l = re.sub(r'\bf\d+\b', 'F', l)
    l = re.sub(r'\br(?!1\b)\d+\b', 'R', l)                       # keep r1 (stack pointer) visible
    l = re.sub(r'-?0x[0-9a-fA-F]+\(r1\)|-?\d+\(r1\)', 'S(r1)', l)  # stack slot
    l = re.sub(r'\.L\w*', '.L', l)
    l = re.sub(r'^(addi\s+R,\s*r1,\s*)(-?0x[0-9a-fA-F]+|-?\d+)', r'\1S', l)   # address of a stack slot
    # anonymous section symbols and @N literals are the same data under another name
    l = re.sub(r'\.\.\.[a-z0-9]+\.\d+|@N|"?@\d+"?', 'ANON', l)
    return l


# instructions that follow from the colouring, not from the source shape
ALLOC = re.compile(r'^(mr|fmr)\s|^(stw|lwz|stfd|lfd|stfs|lfs)\s+[rf]\d+,\s*-?(0x[0-9a-fA-F]+|\d+)\(r1\)'
                   r'|^psq_(st|l)\s+f\d+,\s*-?(0x[0-9a-fA-F]+|\d+)\(r1\),\s*0,\s*qr0|^(stmw|lmw)\s|^stwu\s+r1,'
                   r'|^addi\s+r1,\s*r1,')


def dist(unit, fn, show=False, src=None):
    tag = 'shd_%s_%d' % (re.sub(r'\W', '_', unit.split('/')[-1])[:20], abs(hash(fn)) % 100000)
    cmd = [sys.executable, HERE + '/fcheck.py', unit, '--tag', tag, '--diff', fn]
    if src:
        cmd += ['--src', src]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=900).stdout
    except subprocess.TimeoutExpired:
        return None, 'timeout'
    finally:
        shutil.rmtree('build/scratch/fcheck/' + tag, ignore_errors=True)
    L = [l for l in out.splitlines() if l[:1] in ('+', '-') and not l.startswith(('---', '+++'))]
    if not L and 'COMPILE FAILED' in out:
        return None, 'compile failed'
    keep = lambda s: not ALLOC.match(s)
    m = Counter(norm(l[1:]) for l in L if l[0] == '-' and keep(l[1:].strip()))
    p = Counter(norm(l[1:]) for l in L if l[0] == '+' and keep(l[1:].strip()))
    a, b = m - p, p - m
    # our section anchor (`...data.0`+off) versus the target's NAMED symbol at the same bytes is not a code
    # difference (it links identically): cancel such pairs when the rest of the instruction is equal.
    sym = lambda s: re.sub(r'("[^"]+"|[A-Za-z_$][\w$@]*)(@(ha|l|h|sda21))', r'SYM\2', s)
    both = Counter(sym(k) for k in a.elements() if 'ANON' not in k) & \
        Counter(sym(k) for k in b.elements() if 'ANON' in k)
    if both:
        def drop(c, pred):
            left, res = Counter(both), Counter()
            for k in c.elements():
                if pred(k) and left[sym(k)] > 0:
                    left[sym(k)] -= 1
                else:
                    res[k] += 1
            return res
        a = drop(a, lambda k: 'ANON' not in k)
        b = drop(b, lambda k: 'ANON' in k)
    copies = sum(1 for l in L if re.match(r'^[-+]\s*(mr|fmr)\s', l))
    detail = ''
    if show:
        detail = '\n'.join(['  target-only x%d: %s' % (v, k) for k, v in a.items()] +
                           ['  ours-only   x%d: %s' % (v, k) for k, v in b.items()])
    return (sum(a.values()), sum(b.values()), copies), detail


args = sys.argv[1:]
if '--batch' in args:
    q = args[args.index('--batch') + 1]
    J = int(args[args.index('-j') + 1]) if '-j' in args else 6
    outp = args[args.index('--out') + 1] if '--out' in args else 'build/scratch/shapedist.tsv'
    rows = [l.rstrip('\n').split('\t')[:2] for l in open(q) if l.strip() and not l.startswith('#')]
    with ThreadPoolExecutor(J) as ex:
        res = list(ex.map(lambda r: dist(r[0], r[1])[0], rows))
    with open(outp, 'w') as f:
        f.write('# unit\tfunc\treal_target_only\treal_ours_only\treal_total\tcopy_lines\n')
        for r, d in zip(rows, res):
            if d is None:
                f.write('%s\t%s\t?\t?\t?\t?\n' % tuple(r))
            else:
                f.write('%s\t%s\t%d\t%d\t%d\t%d\n' % (r[0], r[1], d[0], d[1], d[0] + d[1], d[2]))
    print('written', outp)
else:
    src = args[args.index('--src') + 1] if '--src' in args else None
    d, detail = dist(args[0], args[1], '--show' in args, src)
    if d is None:
        print('failed:', detail)
    else:
        print('real shape distance %d (target-only %d, ours-only %d); copy lines %d' % (d[0] + d[1], d[0], d[1], d[2]))
        if detail:
            print(detail)
