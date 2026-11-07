"""labgrid.py - register/codegen lab: which source differences change the machine code?

usage (from the project root): python labgrid.py <template.cpp> [--unit U] [--flags "..."] [-j 8]
                   [--func NAME] [--want "regex"]
  The template is a small C/C++ file. Every <<<a|||b|||c>>> is a set of alternatives; all combinations are compiled
  (with the flags of --unit, required). For each function the variants are GROUPED by identical
  code, and each group shows its instructions that differ from the first group's (so one run answers "which of
  these forms actually matter?").
  --want "regex": mark groups whose disassembly matches (a regex).
--vars: instead of code groups, print every variable's register per variant (debug info).
Output also in build/scratch/labgrid/<template name>.txt.
"""
from dtkproj import *   # REPO, VER, EXE, HERE; chdir to the project root
import difflib, itertools, os, re, shlex, subprocess, sys
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

os.chdir(REPO)
a = sys.argv[1:]
tmpl_path = a[0]
opt = lambda n, d=None: a[a.index(n) + 1] if n in a else d
unit = opt('--unit')
if not unit:
    sys.exit('labgrid: pass --unit <unit> (its compiler flags are used)')
J = int(opt('-j', 8))
only = opt('--func')
want = re.compile(opt('--want')) if opt('--want') else None
OUT = 'build/scratch/labgrid'
os.makedirs(OUT, exist_ok=True)
cmd = subprocess.check_output(['ninja', '-t', 'commands', S('build/@V/src/%s.o') % unit], text=True).strip().splitlines()[-1]
argv = shlex.split(cmd.replace('\\', '/'), posix=True)
k = argv.index('-c')
argv = [x for x in argv[:k] if x != '-MMD']
if opt('--mw'):   # another compiler version, e.g. --mw 2.7 (build/compilers/GC/<v>/)
    argv = [re.sub(r'compilers/GC/[^/]+/', 'compilers/GC/%s/' % opt('--mw'), x) for x in argv]
argv[0] = os.path.abspath(argv[0])
if opt('--flags'):
    argv += shlex.split(opt('--flags'))
if tmpl_path.endswith('.c') and '-lang=c' not in argv:
    argv = [x for x in argv if not x.startswith('-lang')] + ['-lang=c']
tmpl = open(tmpl_path, encoding='utf-8').read()
alts = re.findall(r'<<<(.*?)>>>', tmpl, re.S)
choices = [x.split('|||') for x in alts]
combos = list(itertools.product(*[range(len(c)) for c in choices]))
OBJDUMP = os.path.abspath('build/binutils/powerpc-eabi-objdump' + EXE)
ext = os.path.splitext(tmpl_path)[1]
VARS = '--vars' in a
DTK = os.path.abspath('build/tools/dtk' + EXE)
if VARS:
    argv += ['-sym', 'on']


def dwarf_regs(o):
    """{function: [(var, reg)]} for every local/param kept in a register (from -sym on debug info)"""
    out = subprocess.run([DTK, 'dwarf', 'dump', o], capture_output=True, text=True, errors='replace').stdout
    out = re.sub(chr(27) + r'\[[0-9;]*m', '', out)
    res = OrderedDict()
    for m in re.finditer(r'^[^\n]*?(\w+)\(([^\n]*)\) \{\n(.*?)^\}', out, re.S | re.M):
        vs = [(pm.group(1), pm.group(2)) for pm in re.finditer(r'(\w+) /\* ([rf]\d+) \*/', m.group(2))]
        vs += [(lm.group(1), lm.group(2)) for lm in
               re.finditer(r'^\s+[^/\n]+?\s+(\w+)(?:\[[^\]]*\])?; // ([rf]\d+)\s*$', m.group(3), re.M)]
        res[m.group(1)] = vs
    return res


def build(ix_combo):
    ix, combo = ix_combo
    it = iter(combo)
    src = re.sub(r'<<<(.*?)>>>', lambda m: m.group(1).split('|||')[next(it)], tmpl, flags=re.S)
    p = '%s/v%d%s' % (OUT, ix, ext)
    open(p, 'w', encoding='utf-8').write(src)
    o = p[:-len(ext)] + '.o'
    r = subprocess.run(argv + ['-c', p, '-o', o], capture_output=True, text=True, errors='replace')
    if r.returncode:
        return combo, None
    dis = subprocess.run([OBJDUMP, '-d', '-r', o], capture_output=True, text=True).stdout
    funcs, cur = OrderedDict(), None
    for l in dis.splitlines():
        m = re.match(r'^[0-9a-f]+ <(.+)>:', l)
        if m:
            cur = m.group(1)
            funcs[cur] = []
            continue
        m = re.match(r'^\s+[0-9a-f]+:\s+(?:[0-9a-f]{2} ){4}\s*(.*)$', l)
        if m and cur:
            ins = re.sub(r'\s+', ' ', re.sub(r'<[^>]*>', '', m.group(1))).strip()
            ins = re.sub(r'^(b\w*) [0-9a-f]+', r'\1 L', ins)
            funcs[cur].append(ins)
        m = re.match(r'^\s+[0-9a-f]+: R_PPC_\w+\s+(\S+)', l)
        if m and cur and funcs[cur]:
            funcs[cur][-1] += ' ' + re.sub(r'\+0x[0-9a-f]+$', '', m.group(1))
    if VARS:
        funcs['__vars__'] = dwarf_regs(o)
    return combo, funcs


with ThreadPoolExecutor(J) as ex:
    res = list(ex.map(build, enumerate(combos)))
lines = []
P = lambda s: (print(s), lines.append(s))
label = lambda combo: ' / '.join(choices[i][c].strip().replace('\n', ' ')[:40] for i, c in enumerate(combo)) or '(base)'
fail = [c for c, f in res if f is None]
if fail:
    P('compile failed: ' + '; '.join(label(c) for c in fail[:6]))
ok = [(c, f) for c, f in res if f is not None]
if not ok:
    sys.exit('nothing compiled')
if VARS:
    for c, f in ok:
        for fn, vs in f['__vars__'].items():
            if only and only not in fn:
                continue
            P('%-70s %s: %s' % (label(c)[:70], fn, '  '.join('%s=%s' % v for v in vs)))
    open('%s/%s.txt' % (OUT, os.path.splitext(os.path.basename(tmpl_path))[0]), 'w').write(chr(10).join(lines) + chr(10))
    sys.exit(0)
for fn in ok[0][1]:
    if only and only not in fn:
        continue
    groups = OrderedDict()
    for c, f in ok:
        groups.setdefault(tuple(f.get(fn, [])), []).append(c)
    P('== %s: %d variant(s) -> %d distinct' % (fn, len(ok), len(groups)))
    first = None
    for gi, (code, cs) in enumerate(groups.items()):
        mark = ''
        if want and want.search('\n'.join(code)):
            mark = '  <== WANT'
        P('  [%d] %d insns  %s%s' % (gi, len(code), ' | '.join(label(c) for c in cs[:4]) + (' ...' if len(cs) > 4 else ''), mark))
        if first is None:
            first = code
            continue
        sm = difflib.SequenceMatcher(None, first, code, autojunk=False)
        for op, i1, i2, j1, j2 in sm.get_opcodes():
            if op == 'equal':
                continue
            for x in first[i1:i2][:4]:
                P('        - ' + x)
            for x in code[j1:j2][:4]:
                P('        + ' + x)
open('%s/%s.txt' % (OUT, os.path.splitext(os.path.basename(tmpl_path))[0]), 'w').write('\n'.join(lines) + '\n')
