"""structdist.py: structural distance of one function = instructions that differ after replacing register
names by R/F (multiset compare of the fcheck --diff lines). 0 = only register allocation/order differs.
usage: python structdist.py <unit> <function-prefix> [--show]  (--show lists the differing normalised lines)"""
from dtkproj import *   # REPO, VER, EXE, HERE; chdir to the project root
import sys, subprocess, re
from collections import Counter
unit, fn = sys.argv[1], sys.argv[2]
tag = 'sd_' + re.sub(r'\W', '_', unit.split('/')[-1]) + '_' + str(__import__('os').getpid())  # private folder: safe beside a run
args = ['python', HERE + '/fcheck.py', unit, '--tag', tag, '--diff', fn]
if '--src' in sys.argv: args += ['--src', sys.argv[sys.argv.index('--src') + 1]]
out = subprocess.run(args, capture_output=True, text=True).stdout
__import__('shutil').rmtree('build/scratch/fcheck/' + tag, ignore_errors=True)
L = [l for l in out.splitlines() if l[:1] and l[:1] in '+-' and not l.startswith(('---', '+++'))]
norm = lambda l: re.sub(r'\bf\d+\b', 'F', re.sub(r'\br\d+\b', 'R', l[1:].strip()))
m = Counter(norm(l) for l in L if l[0] == '-'); p = Counter(norm(l) for l in L if l[0] == '+')
a, b = m - p, p - m
print('struct distance %d (target-only %d, ours-only %d)' % (sum(a.values()) + sum(b.values()), sum(a.values()), sum(b.values())))
if '--show' in sys.argv:
    for k, v in a.items(): print('  target-only x%d: %s' % (v, k))
    for k, v in b.items(): print('  ours-only   x%d: %s' % (v, k))
