"""protos.py - whole-program prototypes for m2c, inferred from every function and all its callers.

usage (project root): python tools/draft/protos.py [--iterations 1] [-j 10] [--m2c DIR]

m2c sees one function at a time. `g(f(arg0), f2(arg0))` becomes `t = f2(); f(arg0); g(t);` when m2c does not know that the
callees take arg0 in r3 and return something: one register off, at 99.8%, in dozens of functions. With the callees'
prototypes as m2c --context the draft is right.

  parameters  register liveness over the whole program: an argument register is a parameter when some path reads it
              before writing it, including "only passed on" to a callee (iterated until nothing changes)
  result      a callee returns something when any caller reads r3 / f1 after the call; a result that nobody can have
              set is dropped again
  types       as m2c sees them without a context (--iterations passes, 0 = all s32 / f32)
Variadic functions are recognised by their register save area and left out, except the few in VARARGS.
Writes build/scratch/redraft/protos.json {name: [return type, [parameter types]]}, read by redraft.py.
Needs the target asm of every unit (build/<VER>/asm). Reads only; nothing in src/ changes.
"""
import argparse
import collections
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import unitlib as U   # noqa: E402
from unitlib import VER   # noqa: E402

OUT = 'build/scratch/redraft'
PROTOS = OUT + '/protos.json'


def find_m2c():
    """m2c.py from --m2c DIR or $M2C (a checkout of m2c, or the path of m2c.py)"""
    p = sys.argv[sys.argv.index('--m2c') + 1] if '--m2c' in sys.argv[:-1] else os.environ.get('M2C', '')
    if os.path.isdir(p):
        p = os.path.join(p, 'm2c.py')
    if not os.path.isfile(p):
        if any(a in ('-h', '--help') for a in sys.argv):
            return ''
        sys.exit('m2c not found: pass --m2c DIR or set M2C (a checkout of m2c)')
    return os.path.abspath(p)


M2C = find_m2c()
VARARGS = {'sprintf': 's32 sprintf(void *, void *, ...);', 'snprintf': 's32 snprintf(void *, s32, void *, ...);',
           'printf': 's32 printf(void *, ...);', 'fprintf': 's32 fprintf(void *, void *, ...);',
           'OSReport': 'void OSReport(void *, ...);', 'OSPanic': 'void OSPanic(void *, s32, void *, ...);',
           'DBPrintf': 'void DBPrintf(void *, ...);'}
HEADER = '''typedef signed char s8;
typedef unsigned char u8;
typedef signed short s16;
typedef unsigned short u16;
typedef signed long s32;
typedef unsigned long u32;
typedef signed long long s64;
typedef unsigned long long u64;
typedef float f32;
typedef double f64;
'''
BASE = {'s8', 'u8', 's16', 'u16', 's32', 'u32', 's64', 'u64', 'f32', 'f64', 'void'}


def norm(t):
    t = re.sub(r'\b(const|struct|unsigned|signed)\b', '', t).strip()
    if '*' in t or '(' in t or '[' in t:
        return 'void *'
    return t if t in BASE else {'M2C_UNK': 's32', 'M2C_UNK8': 's8', 'M2C_UNK16': 's16', 'M2C_UNK32': 's32', 'M2C_UNK64': 's64',
                               'BOOL': 's32', 'int': 's32', 'long': 's32', 'char': 's8', 'float': 'f32', 'double': 'f64'}.get(t, 's32')


def split_params(s):
    out, d, cur = [], 0, ''
    for c in s:
        d += c in '([' and 1 or 0
        d -= c in ')]' and 1 or 0
        if c == ',' and d == 0:
            out.append(cur)
            cur = ''
        else:
            cur += c
    if cur.strip():
        out.append(cur)
    return [x.strip() for x in out]


def param_type(p):
    m = re.match(r'^(.*?)(\w+)$', p)
    if m and m.group(1).strip():
        return norm(m.group(1)), m.group(2)
    return norm(p), ''


def context(names, protos):
    out = [HEADER]
    for n in sorted(names):
        if n in VARARGS:
            out.append(VARARGS[n])
        elif n in protos and re.match(r'^[A-Za-z_]\w*$', n) and n not in ('memcpy', 'memset'):
            r, ps = protos[n]
            out.append('%s%s%s(%s);' % (r, '' if r.endswith('*') else ' ', n, ', '.join(ps) or 'void'))
    return '\n'.join(out) + '\n'


def unit_asm(unit, outdir):
    """cleaned asm + jump tables of a unit (redraft.M2cInput), and the functions it defines / calls"""
    import redraft   # here: redraft imports this module
    inp = redraft.M2cInput(unit)
    own = re.findall(r'^\.fn ([^,\s]+),', inp.text, re.M)
    calls = set(re.findall(r'\bb[l]?\s+([A-Za-z_]\w+)', inp.text))
    return inp, own, calls


def run_unit(item):
    unit, protos = item
    try:
        inp, own, calls = unit_asm(unit, OUT)
    except OSError:
        return {}, {}
    ctx = inp.dir + '/pctx.c'
    with open(ctx, 'w', encoding='utf-8') as f:
        f.write(context(set(own) | calls, protos))
    heads, uses = {}, collections.defaultdict(list)

    def go(fns):
        cmd = [sys.executable, M2C, '--target', 'ppc-mwcc-c', '--valid-syntax', '--globals=used', '--no-cache',
               '--context', ctx]
        for f in fns:
            cmd += ['-f', f]
        try:
            r = subprocess.run(cmd + [inp.asm, inp.jt] + inp.pool_files(), capture_output=True, text=True,
                               encoding='utf-8', errors='replace', timeout=400)
        except subprocess.TimeoutExpired:
            return
        for m in re.finditer(r'^([A-Za-z_][\w ]*?[ *]+)(\w+)\((.*)\) \{$', r.stdout, re.M):
            heads[m.group(2)] = (m.group(1).strip(), m.group(3))
        for m in re.finditer(r'^(?:static )?([A-Za-z_][\w ]*?[ *]+)(\w+)\(([^;{}]*)\);', r.stdout, re.M):
            uses[m.group(2)].append(m.group(1).strip())
    go(own)
    return heads, uses



# ---- register liveness over the whole program -----------------------------------------------------------------------------
ARG_I = ['r%d' % i for i in range(3, 11)]
ARG_F = ['f%d' % i for i in range(1, 9)]
VOLATILE = frozenset(['r0', 'r11', 'r12', 'f0'] + ARG_I + ['f%d' % i for i in range(1, 14)])
REG = re.compile(r'\b([rf]\d+)\b')
NODEF = re.compile(r'^(st|cmp|fcmp|psq_st|mt|b|dcb|icb|sync|isync|tw|eieio|crclr|crset|cror|crxor|rfi|sc$|nop)')


def parse_unit(path):
    """{function: [(label or None, op, operands)]}"""
    out, cur, lab = {}, None, None
    for l in open(path, encoding='utf-8'):
        m = re.match(r'^\.fn ([^,\s]+),', l)
        if m:
            cur, lab = [], None
            out[m.group(1)] = cur
            continue
        if l.startswith('.endfn'):
            cur = None
            continue
        if cur is None:
            continue
        m = re.match(r'^(\.L_\w+):', l)
        if m:
            lab = m.group(1)
            continue
        m = re.match(r'^/\*.*?\*/\s+(\S+)\s*(.*?)\s*$', l)
        if m:
            cur.append((lab, m.group(1), m.group(2)))
            lab = None
    return out


def use_def(op, a):
    regs = REG.findall(re.sub(r'@\w+', '', a))
    if not regs:
        return (), ()
    base = op.rstrip('.+-')
    if base in ('lmw', 'stmw'):
        lo = int(regs[0][1:])
        rng = tuple('r%d' % i for i in range(lo, 32))
        return ((regs[1],), rng) if base == 'lmw' else (rng + (regs[1],), ())
    if NODEF.match(base):
        defs = (regs[-1],) if base.startswith('st') and 'u' in base[2:] and len(regs) > 1 and base != 'sthbrx' else ()
        return tuple(regs), defs
    uses = list(regs[1:])
    defs = [regs[0]]
    if base.startswith('rlwimi'):
        uses.append(regs[0])
    if re.match(r'^l\w+u(x)?$', base) and base not in ('lfiwax',) and len(regs) > 1:
        defs.append(regs[-1] if not base.endswith('x') else regs[1])
    if op.startswith('xor') and len(regs) == 3 and regs[1] == regs[2] == regs[0]:
        uses = []
    return tuple(uses), tuple(defs)


class Fn:
    def __init__(self, name, ins):
        self.name = name
        self.code = []     # (kind, callee, uses, defs, succ)
        labs = {l: i for i, (l, _, _) in enumerate(ins) if l}
        n = len(ins)
        self.callees = set()
        va = 0
        for i, (_, op, a) in enumerate(ins):
            nxt = [i + 1] if i + 1 < n else []
            b = op.rstrip('+-')
            if b == 'bl':
                c = a.split('@')[0]
                if re.match(r'^_(save|rest)(gpr|fpr)_|^__(save|restore)_', c):
                    self.code.append(('n', None, frozenset(), frozenset(), nxt))
                else:
                    self.callees.add(c)
                    self.code.append(('c', c, frozenset(), VOLATILE, nxt))
            elif b in ('bctrl', 'blrl'):
                self.code.append(('p', None, frozenset(), VOLATILE, nxt))
            elif b == 'blr':
                self.code.append(('r', None, frozenset(), frozenset(), []))
            elif b == 'bctr':
                self.code.append(('j', None, frozenset(), frozenset(), list(labs.values())))
            elif b.startswith('b') and b not in ('bl',):
                tgt = a.split(',')[-1].strip()
                if tgt in labs:
                    self.code.append(('n', None, frozenset(), frozenset(), [labs[tgt]] + ([] if b == 'b' else nxt)))
                elif b == 'b' and re.match(r'^[A-Za-z_]', tgt):     # tail call
                    self.callees.add(tgt)
                    self.code.append(('t', tgt, frozenset(), VOLATILE, []))
                elif b.endswith('lr'):
                    self.code.append(('r', None, frozenset(), frozenset(), nxt))
                else:
                    self.code.append(('n', None, frozenset(), frozenset(), nxt))
            else:
                u, d = use_def(op, a)
                if op in ('stfd', 'psq_st') and 'f1' in a.split(',')[0].split() and not re.search(r'\(r1\)', a):
                    va += 7   # saves the FPRs of a thread
                if op == 'stfd' and i < 24 and a.startswith('f') and re.match(r'^f[1-8], 0x\w+\(r1\)$', a):
                    va += 1
                self.code.append(('n', None, frozenset(u), frozenset(d), nxt))
        self.varargs = va >= 7
        preds = [[] for _ in range(n)]
        for i, c in enumerate(self.code):
            for s in c[4]:
                preds[s].append(i)
        self.preds = preds

    def solve(self, sig, ret):
        """(live-in arg registers, {callee: registers of its result that are read})"""
        n = len(self.code)
        live_in = [frozenset()] * n
        work = list(range(n))
        results = collections.defaultdict(set)
        r = ret.get(self.name, '')
        while work:
            i = work.pop()
            k, c, u, d, succ = self.code[i]
            out = frozenset().union(*[live_in[s] for s in succ]) if succ else frozenset()
            if k == 'r':
                out = out | ({'r3'} if 'i' in r else set()) | ({'f1'} if 'f' in r else set())
            elif k == 't':
                out = frozenset(({'r3'} if 'i' in r else set()) | ({'f1'} if 'f' in r else set()))
            if k in 'ct':
                if 'r3' in out:
                    results[c].add('i')
                if 'f1' in out:
                    results[c].add('f')
                u = sig.get(c, frozenset())
            new = (out - d) | u
            if new != live_in[i]:
                live_in[i] = new
                work.extend(self.preds[i])
        return (live_in[0] if n else frozenset()), results


def liveness(units):
    fns = {}
    for u in units:
        for n, ins in parse_unit('build/%s/asm/%s.s' % (VER, u)).items():
            if ins:
                fns[n] = Fn(n, ins)
    callers = collections.defaultdict(set)
    for n, f in fns.items():
        for c in f.callees:
            callers[c].add(n)
    sig, ret, banned = {}, {}, set()
    work = set(fns)
    args = frozenset(ARG_I + ARG_F)
    while work:
        n = work.pop()
        f = fns[n]
        li, res = f.solve(sig, ret)
        # a result register that is live at entry ONLY because of the assumed result is never set: no such result
        r = ret.get(n, '')
        if r:
            li0, _ = f.solve(sig, dict(ret, **{n: ''}))
            for kind, reg in (('i', 'r3'), ('f', 'f1')):
                if kind in r and reg in li and reg not in li0:
                    banned.add((n, kind))
            keep = ''.join(k for k in r if (n, k) not in banned)
            if keep != r:
                ret[n] = keep
                li, res = f.solve(sig, ret)
        s = li & args
        if f.varargs:
            s = frozenset()
        if s != sig.get(n, frozenset()):
            sig[n] = s | sig.get(n, frozenset())
            work |= callers[n] & set(fns)
        for c, kinds in res.items():
            old = ret.get(c, '')
            new = ''.join(sorted(set(old) | {k for k in kinds if (c, k) not in banned}))
            if new != old:
                ret[c] = new
                if c in fns:
                    work.add(c)
    work, sig = set(fns), {}
    while work:
        n = work.pop()
        f = fns[n]
        li, _ = f.solve(sig, ret)
        s = frozenset() if f.varargs else li & args
        if s - sig.get(n, frozenset()):
            sig[n] = s | sig.get(n, frozenset())
            work |= callers[n] & set(fns)
    return fns, sig, ret


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--iterations', type=int, default=1, help='m2c passes for the parameter types')
    ap.add_argument('-j', type=int, default=10)
    ap.add_argument('--m2c', help='m2c checkout (default: $M2C)')
    a = ap.parse_args()
    units = sorted(u for u in U.units() if os.path.exists('build/%s/asm/%s.s' % (VER, u)))
    fns, sig, ret = liveness(units)
    print('%d functions: %d with parameters, %d with a result somebody reads' % (
        len(fns), sum(1 for n in fns if sig.get(n)), sum(1 for n in fns if ret.get(n))), flush=True)
    heads = {}
    protos = {}

    def build():
        for n, f in fns.items():
            if f.varargs:
                continue
            s = sig.get(n, frozenset())
            ni = max([int(r[1:]) - 2 for r in s if r[0] == 'r'] or [0])
            nf = max([int(r[1:]) for r in s if r[0] == 'f'] or [0])
            h = heads.get(n)
            order = [x for x in (h[1] if h else [])]
            ints = [x for x in order if x not in ('f32', 'f64')]
            fls = [x for x in order if x in ('f32', 'f64')]
            out, ii, fi = [], 0, 0
            for x in order:          # the order m2c saw, as far as the counts allow
                if x in ('f32', 'f64'):
                    if fi < nf:
                        out.append(x)
                        fi += 1
                elif ii < ni and x not in ('s64', 'u64'):
                    out.append(x)
                    ii += 1
            out += ['s32'] * (ni - ii) + ['f32'] * (nf - fi)
            r = ret.get(n, '')
            hr = h[0] if h else 'void'
            if not r:
                rt = hr if hr != 'void' else 'void'
            elif 'f' in r and 'i' not in r:
                rt = hr if hr in ('f32', 'f64') else 'f32'
            else:
                rt = hr if hr not in ('void', 'f32', 'f64') else 's32'
            protos[n] = [rt, out]
    for it in range(a.iterations):   # parameter / result TYPES as m2c sees them without a context
        usev = collections.defaultdict(collections.Counter)
        with ThreadPoolExecutor(a.j) as ex:
            for hs, uses in ex.map(run_unit, [(u, {}) for u in units]):
                for n, (r, ps) in hs.items():
                    params = []
                    for q in split_params(ps):
                        if q in ('void', '...') or not q:
                            continue
                        ty, nm = param_type(q)
                        if not nm.startswith('arg_sp'):
                            params.append(ty)
                    heads[n] = [norm(re.sub(r'\bstatic\b', '', r)), params]
        before = json.dumps(protos, sort_keys=True)
        build()
        print('types pass %d: %d prototypes, changed: %s' % (it + 1, len(protos), before != json.dumps(protos, sort_keys=True)), flush=True)
    build()
    U.save_json(PROTOS, protos, indent=0, sort_keys=True)
    print('%d prototypes -> %s' % (len(protos), PROTOS))


if __name__ == '__main__':
    main()
