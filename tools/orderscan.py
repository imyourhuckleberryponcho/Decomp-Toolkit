"""orderscan.py: units whose .text FUNCTION ORDER differs from the original.

usage (from the project root): python orderscan.py [--all]
Reads the target order from build/<VER>/asm/<unit>.s (.fn lines) and ours from the last ninja build
(build/<VER>/src/<unit>.o, nm -n). Prints per unit: REVERSED (ours = exact reverse: deferred file written in emission
order -> reverse the source), SAME-SET (same functions, other order: move functions), or MISSING n (functions not
written yet; order compared on the common ones). Fixing the order fixes constant numbering / string pool layout.
"""
from dtkproj import *   # REPO, VER, EXE, HERE; chdir to the project root
import os, re, subprocess, sys

os.chdir(REPO)
NM = os.path.abspath('build/binutils/powerpc-eabi-nm' + EXE)


def target_order(unit):
    p = S('build/@V/asm/%s.s') % unit
    if not os.path.exists(p):
        return None
    out, sec = [], None
    for line in open(p, encoding='utf-8', errors='replace'):
        if line.startswith('.section'):
            sec = line.split()[1].rstrip(',')
        elif re.match(r'\.(text|data|rodata|bss|init|ctors|dtors)\s*$', line):
            sec = line.strip()
        m = re.match(r'\.fn (\S+),', line)
        if m and sec == '.text':
            out.append(m.group(1))
    return out


def our_order(obj):
    r = subprocess.run([NM, '-n', obj], capture_output=True, text=True)
    out = []
    for line in r.stdout.split('\n'):
        parts = line.split()
        if len(parts) == 3 and parts[1] in 'Tt':
            out.append(parts[2])
    return out


def main():
    cfg = open('configure.py', encoding='utf-8').read()
    units = re.findall(r'Object\(NonMatching, "([^"]+)\.(?:cpp|c)"', cfg)
    if '--all' in sys.argv:
        units += re.findall(r'Object\(MatchingFor\([^)]*\), "([^"]+)\.(?:cpp|c)"', cfg)
    rows = []
    for u in units:
        t = target_order(u)
        obj = S('build/@V/src/%s.o') % u
        if not t or not os.path.exists(obj):
            continue
        o = our_order(obj)
        common = [f for f in o if f in set(t)]
        tc = [f for f in t if f in set(o)]
        if len(tc) < 2 or common == tc:
            continue
        missing = len(set(t) - set(o))
        kind = 'REVERSED' if common == tc[::-1] else 'SAME-SET' if not missing else 'MISSING %d' % missing
        # first position where they differ
        i = next(k for k in range(len(tc)) if common[k] != tc[k])
        rows.append((kind, u, len(tc), i, tc[i], common[i]))
    for r in sorted(rows):
        print('%-10s %-55s %3d fns, differs at #%d: target %s / ours %s' % r)
    print('%d units with another function order' % len(rows))


main()
