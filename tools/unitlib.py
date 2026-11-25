"""unitlib.py - shared helpers of the self-verifying fixers (argrepair, permfix, autoinstall, redraft, refport).

  report() / units()            report.json: {symbol: (unit, percent, size)} and {unit: [function rows]}
  source_of(unit)               src/<unit>.c / .cpp / .cp, whichever exists
  scores(unit, src, tag)        compile with fcheck --all -> ({function: percent}, {known problems}, object path)
  strict_exact(unit, fn, obj)   is the function REALLY identical? (see below)
  func_span(text, fn)           where a C function's definition is in a source text
  params / calls_in / value_pool ...  small C text helpers

Why strict_exact exists: a fuzzy 100% is not proof. Depending on the diff settings, objdiff does not look at call targets
or relocation names, and label numbers are normalised. strict_exact() disassembles both objects with dtk and compares
every instruction (registers, immediates, call targets, branch targets as offsets). Only names that carry no meaning
are normalised: anonymous constants (@12), jump-table and lbl_ symbols, and data symbol names behind @ha/@l/@sda21 (data
is judged by fcheck's CONST / DATA checks and by the executable hash).
"""
import json
import os
import re
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from dtkproj import REPO, VER, tool   # noqa: E402  (also chdirs to the project root)
except SystemExit:
    if not any(a in ('-h', '--help') for a in sys.argv):
        raise
    REPO, VER = '.', 'VER'     # --help without a project

    def tool(name):
        return name

TOOLS = os.path.dirname(os.path.abspath(__file__)).replace('\\', '/')
LOCK = threading.RLock()


# ---- project state -------------------------------------------------------------------------------------------------
def unit_name(report_name):
    """report.json calls a unit 'main/game/menu'; the tools call it 'game/menu'"""
    return report_name.split('/', 1)[1] if '/' in report_name else report_name


def units():
    rep = json.load(open('build/%s/report.json' % VER, encoding='utf-8'))
    out = {}
    for u in rep['units']:
        out.setdefault(unit_name(u['name']), []).extend(u.get('functions', []))
    return out


def report():
    return {f['name']: (u, float(f.get('fuzzy_match_percent') or 0), int(f.get('size') or 0))
            for u, fs in units().items() for f in fs}


def source_of(unit):
    return next((p for p in ('src/%s.c' % unit, 'src/%s.cpp' % unit, 'src/%s.cp' % unit) if os.path.exists(p)), None)


def read_source(path):
    """source text with LF line ends (bytes that are not UTF-8 survive a round trip)"""
    return open(path, encoding='utf-8', errors='surrogateescape', newline='').read().replace('\r\n', '\n')


def write_source(path, text):
    """write it back with the line ends the file had"""
    nl = '\n'
    if os.path.exists(path):
        with open(path, 'rb') as f:
            if b'\r\n' in f.read(65536):
                nl = '\r\n'
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8', errors='surrogateescape', newline=nl) as f:
        f.write(text)
    for _ in range(60):           # Windows refuses while another process reads the file
        try:
            os.replace(tmp, path)
            return
        except OSError:
            time.sleep(0.5)
    raise OSError('could not write %s (locked?)' % path)


# ---- scoring -------------------------------------------------------------------------------------------------------
def scores(unit, src=None, tag='unitlib'):
    """({function: percent}, {problem keys}, object path); (None, None, None) when it does not compile.
    Problem keys: 'CALLS:fn' (call list differs from the original), 'CONST:fn' (another constant value)."""
    cmd = [sys.executable, TOOLS + '/fcheck.py', unit, '--all', '--tag', tag]
    if src:     # a scratch copy: quoted includes are still searched next to the real file
        cmd += ['--src', src, '--extra', '-i ' + os.path.dirname(source_of(unit) or 'src/' + unit)]
    o = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace',
                       stdin=subprocess.DEVNULL).stdout
    if 'COMPILE FAILED' in o or not re.search(r'^== ', o, re.M):
        return None, None, None
    try:
        rep = json.load(open('build/scratch/fcheck/%s/proj/r.json' % tag, encoding='utf-8'))
        res = {f['name']: float(f.get('fuzzy_match_percent') or 0) for u in rep['units'] for f in u.get('functions', [])}
    except (OSError, ValueError, KeyError, TypeError):
        return None, None, None
    bad = set()
    if 'CALLS DIFFER' in o:
        bad |= {'CALLS:' + f for f in re.findall(r'^    (\S+): target \[', o.split('CALLS DIFFER', 1)[1], re.M)}
    if 'CONST VALUES DIFFER' in o:
        sec = o.split('CONST VALUES DIFFER', 1)[1].split('\nDATA ', 1)[0].split('\nCALLS ', 1)[0]
        bad |= {'CONST:' + f for f in re.findall(r'^    (\S+):', sec, re.M)}
    m = re.search(r'obj=(\S+\.o)', o)
    return res, bad, m and m.group(1)


def no_worse(before, after, fn=None, eps=1e-9):
    """functions (other than fn) that score lower than before"""
    return [f for f in before if f != fn and after.get(f, 0) < before[f] - eps]


# ---- strict comparison -----------------------------------------------------------------------------------------------
def disasm_funcs(obj, out):
    """{function: [dtk disassembly lines]}"""
    try:
        subprocess.run([tool('dtk'), 'elf', 'disasm', obj, out], capture_output=True, stdin=subprocess.DEVNULL, timeout=120)
        text = open(out, encoding='utf-8', errors='replace').read()
    except (OSError, subprocess.SubprocessError):
        return {}
    funcs, cur, name = {}, None, None
    for l in text.splitlines():
        m = re.match(r'\.fn\s+"?([^",\s]+|[^"]+)"?', l)
        if m:
            name, cur = m.group(1).strip('"'), []
        elif cur is not None and l.startswith('.endfn'):
            funcs[name] = cur
            cur = None
        elif cur is not None:
            cur.append(l)
    return funcs


_ANON = re.compile(r'@\d+|\$\d+|\b(?:lbl|jumptable)_[0-9A-Fa-f]+|\.\.\.\w+\.\d+')


def strict_norm(lines):
    """comparable instruction list of one function (see the module docstring for what is normalised)"""
    start = None
    for l in lines:
        m = re.match(r'/\* ([0-9A-F]{8}) ', l)
        if m:
            start = int(m.group(1), 16)
            break
    out = []
    for l in lines:
        m = re.match(r'/\* ([0-9A-F]{8}) [0-9A-F]+\s+[0-9A-F ]{11} \*/\s*(.*)', l)
        if not m:
            continue
        ins = re.sub(r'\.L_([0-9A-F]{8})', lambda x: 'L+%X' % (int(x.group(1), 16) - start), m.group(2))
        ins = _ANON.sub('@N', ins.replace('"', ''))
        out.append(re.sub(r'[^\s,()]+@(ha|l|sda21|h)\b', r'SYM@\1', ins))
    return out


_TARGET = {}


def target_funcs(unit):
    with LOCK:
        if unit not in _TARGET:
            d = 'build/scratch/unitlib'
            os.makedirs(d, exist_ok=True)
            p = '%s/target_%s.s' % (d, re.sub(r'\W', '_', unit))
            _TARGET[unit] = disasm_funcs('build/%s/obj/%s.o' % (VER, unit), p)
        return _TARGET[unit]


def strict_exact(unit, fn, obj):
    if not obj or not os.path.exists(obj):
        return False
    t = target_funcs(unit).get(fn)
    o = disasm_funcs(obj, obj + '.strict.s').get(fn)
    return bool(t) and o is not None and strict_norm(t) == strict_norm(o)


# ---- C text ----------------------------------------------------------------------------------------------------------
def func_span(text, fn):
    """(start of the definition, index of {, index of }, parameter list) or None. C symbol names only."""
    # the first character is a letter: anything looser also matches the closing brace of the function before
    for m in re.finditer(r'^[A-Za-z_][^;{}]*?\b%s\s*\(([^;{}]*)\)\s*\{' % re.escape(fn), text, re.M):
        depth, i = 0, m.end() - 1
        while i < len(text):
            if text[i] == '{':
                depth += 1
            elif text[i] == '}':
                depth -= 1
                if depth == 0:
                    return m.start(), m.end() - 1, i, m.group(1)
            i += 1
    return None


def definition(text, fn):
    sp = func_span(text, fn)
    return (sp[0], sp[2] + 1) if sp else None


FLOAT_T = re.compile(r'^\s*(?:const\s+)?(?:f32|f64|float|double)\s+\w+\s*$')


def params(plist):
    """[(name, integer register or None)]: integer/pointer parameters get r3, r4, ...; floats None"""
    out, ri = [], 3
    for p in [p.strip() for p in plist.split(',')]:
        if not p or p in ('void', '...'):
            continue
        name = re.findall(r'(\w+)\s*(?:\[\s*\w*\s*\])?$', p)
        if not name:
            continue
        if FLOAT_T.match(p):
            out.append((name[0], None))
        else:
            out.append((name[0], 'r%d' % ri))
            ri += 1
    return out


def calls_in(body, callee):
    """[[(start, end) of each top-level argument], ...] for each call of callee in body"""
    res = []
    for m in re.finditer(r'\b%s\s*\(' % re.escape(callee), body):
        i, depth, start, spans = m.end(), 1, m.end(), []
        while i < len(body) and depth:
            c = body[i]
            if c in '([{':
                depth += 1
            elif c in ')]}':
                depth -= 1
                if depth == 0:
                    spans.append((start, i))
            elif c == ',' and depth == 1:
                spans.append((start, i))
                start = i + 1
            i += 1
        res.append(spans)
    return res


def balanced_calls(body, name):
    """every `name(...)` expression text in body"""
    out = []
    for m in re.finditer(r'\b%s\s*\(' % re.escape(name), body):
        i, depth = m.end(), 1
        while i < len(body) and depth:
            depth += {'(': 1, ')': -1}.get(body[i], 0)
            i += 1
        out.append(body[m.start():i])
    return out


LOCAL_RE = re.compile(r'\b(?:temp_[rf]\d+(?:_\d+)?|var_[rf]\d+(?:_\d+)?|sp[0-9A-F]+|arg\d+|arg_sp\w+)\b')


def value_pool(body, plist, reg, cast=None, target_k=None, limit=24):
    """candidate values for an argument: [(text, kind)]
    kind 'const' = the constant the target loads; 'slot' = the parameter that arrived in reg; 'pool' = anything else"""
    out, seen = [], set()

    def add(t, kind):
        for txt in ([t] + (['(%s)%s' % (cast, t)] if cast else [])):
            if txt not in seen:
                seen.add(txt)
                out.append((txt, kind))
    if target_k is not None:
        add(str(target_k) if target_k < 10 else hex(target_k), 'const')
    ps = params(plist)
    for n, r in ps:
        if r == reg:
            add(n, 'slot')
    for n, r in ps:
        if r != reg:
            add(n, 'pool')
    for n in sorted(set(LOCAL_RE.findall(body)), key=body.find):
        add(n, 'pool')
    for e in sorted(set(balanced_calls(body, 'M2C_FIELD')), key=body.find):
        add(e, 'pool')
    return out[:limit]


# ---- small things ------------------------------------------------------------------------------------------------------
def load_json(path, default):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, obj, **kw):
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f, **kw)
    os.replace(tmp, path)


class Log:
    """prints and appends to a file, usable from several threads"""

    def __init__(self, path):
        self.path = path

    def __call__(self, s):
        with LOCK:
            print(s, flush=True)
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, 'a', encoding='utf-8') as f:
                f.write(s + '\n')
