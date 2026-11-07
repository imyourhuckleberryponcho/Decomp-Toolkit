"""regwhere.py - WHICH SOURCE LINE makes the value that sits in the wrong register.

regplan.py names misplaced *variables*, but most register-only near misses are unnamed values (temporaries, inline
helper locals, loop-hoisted values) that the debug info does not list. This compiles the unit with `-sym on`, reads
the DWARF-1 .line table (address -> source line), lines our instructions up with the target's and, for every
register that differs, prints the instruction that DEFINES our register and the source line it came from.

usage (from the project root): python regwhere.py <unit> <func> [--src FILE]
Line numbers come from the main source file (inline code from headers reports the call site's line).
"""
from dtkproj import *   # REPO, VER, EXE, HERE; chdir to the project root
import difflib, os, re, struct, subprocess, sys
from collections import defaultdict

os.chdir(REPO)
H = os.path.dirname(os.path.abspath(__file__))
OBJDUMP = os.path.abspath('build/binutils/powerpc-eabi-objdump' + EXE)
unit, func = sys.argv[1], sys.argv[2]
src = sys.argv[sys.argv.index('--src') + 1] if '--src' in sys.argv else \
    next(p for p in ('src/%s.cpp' % unit, 'src/%s.c' % unit) if os.path.exists(p))
TAG = 'regwhere%d' % os.getpid()
args = [sys.executable, os.path.join(H, 'fcheck.py'), unit, '--tag', TAG, '--extra', '-sym on', '--diff', func]
if '--src' in sys.argv:
    args += ['--src', src]
out = subprocess.run(args, capture_output=True, text=True)
d = 'build/scratch/fcheck/' + TAG
obj = os.path.join(d, os.path.splitext(os.path.basename(src))[0] + '.o')
if not os.path.exists(obj):
    sys.exit('compile failed:\n' + (out.stdout + out.stderr)[-1500:])
norm = lambda n: re.sub(r'@[0-9]+', '@N', n)


def body(path):
    res, on = [], False
    for ln in open(path, encoding='utf-8', errors='replace'):
        ln = ln.rstrip('\n')
        if ln.startswith('.fn '):
            on = norm(ln.split()[1].rstrip(',').strip('"')) == norm(func)
            continue
        if on:
            if ln.startswith('.endfn'):
                break
            ln = re.sub(r'^/\* \S+ \S+\s+(\S\S ){4}\*/\s*', '', ln).strip()
            ln = re.sub(r'\.L_[0-9A-F]+', '.L', ln)
            ln = re.sub(r'"?@[0-9]+"?', '@N', ln)
            if ln and not ln.startswith('.L'):
                res.append(ln)
    return res


T, M = body(os.path.join(d, 'T.s')), body(os.path.join(d, 'M.s'))
if not M:
    sys.exit('function not found in our object')

# ---- our function's start address in .text and the .line table
syms = subprocess.run([OBJDUMP, '-t', obj], capture_output=True, text=True).stdout
fstart = None
for l in syms.splitlines():
    p = l.split()
    if len(p) >= 6 and norm(p[-1]) == norm(func) and '.text' in l:
        fstart = int(p[0], 16)
        break
if fstart is None:
    sys.exit('symbol not found')
raw = subprocess.run([OBJDUMP, '-s', '-j', '.line', obj], capture_output=True, text=True).stdout
data = bytearray()
for l in raw.splitlines():
    m = re.match(r'^ ([0-9a-f]{4,}) ((?:[0-9a-f]{2,8} ?){1,4})', l)
    if m:
        data += bytes.fromhex(m.group(2).replace(' ', ''))
addr2line = {}
i = 0
while i + 8 <= len(data):
    ln_, base = struct.unpack('>II', data[i:i + 8])   # base is 0 + .text relocation in an unlinked object
    j = i + 8
    while j + 10 <= i + ln_:
        line, pos, delta = struct.unpack('>IHI', data[j:j + 10])
        addr2line[base + delta] = line
        j += 10
    i += max(ln_, 8)
addrs = sorted(addr2line)


def line_of(addr):
    import bisect
    k = bisect.bisect_right(addrs, addr) - 1
    return addr2line[addrs[k]] if k >= 0 else None


srclines = open(src, encoding='latin-1').read().split('\n')
# our instruction index -> address: M.s lists every instruction of the function in order
nz = lambda xs: [re.sub(r'\b[rf]\d+\b', 'R', x) for x in xs]
sm = difflib.SequenceMatcher(None, nz(T), nz(M), autojunk=False)
NODEST = ('st', 'cmp', 'b', 'mt', 'fcmp', 'tw', 'sync', 'dcb', 'icb', 'isync')


def dest(ins):
    op = ins.split()[0]
    if op.startswith(NODEST) and not op.startswith(('subf', 'srawi')):
        return None
    m = re.match(r'\S+\s+([rf]\d+)', ins)
    return m.group(1) if m else None


report = defaultdict(lambda: [0, None, None])
for op, i1, i2, j1, j2 in sm.get_opcodes():
    if op != 'equal':
        continue
    for k in range(i2 - i1):
        t, m = T[i1 + k], M[j1 + k]
        if t == m:
            continue
        for a, b in zip(re.findall(r'\b[rf]\d+\b', m), re.findall(r'\b[rf]\d+\b', t)):
            if a == b:
                continue
            # the instruction that last defined our register a
            jj = j1 + k
            dj = next((x for x in range(jj, -1, -1) if dest(M[x]) == a and (x < jj or M[x].split()[1].rstrip(',') == a)), None)
            key = (a, b, dj)
            report[key][0] += 1
for (a, b, dj), (n, _, _) in sorted(report.items(), key=lambda kv: (kv[0][2] is None, kv[0][2] or 0)):
    if dj is None:
        print('%-4s -> %-4s x%-3d defined before the function body (parameter / saved)' % (a, b, n))
        continue
    addr = fstart + 4 * dj
    ln = line_of(addr)
    txt = srclines[ln - 1].strip() if ln and ln <= len(srclines) else '?'
    print('%-4s -> %-4s x%-3d def #%-4d %-28s line %-5s %s' % (a, b, n, dj, M[dj][:28], ln, txt[:90]))
