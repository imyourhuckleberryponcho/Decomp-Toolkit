"""regplan.py - which VARIABLE sits in the wrong register, and a declaration order that should fix it.

How: compiles the unit with `-sym on` (MWCC debug info; the generated code is byte-identical) and reads every local's
register from the DWARF (dtk dwarf dump). Then lines our instructions up with the original's (same instruction,
other registers) and counts which target register each of our registers turns into. Result per variable:
  `again  ours r25  ->  target r24 (31x)`.
Register-colouring rules measured (guide/02-compiler-model.md): callee-saved = first-declared local gets r31,
volatile = first-declared gets the lowest; inline expansions and loop-hoisted values rank before declared locals.
So for callee-saved variables the wanted declaration order is: highest target register first.

usage (from the project root): python regplan.py <unit> <func> [--try] [-j 6]
  --try : also compile declaration-order variants built from the plan (moves of each misplaced variable's
          declaration among the function-level declarations, plus the full suggested order) and print the best.
          Never edits src/ (variants are compiled from build/scratch/regplan/).
"""
from dtkproj import *   # REPO, VER, EXE, HERE; chdir to the project root
import difflib, os, re, subprocess, sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

os.chdir(REPO)
H = os.path.dirname(os.path.abspath(__file__))
FCHECK = os.path.join(H, 'fcheck.py')
DTK = os.path.abspath('build/tools/dtk' + EXE)
unit, func = sys.argv[1], sys.argv[2]
TRY = '--try' in sys.argv
J = int(sys.argv[sys.argv.index('-j') + 1]) if '-j' in sys.argv else 6
SAVED = {'r%d' % i for i in range(13, 32)}


def fcheck(tag, src=None, extra=None, diff=False):
    args = [sys.executable, FCHECK, unit, '--tag', tag]
    if src:
        args += ['--src', src]
    if extra:
        args += ['--extra', extra]
    if diff:
        args += ['--diff', func]
    else:
        args += ['--all']
    r = subprocess.run(args, capture_output=True, text=True)
    return r.stdout + r.stderr


def body(path, name):
    out, on = [], False
    for ln in open(path, encoding='utf-8', errors='replace'):
        ln = ln.rstrip('\n')
        if ln.startswith('.fn '):
            on = ln.split()[1].rstrip(',').strip('"') == name
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


def objdir(tag):
    return os.path.join('build/scratch/fcheck', tag)


def reg_pairs(t, m):
    """aligned instruction pairs that differ only in registers -> Counter((ours, target))"""
    nz = lambda xs: [re.sub(r'\b[rf]\d+\b', 'R', x) for x in xs]
    sm = difflib.SequenceMatcher(None, nz(t), nz(m), autojunk=False)
    pairs = Counter()
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op != 'equal':
            continue
        for k in range(i2 - i1):
            tr = re.findall(r'\b[rf]\d+\b', t[i1 + k])
            mr = re.findall(r'\b[rf]\d+\b', m[j1 + k])
            for a, b in zip(mr, tr):
                pairs[(a, b)] += 1
    return pairs


def diff_count(t, m):
    sm = difflib.SequenceMatcher(None, t, m, autojunk=False)
    return sum(max(i2 - i1, j2 - j1) for op, i1, i2, j1, j2 in sm.get_opcodes() if op != 'equal')


def dwarf_vars(obj):
    out = subprocess.run([DTK, 'dwarf', 'dump', obj], capture_output=True, text=True, errors='replace').stdout
    out = re.sub(r'\x1b\[[0-9;]*m', '', out)
    plain = func.split('__')[0] if '__' in func else func
    # C++ methods: "Cls::method(" ; C: "func("
    cls = re.match(r'(\w+?)__(\d+)(\w+?)F', func)
    names = [plain]
    if cls:
        n = int(cls.group(2))
        names.insert(0, cls.group(3)[:n] + '::' + plain)
    for nm in names:
        m = re.search(r'^[^\n]*\b' + re.escape(nm) + r'\(([^\n]*)\) \{\n(.*?)^\}', out, re.S | re.M)
        if m:
            break
    else:
        return []
    vs = []
    for pm in re.finditer(r'([\w*&<>: ]+?)\s+(\w+) /\* (r\d+|f\d+) \*/', m.group(1)):
        vs.append(('param', pm.group(2), pm.group(3)))
    for lm in re.finditer(r'^\s+([^/\n]+?)\s+(\w+)(?:\[[^\]]*\])?; // (r\d+|f\d+)\s*$', m.group(2), re.M):
        vs.append(('local', lm.group(2), lm.group(3)))
    return vs


# ---------------------------------------------------------------- analysis
o = fcheck('regplan_sym', extra='-sym on')
mo = re.search(r'obj=(\S+)', o)
if not mo:
    sys.exit('compile failed:\n' + o[-1500:])
obj = mo.group(1)
fcheck('regplan_sym', extra='-sym on', diff=True)
t = body(os.path.join(objdir('regplan_sym'), 'T.s'), func)
m = body(os.path.join(objdir('regplan_sym'), 'M.s'), func)
pairs = reg_pairs(t, m)
base_diff = diff_count(t, m)
want = defaultdict(Counter)
for (a, b), n in pairs.items():
    want[a][b] += n
vars_ = dwarf_vars(obj)
print('%s: %d differing instruction lines; %d variables with a register' % (func, base_diff, len(vars_)))
mis = []
for kind, name, reg in vars_:
    if reg not in want:
        continue
    best, n = want[reg].most_common(1)[0]
    tot = sum(want[reg].values())
    flag = '' if best == reg else '   <-- wants %s' % best
    print('  %-6s %-12s ours %-4s -> target %s (%d/%d)%s' % (kind, name, reg, best, n, tot, flag))
    if best != reg and kind == 'local':
        mis.append((name, reg, best))
moved = [(a, b, n) for (a, b), n in pairs.most_common() if a != b]
print('register moves (ours -> target, count): ' + ', '.join('%s->%s %d' % x for x in moved[:12]))
# where: the call just before each differing instruction (register owners that the debug info does not name are
# usually inline-helper locals or loop-hoisted values - the fix is then a helper / a named local, not a reorder)
nzl = lambda xs: [re.sub(r'\b[rf]\d+\b', 'R', x) for x in xs]
sm = difflib.SequenceMatcher(None, nzl(t), nzl(m), autojunk=False)
where = Counter()
for op, i1, i2, j1, j2 in sm.get_opcodes():
    if op != 'equal':
        continue
    for k in range(i2 - i1):
        if t[i1 + k] != m[j1 + k]:
            prev = next((t[x].split()[1] for x in range(i1 + k, -1, -1) if t[x].startswith('bl ')), '(start)')
            where['after %s (instr %d)' % (prev, (i1 + k) // 10 * 10)] += 1
print('where: ' + '; '.join('%s x%d' % kv for kv in sorted(where.items(), key=lambda kv: kv[0])[:12]))
if not TRY:
    sys.exit(0)

# ---------------------------------------------------------------- try declaration orders
src_path = next(p for p in ('src/%s.cpp' % unit, 'src/%s.c' % unit) if os.path.exists(p))
src = open(src_path, encoding='utf-8').read()
plain = func.split('__')[0] if '__' in func else func
cls = re.match(r'(\w+?)__(\d+)(\w+?)F', func)
head = (cls.group(3)[:int(cls.group(2))] + '::' + plain) if cls else plain
fm = re.search(r'^[^\n;]*\b' + re.escape(head) + r'\s*\([^;{]*\)\s*(?:const\s*)?\{', src, re.M)
if not fm:
    sys.exit('definition not found in ' + src_path)
start = fm.end()
depth, i = 1, start
while depth:
    depth += {'{': 1, '}': -1}.get(src[i], 0)
    i += 1
fbody = src[start:i - 1]
lines = fbody.split('\n')
DECL = re.compile(r'^(    )((?:const\s+)?[A-Za-z_][\w:]*(?:\s*\*+)?)\s+(\**[A-Za-z_]\w*)(\s*=\s*[^;]+)?;\s*$')
top = [k for k, l in enumerate(lines) if DECL.match(l)]   # function-level (4-space indent) declarations


def with_order(order_names):
    """move the function-level declarations of order_names (in that order) to the first declaration slot;
    initialisers stay where they were as plain assignments"""
    L = lines[:]
    decl_lines = []
    for name in order_names:
        for k in top:
            dm = DECL.match(L[k] or '')
            if dm and dm.group(3).lstrip('*') == name:
                ty, nm, init = dm.group(2), dm.group(3), dm.group(4)
                decl_lines.append('    %s %s;' % (ty, nm))
                L[k] = ('    %s%s;' % (nm.lstrip('*'), init.rstrip())) if init else None
                break
    if not decl_lines:
        return None
    first = min(top)
    out = L[:first] + decl_lines + L[first:]
    return '\n'.join(l for l in out if l is not None)


cands = []
mis_names = [n for n, _, _ in mis]
if mis_names:
    # the full plan: callee-saved locals sorted by wanted register (high first), volatile by wanted register (low first)
    loc = [(n, r) for k, n, r in vars_ if k == 'local']
    target_of = {n: want[r].most_common(1)[0][0] if r in want else r for n, r in loc}
    saved = sorted([n for n, r in loc if target_of[n] in SAVED], key=lambda n: -int(target_of[n][1:]))
    vol = sorted([n for n, r in loc if target_of[n] not in SAVED and target_of[n].startswith('r')], key=lambda n: int(target_of[n][1:]))
    for name, order in (('full plan (saved first)', saved + vol), ('saved only', saved), ('volatile only', vol)):
        nb = with_order(order)
        if nb:
            cands.append((name, nb))
    # each misplaced variable alone: to the front / to the back of the declarations
    for n in mis_names:
        nb = with_order([n])
        if nb:
            cands.append(('%s first' % n, nb))
        others = [x for x, _ in loc if x != n]
        nb = with_order(others + [n])
        if nb:
            cands.append(('%s last' % n, nb))
os.makedirs('build/scratch/regplan', exist_ok=True)


def run(ix_c):
    ix, (name, nb) = ix_c
    path = 'build/scratch/regplan/%s_%d%s' % (re.sub(r'\W', '_', func)[:40], ix, os.path.splitext(src_path)[1])
    open(path, 'w', encoding='utf-8').write(src[:start] + nb + src[i - 1:])
    tag = 'regplan_%d' % ix
    out = fcheck(tag, src=path, diff=True)
    if 'COMPILE FAILED' in out or 'rror' in out and 'obj=' not in out:
        return name, None, path
    tt = body(os.path.join(objdir(tag), 'T.s'), func)
    mm = body(os.path.join(objdir(tag), 'M.s'), func)
    return name, diff_count(tt, mm) if mm else None, path


print('\ntrying %d declaration-order variants (base %d differing lines)' % (len(cands), base_diff))
with ThreadPoolExecutor(J) as ex:
    res = list(ex.map(run, enumerate(cands)))
for name, d, path in sorted(res, key=lambda r: (r[1] is None, r[1] or 0)):
    print('  %-30s %s   %s' % (name, 'compile failed' if d is None else '%d lines' % d, path if d is not None and d < base_diff else ''))
