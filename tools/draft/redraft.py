"""redraft.py - draft functions again with m2c and install the new body in src/ when it is better.

usage (project root): python tools/draft/redraft.py [--only UNIT] [--func FN] [--max-pct 100] [--min-gain 0.3] [-j 8]
                                                    [--m2c DIR] [--units REGEX] [--dry] [--fresh]

The first draft of a function is often the problem: searches and permuters cannot repair a function whose body is an
empty stub, whose switch became a chain of ifs, or that lost a third of its statements because they did not compile.
Fix the drafts BEFORE any long search. What this does beyond running m2c once:

  jump tables     the table is taken from the data asm and handed to m2c -> real switch statements
  cror            `cror eq, gt, eq` is spelled the way m2c's >= / <= pattern expects (otherwise M2C_ERROR and ==)
  constants       the .sdata2 pool is handed to m2c -> literals instead of `extern f32` (an extern is reloaded after
                  every call, a literal is not)
  stack arrays    an address-taken stack slot and the slots behind it become ONE array
  byte arithmetic void * becomes u8 *, so pointer + offset means bytes
  prototypes      with protos.json (run protos.py first) every callee's prototype is the m2c context
  repair loop     the compiler's error message picks casts to TRY (kept when the error moves on); a statement is only
                  commented out when nothing helps, and the result must then be clearly better
Every combination (with / without stack arrays, literals, context) is compiled and scored; the best one is kept if the
function gains --min-gain or becomes exact and no exact function of the unit is lost. A score of 100% counts only if
unitlib.strict_exact agrees; otherwise the body is not installed (run redraft again when its neighbours are drafted).

Needs: C units (m2c writes C; C++ units are skipped), m2c's macros visible in the unit (copy m2c_macros.h into include/
and include it in your drafts; units without M2C_FIELD are given the #include), target asm in build/<VER>/asm.
Functions tried on the same current body are skipped (build/scratch/redraft/tried.json; --fresh ignores it).
Log: build/scratch/redraft/redraft.log. The old body is in git. Run ninja afterwards.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import unitlib as U   # noqa: E402
import protos as protolib   # noqa: E402
from unitlib import VER, tool   # noqa: E402
from protos import M2C   # noqa: E402
if not any(x in ('-h', '--help') for x in sys.argv[1:]):
    from dtkproj import unit_command   # noqa: E402

OUT = 'build/scratch/redraft'
MACROS = 'm2c_macros.h'
LOCK = threading.Lock()
VERSION = 3   # bump when the generator changes: everything is tried again
log = U.Log(OUT + '/redraft.log')


# ---- data shared by all units -------------------------------------------------------------------------------------------
def load_data():
    """({jump table name: rows}, path of the collected .sdata2 pool, names defined in it)"""
    tabs, sd2 = {}, []
    root = 'build/%s/asm' % VER
    for d, _, files in os.walk(root):
        for f in files:
            if not f.endswith('.s'):
                continue
            pool_file = f.endswith('_sdata2.s')          # dtk's automatic split of data no unit owns yet
            if not pool_file and not re.search(r'_(data|rodata)\.s$', f):
                continue
            with open(os.path.join(d, f), encoding='utf-8') as fh:
                t = fh.read()
            for m in re.finditer(r'^\.obj (jumptable_[0-9A-F]+),.*?^\.endobj \1$', t, re.M | re.S):
                rows = []
                for x in re.finditer(r'^\s+\.4byte (\S+?)(?:\+0x([0-9A-Fa-f]+))?$', m.group(0), re.M):
                    rows.append((x.group(1), int(x.group(2) or '0', 16)))
                tabs[m.group(1)] = rows
            if pool_file:
                sd2.append(t)
    p = OUT + '/sdata2.s'
    os.makedirs(OUT, exist_ok=True)
    with open(p, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(sd2))
    names = set(re.findall(r'^\.obj "?([^",\s]+)"?,', '\n'.join(sd2), re.M))
    return tabs, p if sd2 else None, names


def fn_addrs():
    out = {}
    for m in re.finditer(r'^(\S+) = \.\w+:0x([0-9A-Fa-f]+); // type:function', open('config/%s/symbols.txt' % VER, encoding='utf-8').read(), re.M):
        out[m.group(1)] = int(m.group(2), 16)
    return out


if any(x in ('-h', '--help') for x in sys.argv[1:]):
    TABS, SD2, POOL, ADDR, PROTO = {}, None, set(), {}, {}
else:
    TABS, SD2, POOL = load_data()
    ADDR = fn_addrs()
    PROTO = U.load_json(OUT + '/protos.json', {})


# ---- text helpers -------------------------------------------------------------------------------------------------------
def close_paren(s, o):
    d = 0
    for i in range(o, len(s)):
        if s[i] in '([':
            d += 1
        elif s[i] in ')]':
            d -= 1
            if d == 0:
                return i
    return -1


def split_top(s):
    out, d, cur = [], 0, ''
    for c in s:
        if c in '([{':
            d += 1
        elif c in ')]}':
            d -= 1
        if c == ',' and d == 0:
            out.append(cur)
            cur = ''
        else:
            cur += c
    return out + [cur] if cur.strip() or out else out


def func_span(text, fn):
    for m in re.finditer(r'^[A-Za-z_][^;{}]*?\b%s\s*\(([^;{}]*)\)\s*\{' % re.escape(fn), text, re.M):
        d = 0
        for i in range(m.end() - 1, len(text)):
            if text[i] == '{':
                d += 1
            elif text[i] == '}':
                d -= 1
                if d == 0:
                    e = i + 1
                    t = re.match(r' */\* draft: stub \*/', text[e:])
                    return m.start(), e + (t.end() if t else 0)
    return None


# ---- m2c ------------------------------------------------------------------------------------------------------------------
class M2cInput:
    def __init__(self, unit):
        self.unit = unit
        t = open('build/%s/asm/%s.s' % (VER, unit), encoding='utf-8').read()
        t = t.replace('cror eq, lt, eq', 'cror 2, 0, 2').replace('cror eq, gt, eq', 'cror 2, 1, 2')
        jt = ['.section .data']
        want = set()
        for n in sorted(set(re.findall(r'\bjumptable_[0-9A-F]+', t))):
            rows = TABS.get(n)
            if not rows or re.search(r'^\.obj %s,' % n, t, re.M):   # unknown, or the unit's own asm has it
                continue
            jt.append('.obj %s, global' % n)
            for base, off in rows:
                a = ADDR.get(base)
                jt.append('\t.4byte ' + ('.L_%08X' % (a + off) if a is not None and off else base))
                if a is not None and off:
                    want.add(a + off)
            jt.append('.endobj %s' % n)
        for a in want:
            if not re.search(r'^\.L_%08X:' % a, t, re.M):
                t = re.sub(r'^(/\* %08X )' % a, '.L_%08X:\n\\1' % a, t, count=1, flags=re.M)
        self.dir = '%s/in/%s' % (OUT, unit.replace('/', '_'))
        os.makedirs(self.dir, exist_ok=True)
        self.asm = self.dir + '/a.s'
        self.jt = self.dir + '/jt.s'
        open(self.asm, 'w', encoding='utf-8').write(t)
        open(self.jt, 'w', encoding='utf-8').write('\n'.join(jt) + '\n')
        self.text = t
        if os.path.exists(self.dir + '/ctx.ok'):
            os.remove(self.dir + '/ctx.ok')

    def pool_files(self):
        return [SD2] if SD2 else []

    def run(self, fn, literals, ctx=False):
        extra = []
        if ctx:
            if not PROTO:
                return None
            if not os.path.exists(self.dir + '/ctx.ok'):
                calls = set(re.findall(r'\bb[l]?\s+([A-Za-z_]\w+)', self.text)) | set(re.findall(r'^\.fn ([^,\s]+),', self.text, re.M))
                open(self.dir + '/ctx.c', 'w', encoding='utf-8').write(protolib.context(calls, PROTO))
                open(self.dir + '/ctx.ok', 'w').close()
            extra = ['--context', self.dir + '/ctx.c']
        files = [self.asm, self.jt] + (self.pool_files() if literals else [])
        try:
            r = subprocess.run([sys.executable, M2C, '--target', 'ppc-mwcc-c', '--valid-syntax', '--globals=used',
                                '--no-cache', '-f', fn] + extra + files,
                               capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=300)
        except subprocess.TimeoutExpired:
            return None
        if r.returncode != 0:
            return None
        return self.parse(fn, r.stdout)

    @staticmethod
    def parse(fn, text):
        lines = text.splitlines()
        start = next((i for i, l in enumerate(lines) if re.match(r'^\S.*\b%s\s*\(.*\)\s*\{\s*$' % re.escape(fn), l)), None)
        if start is None:
            return None
        end = next((j for j in range(start + 1, len(lines)) if lines[j] == '}'), None)
        if end is None:
            return None
        decls = {}
        for l in lines[:start]:
            l = re.sub(r'/\*.*?\*/', '', l).strip()
            if not l.endswith(';') or l.startswith(('typedef', 'struct', 'union', '}')):
                continue
            l = re.sub(r'^static\s+', '', l)
            mf = re.search(r'(\w+)\s*\([^()]*(?:\([^()]*\)[^()]*)*\)\s*;$', l)
            if mf:
                decls.setdefault(mf.group(1), l)
                continue
            l = re.sub(r'\s*=.*;$', ';', l)
            mv = re.search(r'(\w+)\s*(\[[^\]]*\])*\s*;$', l)
            if mv:
                decls.setdefault(mv.group(1), l if l.startswith('extern ') else 'extern ' + l)
        head = re.sub(r'^static\s+', '', lines[start].rstrip()[:-1].rstrip())
        return decls, head, lines[start + 1:end]


# ---- body passes ----------------------------------------------------------------------------------------------------------
def fix_bytes(body):
    """byte arithmetic: void * -> u8 *, `&lbl + N` -> byte offset"""
    out = []
    for l in body:
        l = re.sub(r'\bvoid \*', 'u8 *', l)
        l = re.sub(r'(?<!\)\))(?<![\w)] )&(\w+) (?=[+-] )', r'(u8 *)&\1 ', l)
        if out and l.strip().startswith('}') and re.match(r'^\s*(\w+|case [^:;]+|default):\s*(/\*.*\*/)?$', out[-1]):
            out.append('    ;')
        out.append(l)
    return out


def group_stack(body):
    """address-taken slot + the slots behind it (up to the next address-taken one) -> one array"""
    decl = {}
    for i, l in enumerate(body):
        m = re.match(r'^    (\w[\w ]*?\**) ?(\**)(sp[0-9A-F]+);$', l)
        if m:
            decl[int(m.group(3)[2:], 16)] = (i, (m.group(1) + m.group(2)).strip(), m.group(3))
    if not decl:
        return None
    code = '\n'.join(body)
    taken = {o for o, (_, _, n) in decl.items() if re.search(r'(?<!&)&%s\b(?!\[)' % n, code)}
    offs = sorted(decl)
    groups = []
    for o in offs:
        if o not in taken:
            continue
        mem = [o]
        for p in offs:
            if p <= o:
                continue
            if p in taken:
                break
            mem.append(p)
        nxt = next((p for p in offs if p > o and p in taken), None)
        size = (nxt - o) if nxt is not None else (mem[-1] - o + 4)
        if len(mem) == 1 and (size <= 4 or decl[o][1] not in ('M2C_UNK', 's32', 'u32', 'f32', 'u8', 's8')):
            continue
        if size > 0x2000 or len(mem) == 1 and decl[o][1] not in ('M2C_UNK',) and nxt is None:
            continue
        groups.append((o, mem, size))
    if not groups:
        return None
    body = list(body)
    drop = set()
    for o, mem, size in groups:
        i0, t0, n0 = decl[o]
        types = {decl[p][1] for p in mem}
        is4 = len(types) == 1 and t0 in ('f32', 's32', 'u32', 'M2C_UNK') and all((p - o) % 4 == 0 for p in mem)
        et = t0 if is4 and t0 != 'M2C_UNK' else 's32'
        n = (size + 3) // 4
        body[i0] = '    %s %s[%d];' % (et, n0, n)
        for p in mem:
            ip, tp, np = decl[p]
            if p != o:
                drop.add(ip)
            for k, l in enumerate(body):
                if k == i0 or k == ip or np not in l:
                    continue
                if p == o:
                    l = re.sub(r'(?<!&)&%s\b(?!\[)' % np, '@@ARR@@', l)
                if is4:
                    l = re.sub(r'\b%s\b' % np, '%s[%d]' % (n0, (p - o) // 4), l)
                else:
                    l = re.sub(r'\b%s\b' % np, 'M2C_FIELD(%s, %s *, 0x%X)' % ('@@ARR@@', tp, p - o), l)
                body[k] = l.replace('@@ARR@@', n0)
    return [l for k, l in enumerate(body) if k not in drop]


# ---- compile + score --------------------------------------------------------------------------------------------------------
class Unit:
    def __init__(self, unit):
        self.unit = unit
        self.dir = '%s/w/%s' % (OUT, unit.replace('/', '_'))
        os.makedirs(self.dir + '/proj', exist_ok=True)
        self.src = 'src/%s.c' % unit
        self.c = self.dir + '/t.c'
        self.o = self.dir + '/t.o'
        argv = unit_command(unit, out_dir=os.path.abspath(self.o), src=os.path.abspath(self.c))
        for opt in ('-maxerrors', '-msgstyle'):      # the repair loop reads MWCC's own message style
            while opt in argv:
                k = argv.index(opt)
                del argv[k:k + 2]
        k = argv.index('-c')
        argv[k:k] = ['-maxerrors', '1', '-i', os.path.dirname(self.src)]
        self.argv = argv
        U.save_json(self.dir + '/proj/objdiff.json', {'min_version': '2.0.0', 'units': [{
            'name': 'u', 'base_path': '../t.o',
            'target_path': os.path.relpath('build/%s/obj/%s.o' % (VER, unit), self.dir + '/proj').replace('\\', '/')}]})
        self.compiles = 0

    def errors(self, text):
        """every error of one compile: [(line, msg, shown)]"""
        with open(self.c, 'w', encoding='utf-8', errors='surrogateescape', newline='') as f:
            f.write(text)
        argv = list(self.argv)
        argv[argv.index('-maxerrors') + 1] = '300'
        try:
            r = subprocess.run(argv, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=25)
        except subprocess.TimeoutExpired:
            return []
        out = []
        for blk in re.split(r'^### mwcceppc(?:\.exe)? Compiler:\s*$', r.stdout + r.stderr, flags=re.M)[1:]:
            m = re.search(r'^#\s+(\d+):([^\n]*)\n#\s+Error:[^\n]*\n((?:#[^\n]*\n?)*)', blk, re.M)
            if m:
                msg = ' '.join(x.lstrip('#').strip() for x in m.group(3).splitlines()).strip()
                out.append((int(m.group(1)), msg.replace(' Too many errors printed, aborting program', ''), m.group(2).strip()))
        return out

    def compile(self, text):
        self.compiles += 1
        with open(self.c, 'w', encoding='utf-8', errors='surrogateescape', newline='') as f:
            f.write(text)
        if os.path.exists(self.o):
            os.remove(self.o)
        try:
            r = subprocess.run(self.argv, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=25)
        except subprocess.TimeoutExpired:
            return 0, 'TIMEOUT', ''
        if r.returncode == 0 and os.path.exists(self.o):
            return None
        out = r.stdout + r.stderr
        for blk in re.split(r'^### mwcceppc(?:\.exe)? Compiler:\s*$', out, flags=re.M)[1:]:
            m = re.search(r'^#\s+(\d+):([^\n]*)\n#\s+Error:[^\n]*\n((?:#[^\n]*\n?)*)', blk, re.M)
            if m:
                msg = ' '.join(x.lstrip('#').strip() for x in m.group(3).splitlines()).strip()
                return int(m.group(1)), msg.replace(' Too many errors printed, aborting program', ''), m.group(2).strip()
        return 0, 'CRASH ' + out[-200:].replace('\n', ' '), ''

    def score(self):
        r = subprocess.run([tool('objdiff-cli'), 'report', 'generate', '-p', self.dir + '/proj',
                            '-o', self.dir + '/proj/r.json', '-f', 'json'], capture_output=True, text=True)
        if r.returncode != 0:
            return None
        with open(self.dir + '/proj/r.json', encoding='utf-8') as f:
            rep = json.load(f)
        self.size = {f['name']: int(f.get('size') or 0) for u in rep['units'] for f in u.get('functions', [])}
        return {f['name']: float(f.get('fuzzy_match_percent') or 0) for u in rep['units'] for f in u.get('functions', [])}


# ---- repair -----------------------------------------------------------------------------------------------------------------
TEMP_RE = re.compile(r'^((unk)?sp[0-9A-Fa-f]+|var_\w+|temp_\w+|saved_reg_\w+|arg\d+|farg\d+|phi_\w+)$')


def sub_exprs(s):
    """(start, end) of the sub-expressions of a statement that a cast may be put on, outermost first"""
    out = []
    m = re.match(r'^(.*?[^=!<>+\-*/|&^])= (?!=)(.*);$', s)
    if m:
        out.append((m.start(2), m.end(2)))
    m = re.match(r'^return (.*);$', s)
    if m:
        out.append((m.start(1), m.end(1)))
    for m in re.finditer(r'\(', s):   # arguments of calls / parenthesised assignments
        e = close_paren(s, m.start())
        if e < 0:
            continue
        inner = s[m.start() + 1:e]
        pre = s[:m.start()].rstrip()
        am = re.match(r'^(\w+) = (?!=)(.*)$', inner)
        if am:
            out.append((m.start() + 1 + am.start(2), e))
        if not (pre and (pre[-1].isalnum() or pre[-1] in '_)')):
            continue
        if re.search(r'\b(M2C_FIELD|M2C_BITWISE|if|while|switch|for|sizeof)$', pre):
            continue
        pos = m.start() + 1
        for a in split_top(inner):
            st = pos + (len(a) - len(a.lstrip()))
            en = pos + len(a.rstrip())
            if en > st:
                out.append((st, en))
            pos += len(a) + 1
    return out


def cast_to(t, e, via):
    t = t.strip()
    if via:
        return '(%s)(s32)(%s)' % (t, e)
    return '(%s)(%s)' % (t, e)


def candidates(msg, s):
    """rewritten versions of statement s that might cure msg, best first"""
    c = []
    m = re.search(r"illegal (implicit|explicit) conversion from '(.+?)' to '(.+?)'$", msg)
    if m:
        kind, a, b = m.groups()
        if kind == 'implicit':
            fl = re.search(r'float|double', a + ' ' + b) and '*' in a + b
            for st, en in sub_exprs(s):
                if not s[st:en].startswith('(%s)' % b):
                    c.append(s[:st] + cast_to(b, s[st:en], fl) + s[en:])
            c += [s[:st] + '*(%s *)&%s' % (b, s[st:en]) + s[en:] for st, en in sub_exprs(s)[:2] if re.match(r'^\w+$', s[st:en])]
        else:   # an explicit cast the compiler refuses: go through s32
            for x in re.finditer(r'\((f32|f64|s32|u32|u8|s8|s16|u16|[\w ]+\*+)\) ?', s):
                c.append(s[:x.end()] + '(s32) ' + s[x.end():])
    if 'pointer/array required' in msg or 'illegal type' in msg or 'illegal operand' in msg or 'not a struct' in msg:
        for x in re.finditer(r'(?<![\w)\]] )(?<![\w)\]])\*(\w+)\b(?!\()', s):
            for t in ('s32', 'u8'):
                c.append(s[:x.start()] + '*(%s *)%s' % (t, x.group(1)) + s[x.end():])
        for x in re.finditer(r'\b(\w+)\[', s):
            c.append(s[:x.start()] + '((s32 *)%s)[' % x.group(1) + s[x.end():])
        for x in re.finditer(r'\b(\w+)(?= [-+*/%&|^] )|(?<= [-+*/%&|^] )(\w+)\b', s):
            n = x.group(1) or x.group(2)
            if not n[0].isdigit():
                c.append(s[:x.start()] + '((s32)%s)' % n + s[x.end():])
        for x in re.finditer(r'(M2C_FIELD)\(', s):
            e = close_paren(s, x.end() - 1)
            if e > 0:
                c.append(s[:x.start()] + '((s32)%s)' % s[x.start():e + 1] + s[e + 1:])
                c.append(s[:x.start()] + '((u8 *)%s)' % s[x.start():e + 1] + s[e + 1:])
    if 'illegal operands' in msg or 'type mismatch' in msg or 'illegal operand' in msg:
        for x in re.finditer(r'(==|!=|<=|>=|(?<![<-])<(?![<=])|(?<![>-])>(?![>=])| [&|^+\-] )', s):
            # nearest operand on both sides
            r = re.match(r'\s*((?:\([\w ]+\**\) ?)*[&*]*\w+(?:\((?:[^()]|\([^()]*\))*\))?)', s[x.end():])
            l = re.search(r'((?:\([\w ]+\**\) ?)*[&*]*\w+(?:\((?:[^()]|\([^()]*\))*\))?)\s*$', s[:x.start()])
            if r and r.group(1) not in ('0', 'NULL'):
                c.append(s[:x.end()] + ' (s32)(%s)' % r.group(1) + s[x.end() + r.end():])
            if l:
                c.append(s[:l.start(1)] + '(s32)(%s) ' % l.group(1) + s[x.start():])
            if r and l:
                c.append(s[:l.start(1)] + '(s32)(%s) ' % l.group(1) + x.group(1) + ' (s32)(%s)' % r.group(1) + s[x.end() + r.end():])
    if 'modifiable lvalue' in msg or 'lvalue' in msg:
        m = re.match(r'^\((?:[\w ]+\**)\) ?(\w+) = ', s)
        if m:
            c.append(s[:0] + m.group(1) + ' = ' + s[m.end():])
    m = re.search(r"function call '(\w+)\(.*\)' does not match '\1\((.*)\)'", msg)
    if m:
        n, want = m.group(1), [w.strip() for w in split_top(m.group(2))]
        want = [w for w in want if w and w != 'void']
        if not (want and want[-1] == '...'):
            for x in re.finditer(r'\b%s\(' % re.escape(n), s):
                e = close_paren(s, x.end() - 1)
                if e < 0:
                    continue
                have = [a.strip() for a in split_top(s[x.end():e])] if s[x.end():e].strip() else []
                have = (have + ['0'] * len(want))[:len(want)]
                args = [a if a.startswith('(%s)' % t) else '(%s)(%s)' % (t, a) for t, a in zip(want, have)]
                c.append(s[:x.end()] + ', '.join(args) + s[e:])
    seen, out = set(), []
    for x in c:
        if x != s and x not in seen:
            seen.add(x)
            out.append(x)
    return out[:24]


def strip_bodies(text, fn):
    out, pos = [], 0
    for m in re.finditer(r'^([A-Za-z_][^;{}()=]*?\b(\w+)\s*\(([^;{}]*)\))\s*\{', text, re.M):
        if m.start() < pos or m.group(2) == fn:
            continue
        d, e = 0, None
        for i in range(m.end() - 1, len(text)):
            if text[i] == '{':
                d += 1
            elif text[i] == '}':
                d -= 1
                if d == 0:
                    e = i + 1
                    break
        if e is None:
            break
        out.append(text[pos:m.start()])
        out.append(m.group(1) + ';')
        pos = e
    out.append(text[pos:])
    return ''.join(out)


class Draft:
    """one new body for fn, compiled inside the current unit text"""

    def __init__(self, u, text, fn, decls, head, body, ctx=False):
        self.ctx, self.redecl = ctx, {}
        self.u, self.text, self.fn = u, text, fn
        self.span = func_span(text, fn)
        self.old = text[self.span[0]:self.span[1]]
        # for the repair loop the bodies of the other functions are dropped (their prototypes stay): faster compiles
        self.thin = strip_bodies(text, fn)
        self.thin_span = func_span(self.thin, fn)
        self.static = self.old.startswith('static ')
        self.head, self.body = head, list(body)
        self.m2c_decls = decls
        self.decls = {}          # extra declarations in front of the first definition
        self.unproto = set()     # functions whose existing prototype becomes ()
        self.commented = 0
        self.limit = max(8, len([l for l in body if l.strip().endswith(';')]) // 8)
        self.why = ''
        self.own_proto_open = False
        self.banned = set()
        self.history = {}

    def assemble(self, full=False):
        t = self.text if full else self.thin
        new = ('static ' if self.static else '') + self.head + ' {\n'
        sp = self.span if full else self.thin_span
        pre, post = t[:sp[0]], t[sp[1]:]
        # the own prototype
        h = ('static ' if self.static else '') + self.head
        if self.own_proto_open:
            h = re.sub(r'\(.*\)$', '()', h, flags=re.S)
        own = re.compile(r'^[A-Za-z_][^;{}()=]*?\b%s\s*\([^;{}]*\);' % re.escape(self.fn), re.M)
        pre = own.sub(lambda m: h + ';', pre, count=1)
        for n, (rx, new_decl) in self.redecl.items():
            pre = rx.sub(lambda m: new_decl, pre, count=1)
        for n in self.unproto:
            rx = re.compile(r'^([A-Za-z_][^;{}()=]*?\b%s\s*)\([^;{}]*\);' % re.escape(n), re.M)
            pre = rx.sub(lambda m: m.group(1) + '();', pre)
        if self.decls:
            block = '\n'.join(self.decls.values()) + '\n'
            # after the last #include
            incs = [m.end() for m in re.finditer(r'^#include[^\n]*\n', pre, re.M)]
            k = incs[-1] if incs else 0
            pre = pre[:k] + block + pre[k:]
        self.first = pre.count('\n') + 2
        self.decl_line0 = None
        out = pre + new + '\n'.join(self.body) + '\n}' + post
        return out

    def have_decl(self, n):
        return n in self.decls or re.search(r'^[^\n/]*\b%s\b\s*(\([^;{}]*\)|\[[^\]]*\])?\s*[;{,=]' % re.escape(n),
                                            self.text, re.M) and re.search(
            r'^(extern|static|[A-Za-z_]\w*)[^\n;{}]*\b%s\b[^\n;]*[;{]' % re.escape(n), self.text, re.M)

    def line_index(self, line):
        k = line - self.first
        return k if 0 <= k < len(self.body) else None

    def add_missing(self):
        if self.ctx:
            # callees the unit declares with a result / parameters the whole program contradicts
            used = {n for n in set(re.findall(r'\b([A-Za-z_]\w+)\(', '\n'.join(self.body))) if n in PROTO and n != self.fn}
            for n in used:
                rx = re.compile(r'^(?:M2C_UNK|void|s32|void \*|f32|u32|u8 \*)\s*\**%s\([^;{}]*\);\n' % re.escape(n), re.M)
                m = rx.search(self.text)
                if m and not re.search(r'^[A-Za-z_][^;{}]*?\b%s\s*\([^;{}]*\)\s*\{' % re.escape(n), self.text, re.M):
                    r = PROTO[n][0]
                    self.redecl[n] = (rx, '%s%s%s();\n' % (r, '' if r.endswith('*') else ' ', n))
        if self.ctx:
            for n in set(re.findall(r'\b([A-Za-z_]\w+)\(', '\n'.join(self.body))):
                if n in PROTO and n not in self.m2c_decls:
                    r = PROTO[n][0]
                    self.m2c_decls[n] = '%s%s%s();' % (r, '' if r.endswith('*') else ' ', n)
        for n, l in self.m2c_decls.items():
            if n != self.fn and n not in ('memcpy', 'memset') and not self.have_decl(n):
                self.decls[n] = re.sub(r'\bvoid \*', 'void *', l)

    def run(self, deadline):
        self.add_missing()
        self.batch(deadline)
        last = None
        for _ in range(600):
            if time.time() > deadline:
                self.why = 'time'
                return None
            text = self.assemble()
            err = self.u.compile(text)
            if err is None:
                break
            if err == last:
                self.stuck = getattr(self, 'stuck', 0) + 1
                if self.stuck > 4:
                    self.why = 'stuck: %s' % (err,)
                    return None
            last = err
            if not self.repair(err, text):
                self.why = '%s | %s' % (err[1][:120], err[2][:100])
                return None
        else:
            self.why = 'rounds'
            return None
        # now with the other functions back in
        for _ in range(12):
            text = self.assemble(True)
            err = self.u.compile(text)
            if err is None:
                return text
            m = re.search(r'\b%s\b' % re.escape(self.fn), err[1] + err[2])
            if m and not self.own_proto_open:
                self.own_proto_open = True
                continue
            for n in re.findall(r'\b(\w+)\s*\(', err[2]):   # a prototype we opened / a declaration we added
                if n in self.unproto or n in self.decls:
                    break
            else:
                n = None
            if n and n in self.decls and not self.decls[n].endswith('();'):
                self.decls[n] = re.sub(r'\(.*\)\s*;$', '();', self.decls[n])
                continue
            break
        self.why = 'full unit: %s | %s' % (err[1][:100], err[2][:80])
        return None

    def batch(self, deadline):
        """repair all errors of one compile at once with their first candidate (the single-step loop does the rest)"""
        for _ in range(8):
            if time.time() > deadline:
                return
            errs = self.u.errors(self.assemble())
            done, seen = 0, set()
            grow = []
            for line, msg, shown in errs:
                k = self.line_index(line)
                if k is None or k in seen:
                    continue
                s = self.body[k].strip()
                m = re.search(r"undefined identifier '([^']+)'", msg)
                if m:
                    n = m.group(1)
                    if TEMP_RE.match(n):
                        if n not in grow and '    s32 %s;' % n not in self.body:
                            grow.append(n)
                    elif n not in self.decls and n not in self.banned:
                        self.decls[n] = ('s32 %s();' if re.search(r'\b%s\s*\(' % re.escape(n), s) else 'extern s32 %s;') % n
                        done += 1
                    seen.add(k)
                    continue
                m = re.search(r"function call '(\w+)\(.*\)' does not match", msg)
                if m:
                    n = m.group(1)
                    if n in self.decls and not self.decls[n].endswith('();'):
                        self.decls[n] = re.sub(r'\([^()]*(?:\([^()]*\)[^()]*)*\)\s*;$', '();', self.decls[n])
                        done += 1
                    elif n != self.fn and n not in self.unproto and n not in self.decls:
                        self.unproto.add(n)
                        done += 1
                    seen.add(k)
                    continue
                if 'implicit conversion' in msg and re.match(r'^(return |.*[^=!<>+\-*/|&^]= )', s) and s.endswith(';'):
                    c = candidates(msg, s)
                    if c:
                        self.set_line(k, c[0])
                        seen.add(k)
                        done += 1
            for n in grow:
                self.body.insert(0, '    s32 %s;' % n)
                done += 1
            if not done:
                return

    def set_line(self, k, s):
        ind = re.match(r'^\s*', self.body[k]).group(0)
        self.body[k] = ind + s

    def repair(self, err, text):
        line, msg, shown = err
        if msg.startswith(('CRASH', 'TIMEOUT')):
            return False
        k = self.line_index(line)
        m = re.search(r"undefined identifier '([^']+)'", msg)
        if m:
            n = m.group(1)
            if n in self.banned:
                return k is not None and self.comment(k)
            if TEMP_RE.match(n) and k is not None:
                d = '    s32 %s;' % n
                if d in self.body:
                    return self.comment(k)
                self.body.insert(0, d)
                return True
            if n in self.decls:
                self.banned.add(n)
                return k is not None and self.comment(k)
            txt = self.body[k] if k is not None else shown
            self.decls[n] = ('s32 %s();' if re.search(r'\b%s\s*\(' % re.escape(n), txt) else 'extern s32 %s;') % n
            return True
        if k is None:
            # a declaration we added, or another function of the unit that no longer fits our head / opened prototype
            for n, l in list(self.decls.items()):
                if shown and shown == l.strip():
                    if l.endswith('();') or l.startswith('extern s32 '):
                        del self.decls[n]
                        self.banned.add(n)
                    else:
                        self.decls[n] = ('s32 %s();' if '(' in l else 'extern s32 %s;') % n
                    return True
            if re.search(r'\b%s\b' % re.escape(self.fn), msg + shown) and not self.own_proto_open:
                self.own_proto_open = True
                return True
            return False
        s = self.body[k].strip()
        # multi-line statements: the error line may be a continuation; only single lines are repaired
        m = re.search(r"function call '(\w+)\(.*\)' does not match", msg)
        if m and m.group(1) != self.fn and m.group(1) not in self.unproto and not re.search(
                r'^[A-Za-z_][^;{}]*?\b%s\s*\([^;{}]*\)\s*\{' % re.escape(m.group(1)), self.text, re.M):
            n = m.group(1)
            if n in self.decls:
                new = re.sub(r'\([^()]*(?:\([^()]*\)[^()]*)*\)\s*;$', '();', self.decls[n])
                if new != self.decls[n]:
                    self.decls[n] = new
                    return True
            else:
                self.unproto.add(n)
                return True
        uid = self.body[k].rstrip()
        orig, n = self.history.get(uid, (uid, 0))
        if n < 6:
            base = list(self.body)
            best = None
            for cand in candidates(msg, s):
                self.body = list(base)
                self.set_line(k, cand)
                e2 = self.u.compile(self.assemble())
                if e2 is None or e2[0] > line:
                    rank = 3
                elif e2[0] == line and e2[1] != msg and 'syntax' not in e2[1] and 'explicit' not in e2[1]:
                    rank = 1
                else:
                    continue
                if best is None or rank > best[0]:
                    best = (rank, self.body)
                if rank == 3:
                    break
            if best:
                self.body = best[1]
                self.history[self.body[k].rstrip()] = (orig, n + 1)
                return True
            self.body = base
        self.body[k] = orig
        return self.comment(k)

    def comment(self, k):
        if self.commented >= self.limit:
            return False
        s = self.body[k].strip()
        ind = re.match(r'^\s*', self.body[k]).group(0)
        void = re.match(r'^(static\s+)?void\s+\w', self.head)
        m = re.match(r'^((?:\}\s*else\s+)?(?:if|while|switch)\s*\()(.*)(\)\s*\{)$', s)
        if m:
            new = '%s0 /* draft: did not compile */%s' % (m.group(1), m.group(3))
        elif re.match(r'^\} while \(.*\);$', s):
            new = '} while (0 /* draft: did not compile */);'
        elif s.startswith('return ') and s.endswith(';'):
            new = 'return;' if void else 'return 0;'
            if s == new:
                return False
        elif s == 'return;' and not void:
            new = 'return 0;'
        elif re.match(r'^(case\b.*|default):', s):
            new = '/* draft: did not compile: %s */' % s.replace('*/', '* /')[:120]
        elif re.match(r'^[\w ]+\**\s*\(?\*?\w+\)?(\[[^\]]*\])*(\([^)]*\))?;$', s) and not re.match(r'^(return|goto)\b', s) \
                and not re.match(r'^\w+\(', s):
            n = re.search(r'(sp\w+|var_\w+|temp_\w+|unksp\w+)', s)
            new = ('s32 %s;' % n.group(1)) if n and s != 's32 %s;' % n.group(1) else ''
            if not new:
                return False
        elif s.endswith(';') and not re.match(r'^(for\b|else\b|\})', s):
            new = '/* draft: did not compile: %s */' % s.replace('*/', '* /')[:200]
        else:
            return False
        self.body[k] = ind + new
        self.commented += 1
        return True


# ---- one unit -----------------------------------------------------------------------------------------------------------------
TRIED_F = OUT + '/tried.json'
TRIED = U.load_json(TRIED_F, {})


def key(old):
    return '%d:%s' % (VERSION, hashlib.md5(re.sub(r'\s+', ' ', old).encode('utf-8', 'surrogateescape')).hexdigest()[:12])


def do_unit(unit, a):
    u = Unit(unit)
    text = U.read_source(u.src)
    if u.compile(text) is not None:
        log('%s: current source does not compile, skipped' % unit)
        return []
    cur = u.score()
    if cur is None:
        return []
    todo = [f for f, p in cur.items() if p < a.max_pct and f in ADDR and (not a.func or f == a.func)]
    todo = [f for f in todo if func_span(text, f)]
    if not a.fresh:
        todo = [f for f in todo if TRIED.get(f) != key(text[slice(*func_span(text, f))])]
    if not todo:
        return []
    inp = M2cInput(unit)
    if not re.search(r'\bM2C_FIELD\b|%s' % re.escape(MACROS), text) and os.path.exists('include/' + MACROS):
        text = '#include "%s"\n' % MACROS + text      # drafts use its macros; changes no code by itself
        if u.compile(text) is not None:
            text = text.split('\n', 1)[1]
        else:
            cur = u.score() or cur
    kept = []
    for fn in todo:
        sp = func_span(text, fn)
        if not sp:
            continue
        oldkey = key(text[sp[0]:sp[1]])
        best, fake = None, False
        deadline = time.time() + a.timeout
        whys = []
        fnasm = re.search(r'^\.fn %s,.*?^\.endfn' % re.escape(fn), inp.text, re.M | re.S)
        pool = bool(fnasm and POOL & set(re.findall(r'"?([\w@$.]+)"?@sda21', fnasm.group(0))))
        for literals, ctx in ((True, True), (True, False), (False, True), (False, False)):
            if not literals and not pool:
                break
            res = inp.run(fn, literals, ctx)
            if not res:
                whys.append('m2c')
                continue
            decls, head, body = res
            body = fix_bytes(body)
            variants = [('plain', body)]
            g = group_stack(body)
            if g:
                variants.insert(0, ('stack', g))
            for vn, b in variants:
                d = Draft(u, text, fn, decls, head, b, ctx)
                nt = d.run(deadline)
                if nt is None:
                    whys.append(d.why)
                    continue
                sc = u.score()
                if not sc:
                    continue
                worse = [f for f in cur if f != fn and sc.get(f, 0) < cur[f] - 1e-4]
                # opening a shared prototype may cost another draft a little: fine for a much larger gain, never an exact
                lost = sum((cur[f] - sc.get(f, 0)) * u.size.get(f, 0) for f in worse)
                won = (sc.get(fn, 0) - cur[fn]) * u.size.get(fn, 0)
                if worse and (any(cur[f] >= 100 for f in worse) or lost > 0.15 * won):
                    whys.append('worse: ' + ','.join(worse[:3]))
                    continue
                p = sc.get(fn, 0)
                if p >= 100 and not U.strict_exact(unit, fn, u.o):
                    # scored 100% but not identical, typically because the compiler inlined a neighbour that is still a
                    # stub: a fake 100% would hide the function from every later search. Not installed, and not marked
                    # as tried, so the next run looks again when the neighbour has a real body
                    fake = True
                    whys.append('scores 100% but is not identical')
                    continue
                if d.commented and cur[fn] >= 10 and p < 100 and p < cur[fn] + 3 * d.commented:
                    continue   # a draft that drops statements must be clearly better
                if (p >= 100 > cur[fn] or p > cur[fn] + a.min_gain) and (best is None or p > best[0]):
                    best = (p, nt, sc, '%s%s%s c=%d' % (vn, ',ctx' if ctx else '', '' if literals else ',extern', d.commented))
            if best and best[0] >= 100:
                break
        if best:
            log('kept %s %s: %.2f -> %.2f (%s)' % (unit, fn, cur[fn], best[0], best[3]))
            kept.append((fn, cur[fn], best[0]))
            text, cur = best[1], best[2]
            sp = func_span(text, fn)
            if sp:
                oldkey = key(text[sp[0]:sp[1]])
        elif cur[fn] < 10:
            log('   no %s %s (%.2f): %s' % (unit, fn, cur[fn], ' / '.join(w for w in whys if w)[:260]))
        if best or not fake:
            with LOCK:
                TRIED[fn] = oldkey
    if kept and not a.dry:
        U.write_source(u.src, text)
    return kept


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--only')
    ap.add_argument('--func')
    ap.add_argument('--max-pct', type=float, default=100.0)
    ap.add_argument('--min-gain', type=float, default=0.3)
    ap.add_argument('--timeout', type=float, default=400)
    ap.add_argument('--units', default='', help='only units whose name matches this regular expression')
    ap.add_argument('--m2c', help='m2c checkout (default: $M2C)')
    ap.add_argument('-j', type=int, default=8)
    ap.add_argument('--dry', action='store_true')
    ap.add_argument('--fresh', action='store_true')
    a = ap.parse_args()
    units = []
    for n, fs in U.units().items():
        if U.source_of(n) != 'src/%s.c' % n or not re.search(a.units, n):
            continue
        if not os.path.exists('build/%s/asm/%s.s' % (VER, n)):
            continue
        if a.only and n != a.only.replace('\\', '/').removeprefix('src/').removesuffix('.c'):
            continue
        low = sum(int(f['size']) for f in fs if (f.get('fuzzy_match_percent') or 0) < min(a.max_pct, 100))
        if low:
            units.append((low, n))
    units.sort(reverse=True)
    t0 = time.time()
    allk = []
    with ThreadPoolExecutor(a.j) as ex:
        for k in ex.map(lambda x: safe(x[1], a), units):
            allk += k
    if not a.dry:
        with LOCK:
            U.save_json(TRIED_F, TRIED)
    log('redraft: kept %d of the functions tried in %d units (%d now exact), %.0f min'
        % (len(allk), len(units), sum(1 for k in allk if k[2] >= 100), (time.time() - t0) / 60))


def safe(n, a):
    try:
        return do_unit(n, a)
    except Exception as e:
        import traceback
        log('ERROR %s: %r %s' % (n, e, traceback.format_exc()[-400:].replace('\n', ' | ')))
        return []


if __name__ == '__main__':
    main()
