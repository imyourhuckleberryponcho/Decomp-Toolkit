"""sdascan.py: data symbols whose ADDRESSING MODE differs between the target and our source.

The target addresses small-data globals (<= 8 bytes, sdata/sbss) with `sym@sda21(r0)` and everything else with
`lis rX, sym@ha` + `sym@l(rX)`. A source that declares a big/array object as a plain scalar (`extern f32 __float_nan;`
instead of `extern f32 __float_nan[];`) makes MWCC emit the sda21 form - fcheck's CALLS/CONST checks do not notice.

usage (from the project root): python sdascan.py [--unit SUBSTR]
Checks every src file that has a target asm file: symbols the target reaches only via @ha/@l whose source
declaration is a plain scalar of a small type (and the reverse: target sda21, source declares an array).
"""
from dtkproj import *   # REPO, VER, EXE, HERE; chdir to the project root
import os, re, sys

os.chdir(REPO)
ASM = S('build/@V/asm/')
only = sys.argv[sys.argv.index('--unit') + 1] if '--unit' in sys.argv else None
SMALL = r'(?:f32|f64|float|double|s8|u8|s16|u16|s32|u32|int|long|short|char|bool|BOOL|unsigned\s+\w+|signed\s+\w+|\w+\s*\*)'
hits = 0
for root, _, files in os.walk('src/game'):
    for fn in files:
        if not fn.endswith(('.c', '.cpp')):
            continue
        src = os.path.join(root, fn).replace('\\', '/')
        unit = src[4:].rsplit('.', 1)[0]
        if only and only not in unit:
            continue
        asm = ASM + unit + '.s'
        if not os.path.exists(asm):
            continue
        a = open(asm, encoding='latin-1').read()
        s = open(src, encoding='latin-1').read()
        ha = set(re.findall(r'\b([A-Za-z_]\w*)@ha\b', a))
        sda = set(re.findall(r'\b([A-Za-z_]\w*)@sda21\b', a))
        defined = set(re.findall(r'^\.(?:obj|fn) (\w+)', a, re.M))
        for sym in sorted(ha - sda - defined):
            m = re.search(r'^\s*extern\s+(?:volatile\s+)?(?:const\s+)?' + SMALL + r'\s*\b' + re.escape(sym) + r'\s*;', s, re.M)
            if m:
                hits += 1
                print('%-60s %-28s target @ha/@l, source scalar: %s' % (unit, sym, m.group(0).strip()))
        for sym in sorted(sda - ha - defined):
            m = re.search(r'^\s*extern\s+[\w\s\*]+\b' + re.escape(sym) + r'\s*\[\s*\]\s*;', s, re.M)
            if m:
                hits += 1
                print('%-60s %-28s target sda21, source array: %s' % (unit, sym, m.group(0).strip()))
print('%d suspicious declarations' % hits)
