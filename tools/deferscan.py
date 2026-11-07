"""deferscan.py: find unlinked units that look DEFERRED (-inline deferred) but are not compiled that way.
Tell: in a normal file the anonymous constants (@N) are numbered in the order their first user appears in .text.
In a deferred file local aggregate initialisers (`u8 t[4] = {..}`, `GXColor c = {..}`) are numbered at parse time
and come out LOWER than constants of earlier functions (seen in several files).
usage: python deferscan.py [--all]   (--all: also units already built with deferred / linked units)
Prints units with at least one inversion: constant @N used first by function #k while a constant with a higher
number is used first by an earlier function."""
from dtkproj import *   # REPO, VER, EXE, HERE; chdir to the project root
import re, sys, os, json

ROOT = REPO
show_all = '--all' in sys.argv

conf = open(os.path.join(ROOT, 'configure.py'), encoding='utf-8').read()
units = {}
for m in re.finditer(r'Object\((\w+|MatchingFor\("[^"]+"\)),\s*"([^"]+)"(.*?)\)', conf):
    status, path, rest = m.group(1), m.group(2), m.group(3)
    units[os.path.splitext(path)[0]] = (status, 'deferred' in rest)

out = []
for unit, (status, deferred) in sorted(units.items()):
    if not show_all and (status.startswith('MatchingFor') or deferred):
        continue
    asm = os.path.join(ROOT, S('build/@V/asm'), unit + '.s')
    if not os.path.exists(asm):
        continue
    fn_idx = -1
    fname = None
    first_use = {}
    names = []
    kinds = {}
    cur = None
    for line in open(asm, encoding='utf-8', errors='replace'):
        mo = re.match(r'\.obj "@(\d+)"', line)
        if mo:
            cur = int(mo.group(1))
            kinds[cur] = set()
            continue
        if line.startswith('.endobj'):
            cur = None
            continue
        if cur is not None:
            md = re.match(r'\s*\.(\w+)\s*(.*)', line)
            if md:
                kinds[cur].add(md.group(1))
                if md.group(1) in ('4byte', '2byte', 'byte') and not re.fullmatch(r'(0x[0-9A-Fa-f]+|-?\d+)(\s*,\s*(0x[0-9A-Fa-f]+|-?\d+))*', md.group(2).strip()):
                    kinds[cur].add('address')
            continue
        if line.startswith('.fn '):
            fn_idx += 1
            fname = line.split()[1].rstrip(',')
            names.append(fname)
            continue
        if fn_idx < 0 or line.startswith('.obj') or line.startswith('.endobj'):
            continue
        for c in re.findall(r'"@(\d+)"', line):
            c = int(c)
            if c not in first_use:
                first_use[c] = fn_idx
    if len(first_use) < 2:
        continue
    # inversion: constant a < b but first_use[a] > first_use[b]
    inv = []
    items = sorted(first_use.items())
    for i, (a, fa) in enumerate(items):
        # only aggregate initialisers (raw words/bytes), not floats/doubles/strings
        if not kinds.get(a) or kinds[a] & {'float', 'double', 'string', 'asciz', 'address'}:
            continue
        later = [(b, fb) for b, fb in items[i + 1:] if fb < fa]
        if later:
            inv.append((a, names[fa], later[0][0], names[later[0][1]]))
    if inv:
        out.append((unit, status, deferred, inv))

for unit, status, deferred, inv in out:
    a, fa, b, fb = inv[0]
    print('%-55s %-12s %s inversions=%d  e.g. @%d (first in %s) < @%d (first in %s)' % (
        unit, 'linked' if status.startswith('Matching') else status, 'deferred' if deferred else '', len(inv), a, fa, b, fb))
print(len(out), 'units')
