"""regsolve.py - targeted search for register / order near misses of ONE function.

Instead of random rewrites (a permuter), compile only the source edits that are known to move the compiler's
register colouring or instruction order, all of them behaviour-preserving:
  move     move one function-level declaration before another / to the end of the declarations
  hoist    a block-scoped declaration -> declared at the top of the function (initialiser stays in place)
  type     int <-> s32 / long (same 32-bit type, different spelling) for a local
  width    other integer type respellings of a local (checked by the compiler for the cast it needs)
  wrap     route a call through a tiny static inline helper
  temp     hold a call's result in a named local just before the statement (also at the top / with a cast)
  ptrin    replace a single-assignment pointer local `T* p = &X;` by X itself
  inarg    a once-used call-result local passed on directly as an argument (reverse of temp)
  tern     if/else assigning one variable <-> ternary
  name     hold a sub-expression of the line that defines a misplaced register value in a local
  flip     swap operands of the culprit line's commutative operators / compares
  helper   un-alias a pointer local inside the project's inline helpers the function calls
  access   hold a member / array access chain in a local
  expr     expression rewrites: x++ split out, ++x before, compound <-> plain, reuse of a just-loaded local,
           moving a call-free assignment up, integer sum regrouping, load into a temp, for -> while
Round 1 compiles every single edit, round 2 every pair of the best singles, round 3 grows the best combination greedily,
and the whole thing restarts from the best text up to --iter times. Score = differing instruction lines of the function
(strict, registers included; 0 = exact) + 10 per instruction only one side has (shape change) + 100 per other function
of the unit that stops matching; the RESULT line reports the plain differing-line count.

Never touches src/: variants are written to build/scratch/search/ and compiled with fcheck --src (the unit's real
compile command). C and C++ (methods, constructors) are handled; the function is found by its asm symbol name.

usage (project root, or set DECOMP_REPO):
  python tools/search/regsolve.py UNIT FUNC [--src FILE] [-j 6] [--top 10] [--iter 4] [--timeout SEC]
                                  [--ops move,hoist,type,...] [--write OUT] [--seeds N [--seeds-dir D]]
  UNIT = path under src/ without extension; FUNC = asm symbol (e.g. update__5PlayerFv); --src = start from this
  file instead of src/UNIT.c(pp) (e.g. the best text of another search).
Last output line (machine readable):
  RESULT <unit> <func> <base_score> <best_score> <path-to-best-variant-or-NONE>      (lower = better, 0 = exact)
Environment: SEARCH_SCRATCH (default build/scratch/search), SEARCH_TAG (prefix for fcheck tag folders).
"""
import difflib, itertools, os, re, shlex, subprocess, sys, threading, queue, time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

H = os.path.dirname(os.path.abspath(__file__)).replace('\\', '/')
sys.path.insert(0, os.path.dirname(H))
sys.path.insert(0, H)
if any(a in ('-h', '--help') for a in sys.argv[1:]) or len(sys.argv) < 3:
    print(__doc__)
    sys.exit(0 if len(sys.argv) > 1 else 2)
from dtkproj import *   # REPO, VER, EXE; chdir to the project root
FCHECK = os.path.join(os.path.dirname(H), 'fcheck.py')
SCRATCH = os.environ.get('SEARCH_SCRATCH', 'build/scratch/search').rstrip('/') + '/regsolve'
TAGP = os.environ.get('SEARCH_TAG', '')
T0 = time.time()
args = sys.argv[1:]


def opt(name, default=None):
    return args[args.index(name) + 1] if name in args else default


unit, func = args[0], args[1]
J = int(opt('-j', 6))
TOP = int(opt('--top', 10))
OPS = set(opt('--ops', 'move,hoist,type,width,wrap,temp,ptrin,inarg,tern,name,flip,helper,access,expr').split(','))
TIMEOUT = float(opt('--timeout', 0) or 0)   # seconds for the whole search; 0 = unlimited
DEADLINE = (T0 + TIMEOUT) if TIMEOUT else None
TIMED_OUT = [False]
RAW = {}   # source text -> plain differing-line count (what RESULT reports)
src_path_orig = next((p for p in ('src/%s.cpp' % unit, 'src/%s.c' % unit, 'src/%s.cp' % unit) if os.path.exists(p)), None)
if not src_path_orig:
    sys.exit('no source file src/%s.c|cpp' % unit)
src_path = opt('--src') or src_path_orig   # variants keep the original file name (anonymous-namespace mangling)
ext = os.path.splitext(src_path)[1]
raw = open(src_path, encoding='latin-1', newline='').read()
CRLF = '\r\n' in raw
SRC = raw.replace('\r\n', '\n')
IS_C = ext == '.c'
NL = chr(10)
HTYPES = {'u8': ['u32', 's32', 'int'], 'u16': ['u32', 's32'], 'u32': ['u8', 's32', 'int'], 's32': ['int', 'u32'],
          'int': ['s32', 'u32'], 's8': ['s32'], 's16': ['s32']}
KEYW = {'if', 'for', 'while', 'switch', 'return', 'sizeof', 'do', 'else', 'case', 'new', 'delete'}

# ------------------------------------------------------------------ locate the function
plain = func.split('__')[0] if '__' in func else func


def mangled_class(f):
    """class name of a mangled method (last component of a Q-qualified name), or None"""
    k = f.find('__', 1)
    if k < 0:
        return None
    s = f[k + 2:]
    comps, n = [], 1
    if re.match(r'Q\d', s):
        n, s = int(s[1]), s[2:]
    for _ in range(n):
        m = re.match(r'(\d+)', s)
        if not m:
            return None
        ln = int(m.group(1))
        comps.append(s[len(m.group(1)):len(m.group(1)) + ln])
        s = s[len(m.group(1)) + ln:]
    return comps[-1] if comps and s.startswith(('F', 'C')) else None


CLS = mangled_class(func)
if CLS and CLS.startswith('@unnamed@'):   # free function in an anonymous namespace, not a method
    CLS = None
if func.startswith(('__ct__', '__dt__')) and CLS:   # constructors / destructors
    plain = CLS if func.startswith('__ct__') else '~' + CLS
HEAD = (CLS + '::' + plain) if CLS else plain


def locate(text):
    """(head_start, body_start, body_end) of the function in text; body excludes the braces"""
    # definitions start in column 0 with their return type / qualifiers (call sites are indented);
    # extern "C" prefixes and constructor initialiser lists are allowed
    fm = re.search(r'^(?:[A-Za-z_][\w:\*&<>" \t]*?[\s\*&])?' + re.escape(HEAD) +
                   r'\s*\([^;{]*\)\s*(?:const\s*)?(?::[^;{]*)?\{', text, re.M)
    if not fm:
        return None
    s = fm.end()
    depth, i = 1, s
    while depth and i < len(text):
        depth += {'{': 1, '}': -1}.get(text[i], 0)
        i += 1
    return fm.start(), s, i - 1


if not locate(SRC):
    print('definition of %s not found in %s' % (HEAD, src_path))
    print('RESULT %s %s -1 -1 NONE' % (unit, func))
    sys.exit(2)

# ------------------------------------------------------------------ prototypes (preprocessed source)
PRE = ''
if OPS & {'wrap', 'temp'}:
    cmd = subprocess.run(['ninja', '-t', 'commands', 'build/%s/src/%s.o' % (VER, unit)], capture_output=True,
                         text=True).stdout.strip().splitlines()[-1]
    argv = shlex.split(cmd.replace('\\', '/'), posix=True)
    k = argv.index('-c')
    argv = [x for x in argv[:k] if x != '-MMD'] + ['-E', src_path]
    argv[0] = os.path.abspath(argv[0])
    PRE = subprocess.run(argv, capture_output=True, text=True, encoding='latin-1', errors='replace').stdout
    PRE = re.sub(r'^#.*$', '', PRE, flags=re.M)

TYPE_ONLY = {'int', 'char', 'short', 'long', 'float', 'double', 'unsigned', 'signed', 'bool', 'void', 'const',
             'struct', 'class', 'enum', 'volatile'}


def split_top(s):
    out, depth, cur = [], 0, ''
    for ch in s:
        if ch in '([{':
            depth += 1
        elif ch in ')]}':
            depth -= 1
        if ch == ',' and depth == 0:
            out.append(cur)
            cur = ''
        else:
            cur += ch
    if cur.strip():
        out.append(cur)
    return [x.strip() for x in out]


def param_types(params):
    ps = split_top(params)
    if ps in ([], ['void']):
        return []
    tys = []
    for p in ps:
        p = re.sub(r'\s*=.*$', '', p).strip()
        if '(' in p or p == '...':
            return None
        m = re.match(r'^(.*?[\s\*&])\s*([A-Za-z_]\w*)$', p)
        if m and m.group(1).strip() and not set(m.group(1).split()) <= {'const', 'unsigned', 'signed', 'struct',
                                                                          'class', 'enum', 'volatile'} \
                and m.group(2) not in TYPE_ONLY:
            p = m.group(1).strip()
        tys.append(p)
    return tys


def clean_ret(t):
    t = re.sub(r'extern\s+"C"', '', t)
    t = re.sub(r'\b(static|inline|virtual|extern|__declspec\(\w+\))\b', '', t)
    return ' '.join(t.split())


CLASSES = {}   # name -> (bases, body text)
FREE = PRE


def _scan_classes():
    global FREE
    out, last = [], 0
    for m in re.finditer(r'\b(?:class|struct)\s+([A-Za-z_]\w*)\s*(:[^;{]*)?\{', PRE):
        if m.start() < last:
            continue
        d, i = 1, m.end()
        while d and i < len(PRE):
            d += {'{': 1, '}': -1}.get(PRE[i], 0)
            i += 1
        bases = re.findall(r'([A-Za-z_]\w*)\s*(?:,|$)', (m.group(2) or '').lstrip(':').strip())
        CLASSES.setdefault(m.group(1), (bases, PRE[m.end():i]))
        out.append(PRE[last:m.end()])
        last = i - 1
    out.append(PRE[last:])
    FREE = ''.join(out)


if PRE:
    _scan_classes()


def proto(name, member):
    """(return type, [param types]) of a free function / method of CLS (or its bases), or None"""
    if member:
        seen, todo = set(), [CLS]
        while todo:
            c = todo.pop(0)
            if c in seen or c not in CLASSES:
                continue
            seen.add(c)
            r = _proto_in(name, CLASSES[c][1])
            if r:
                return r
            todo += CLASSES[c][0]
        return None
    return _proto_in(name, FREE)


def _proto_in(name, text):
    pat = r'(?:^|[;{}:])\s*([A-Za-z_][\w\s\*&:<>"]*?)\b' + re.escape(name) + \
          r'\s*\(((?:[^()]|\([^()]*\))*)\)\s*(?:const\s*)?[;{]'
    for m in re.finditer(pat, text, re.M):
        ret = clean_ret(m.group(1))
        if not ret or ret.split()[0] in KEYW or ret.split()[-1] in KEYW or '=' in ret:
            continue
        tys = param_types(m.group(2))
        if tys is None:
            return None
        return ret, tys
    return None


# ------------------------------------------------------------------ edits (functions text -> text or None)
DECL = re.compile(r'^(\s+)((?:const\s+)?(?:unsigned\s+|signed\s+|struct\s+)?[A-Za-z_][\w:]*(?:\s*\*+)?)\s+'
                  r'(\**)([A-Za-z_]\w*)(\s*=\s*[^;]+)?;[ \t]*$')


def body_lines(text):
    loc = locate(text)
    if not loc:
        return None
    hs, s, e = loc
    return loc, text[s:e].split('\n')


def rebuild(text, loc, lines):
    hs, s, e = loc
    return text[:s] + '\n'.join(lines) + text[e:]


def decls(lines, top_only):
    out = []
    for k, l in enumerate(lines):
        m = DECL.match(l)
        if not m or m.group(2) in KEYW or m.group(2) in ('return', 'goto', 'delete'):
            continue
        if top_only and len(m.group(1).expandtabs()) != 4:
            continue
        out.append((k, m))
    return out


def move_lines(L, name, before):
    """declaration of `name` moved before the declaration of `before` (None = after the last top declaration);
    an initialised declaration is split into declaration + assignment"""
    top = decls(L, True)
    me = [(k, m) for k, m in top if m.group(4) == name]
    if not me:
        return None
    k, m = me[0]
    ind, ty, star, nm, init = m.groups()
    newdecl = '%s%s %s%s;' % (ind, ty, star, nm)
    L = L[:]
    if init:
        L[k] = '%s%s%s;' % (ind, nm, init.rstrip())
    else:
        L[k] = None
    top2 = [(kk, mm) for kk, mm in top if mm.group(4) != name]
    if before is None:
        pos = max(kk for kk, _ in top) + 1
    else:
        b = [kk for kk, mm in top2 if mm.group(4) == before]
        if not b:
            return None
        pos = b[0]
    if init and pos > k:
        return None   # would move the declaration below its own initialisation
    L.insert(pos, newdecl)
    return [l for l in L if l is not None]


def op_move(name, before):
    """move declaration of `name` before the declaration of `before` (None = after the last top declaration)"""
    def f(text):
        bl = body_lines(text)
        if not bl:
            return None
        loc, L = bl
        L2 = move_lines(L, name, before)
        return rebuild(text, loc, L2) if L2 else None
    return f


def op_hmove(helper, name, before):
    """the same inside an inline helper's body (the local order of an inlined helper
    changes the caller's colouring)"""
    def f(text):
        hd = helper_defs(text, [helper]).get(helper)
        if not hd:
            return None
        a, b = hd
        L2 = move_lines(text[a:b].split(NL), name, before)
        return text[:a] + NL.join(L2) + text[b:] if L2 else None
    return f


def op_htype(helper, name, old, new, cast=False):
    """change the type of a helper local (u8/u16/u32/s32/int ...); cast=True: comparisons keep the old width
    (`x != name` -> `x != (old)name`)"""
    def f(text):
        hd = helper_defs(text, [helper]).get(helper)
        if not hd:
            return None
        a, b = hd
        body = text[a:b]
        body2, n = re.subn(r'(?m)^(\s+)' + re.escape(old) + r'(\s+' + re.escape(name) + r'\b)', r'\g<1>' + new + r'\2', body, 1)
        if n and cast:
            body2, n = re.subn(r'([!=]=\s*)' + re.escape(name) + r'\b', r'\g<1>(' + old + ')' + name, body2)
        return text[:a] + body2 + text[b:] if n else None
    return f


def op_hoist(name):
    def f(text):
        bl = body_lines(text)
        if not bl:
            return None
        loc, L = bl
        alld = decls(L, False)
        if sum(1 for _, m in alld if m.group(4) == name) != 1:
            return None
        k, m = next((k, m) for k, m in alld if m.group(4) == name)
        if len(m.group(1).expandtabs()) == 4:
            return None
        ind, ty, star, nm, init = m.groups()
        top = decls(L, True)
        pos = (max(kk for kk, _ in top) + 1) if top else next(i for i, l in enumerate(L) if l.strip())
        L = L[:]
        L[k] = ('%s%s%s;' % (ind, nm, init.rstrip())) if init else None
        L.insert(pos, '    %s %s%s;' % (ty, star, nm))
        return rebuild(text, loc, [l for l in L if l is not None])
    return f


def op_type(name, new):
    def f(text):
        bl = body_lines(text)
        if not bl:
            return None
        loc, L = bl
        for k, m in decls(L, False):
            if m.group(4) == name and m.group(2) in ('int', 's32') and not m.group(3):
                L = L[:]
                L[k] = L[k].replace(m.group(2) + ' ', new + ' ', 1)
                return rebuild(text, loc, L)
        return None
    return f


WIDTHS = ['s8', 'u8', 's16', 'u16', 'int', 'u32']


def op_width(name, new):
    """another integer width for a local; `(new)name` casts in the body are dropped (e.g. `u16 count` instead of
    `s8 count` + `(u16)count` at every use)"""
    def f(text):
        bl = body_lines(text)
        if not bl:
            return None
        loc, L = bl
        hits = [(k, m) for k, m in decls(L, False) if m.group(4) == name]
        if len(hits) != 1:
            return None
        k, m = hits[0]
        if m.group(3) or m.group(2) not in WIDTHS + ['s32', 'u16', 'u32']:
            return None
        L = L[:]
        L[k] = L[k].replace(m.group(2) + ' ', new + ' ', 1)
        cast = re.compile(r'\(\s*' + re.escape(new) + r'\s*\)\s*' + re.escape(name) + r'\b')
        L = [cast.sub(name, l) if i != k else l for i, l in enumerate(L)]
        return rebuild(text, loc, L)
    return f


def calls_in(body):
    """[(name, start, end, argtext, member)] for every call in the body (end = after ')')"""
    out = []
    for m in re.finditer(r'(->|\.)?\b([A-Za-z_]\w*)\s*\(', body):
        name = m.group(2)
        if name in KEYW or name in TYPE_ONLY:
            continue
        pre = m.group(1)
        if pre:
            if not body[:m.start()].endswith('this->') and not body[:m.start()].rstrip().endswith('this'):
                continue
        # skip declarations / casts: an identifier right before the name means "Type name(" (a declaration)
        before = body[:m.start()].rstrip()
        if re.search(r'\b[A-Za-z_]\w*[\s\*&]*$', before) and not re.search(r'\b(return|else|case)\s*$', before) \
                and not before.endswith(('this->', 'this')):
            if re.search(r'[A-Za-z_]\w*\s*[\*&]*$', before) and not re.search(r'[=,(+\-*/!<>&|?:;{}]\s*$', before):
                continue
        d, i = 1, m.end()
        while d and i < len(body):
            d += {'(': 1, ')': -1}.get(body[i], 0)
            i += 1
        start = m.start() if not pre else m.start() - (len('this') if body[:m.start()].endswith('this') else 0)
        if pre:
            start = body.rfind('this', 0, m.start())
        out.append((name, start, i, body[m.end():i - 1], bool(pre) or None))
    return out


WRAPN = [0]


def op_wrap(name, nth, member_hint):
    """route the nth call of `name` through `static inline RET _rsN(params) { return name(params); }`"""
    def f(text):
        loc = locate(text)
        if not loc:
            return None
        hs, s, e = loc
        body = text[s:e]
        cs = [c for c in calls_in(body) if c[0] == name]
        if nth >= len(cs):
            return None
        _, a, b, argt, member = cs[nth]
        pr = proto(name, True) if CLS else None
        member = pr is not None
        if pr is None and not body[:a].endswith('this->'):
            pr = proto(name, False)
        if pr is None:
            return None
        ret, tys = pr
        actual = split_top(argt)
        if len(actual) != len(tys):
            return None
        hid = '_rs%d_%s' % (nth, name)
        params = ', '.join('%s p%d' % (t, i) for i, t in enumerate(tys))
        callargs = ', '.join('p%d' % i for i in range(len(tys)))
        rv = '' if ret == 'void' else 'return '
        if member:
            params = ('%s* self' % CLS) + (', ' + params if params else '')
            helper = 'static inline %s %s(%s) { %sself->%s(%s); }\n\n' % (ret, hid, params, rv, name, callargs)
            newcall = '%s(this%s)' % (hid, (', ' + argt) if argt.strip() else '')
        else:
            helper = 'static inline %s %s(%s) { %s%s(%s); }\n\n' % (ret, hid, params or ('' if not IS_C else 'void'),
                                                               rv, name, callargs)
            newcall = '%s(%s)' % (hid, argt)
        body2 = body[:a] + newcall + body[b:]
        return text[:hs] + helper + text[hs:s] + body2 + text[e:]
    return f


def op_temp(name, nth, top_decl):
    """hold the nth call of `name` in a local: `T _rtN = call;` before its statement"""
    def f(text):
        loc = locate(text)
        if not loc:
            return None
        hs, s, e = loc
        body = text[s:e]
        cs = [c for c in calls_in(body) if c[0] == name]
        if nth >= len(cs):
            return None
        _, a, b, argt, member = cs[nth]
        ls = body.rfind('\n', 0, a) + 1
        le = body.find('\n', b)
        line = body[ls:le if le >= 0 else len(body)]
        stmt_head = line.lstrip()
        if re.match(r'(for|while|do|else|case|\}|switch)\b', stmt_head) or '&&' in line or '||' in line \
                or '?' in line or not line.rstrip().endswith((';', '{')):
            return None
        if body[a:b] == line.strip().rstrip(';').strip():
            return None   # the call is the whole statement
        pr = (proto(name, True) if CLS else None) or proto(name, False)
        if pr is None or pr[0] == 'void':
            return None
        ret = pr[0]
        ind = line[:len(line) - len(stmt_head)]
        v = '_rt%d' % nth
        newline = line[:a - ls] + v + line[b - ls:]
        if top_decl:
            ins = '%s%s = %s;\n' % (ind, v, body[a:b])
            body2 = body[:ls] + ins + newline + body[ls + len(line):]
            body2 = '\n    %s %s;' % (ret, v) + body2
        else:
            if IS_C:
                return None
            ins = '%s%s %s = %s;\n' % (ind, ret, v, body[a:b])
            body2 = body[:ls] + ins + newline + body[ls + len(line):]
        return text[:s] + body2 + text[e:]
    return f


TERN_RE = re.compile(r'^(\s*)([\w\.\->\[\]]+) = \(?(.+?)\)? \? (.+?) : (.+?);\s*$')
IFA_RE = re.compile(r'^(\s*)if \((.+)\) \{\s*$')
ASG_RE = re.compile(r'^\s*([\w\.\->\[\]]+) = (.+);\s*$')


def _tern_at(L, k):
    """lines k..k+4 = `if (C) {` / `X = A;` / `} else {` / `X = B;` / `}` -> (indent, X, C, A, B)"""
    if k + 4 >= len(L):
        return None
    m = IFA_RE.match(L[k])
    a, b = ASG_RE.match(L[k + 1]), ASG_RE.match(L[k + 3])
    if not (m and a and b and L[k + 2].strip() == '} else {' and L[k + 4].strip() == '}'):
        return None
    if a.group(1) != b.group(1) or L[k + 4][:len(m.group(1))] != m.group(1):
        return None
    return m.group(1), a.group(1), m.group(2), a.group(2), b.group(2)


def op_tern(nth):
    """if/else assigning one variable <-> `x = c ? a : b;`: the ternary
    makes one value that ranks above a loop induction offset, the two-arm form a weaker split value"""
    def f(text):
        bl = body_lines(text)
        if not bl:
            return None
        loc, L = bl
        n = 0
        for k in range(len(L)):
            t = _tern_at(L, k)
            if t:
                if n == nth:
                    ind, x, c, a, b = t
                    return rebuild(text, loc, L[:k] + ['%s%s = (%s) ? %s : %s;' % (ind, x, c, a, b)] + L[k + 5:])
                n += 1
        for k, l in enumerate(L):
            m = TERN_RE.match(l)
            if m:
                if n == nth:
                    ind, x, c, a, b = m.groups()
                    if '?' in a or '?' in b:
                        return None
                    return rebuild(text, loc, L[:k] + ['%sif (%s) {' % (ind, c), '%s    %s = %s;' % (ind, x, a),
                                                      '%s} else {' % ind, '%s    %s = %s;' % (ind, x, b),
                                                      '%s}' % ind] + L[k + 1:])
                n += 1
        return None
    return f


def op_inarg(name):
    """the reverse of `temp` (a call result passed STRAIGHT as an argument shifts every
    saved local one register round; through a local it does not): `x = g(..);` whose only use is an argument of a
    call on the next statement -> pass g(..) there directly (declaration removed). Only when x's type is the callee's
    return type or a full-width type, and the receiving call's other arguments contain no calls."""
    WIDE = ('int', 's32', 'u32', 'long', 'f32', 'float', 'double', 'f64')

    def f(text):
        bl = body_lines(text)
        if not bl:
            return None
        loc, L = bl
        ds = [(k, m) for k, m in decls(L, False) if m.group(4) == name]
        if len(ds) != 1:
            return None
        dk, dm = ds[0]
        ty = dm.group(2) + (dm.group(3) or '')
        refs = [k for k, l in enumerate(L) if re.search(r'\b%s\b' % re.escape(name), l)]
        if dm.group(5):
            ak, expr = dk, dm.group(5).split('=', 1)[1].strip().rstrip(';').strip()
        else:
            asg = [k for k in refs if re.match(r'^\s*%s\s*=[^=](.*);\s*$' % re.escape(name), L[k])]
            if len(asg) != 1:
                return None
            ak = asg[0]
            expr = L[ak].split('=', 1)[1].strip().rstrip(';').strip()
        cm = re.match(r'^([A-Za-z_][\w:]*)\s*\(', expr)
        if not cm or not expr.endswith(')'):
            return None
        uses = [k for k in refs if k not in (dk, ak)]
        if len(uses) != 1 or len(re.findall(r'\b%s\b' % re.escape(name), L[uses[0]])) != 1:
            return None
        uk = uses[0]
        if uk < ak or any(L[k].strip() for k in range(ak + 1, uk)):
            return None   # the use must be the very next statement
        if '*' not in ty and ty not in WIDE:
            pr = proto(cm.group(1), False)
            if not pr or pr[0].replace(' ', '') != ty.replace(' ', ''):
                return None
        u = L[uk]
        m = re.search(r'([(,]\s*)%s(\s*[,)])' % re.escape(name), u)
        if not m:
            return None
        depth, st = 0, m.start(1)
        while st > 0:
            st -= 1
            if u[st] == ')':
                depth += 1
            elif u[st] == '(':
                if depth == 0:
                    break
                depth -= 1
        depth, en = 0, m.end(2) - 1
        while en < len(u):
            if u[en] == '(':
                depth += 1
            elif u[en] == ')':
                if depth == 0:
                    break
                depth -= 1
            en += 1
        if '(' in u[st + 1:en].replace(name, ''):
            return None   # other arguments with calls: evaluation order could change
        L2 = list(L)
        L2[uk] = u[:m.start()] + m.group(1) + expr + m.group(2) + u[m.end():]
        for k in sorted({dk, ak}, reverse=True):
            del L2[k]
        return rebuild(text, loc, L2)
    return f


def op_ptrin(name):
    def f(text):
        bl = body_lines(text)
        if not bl:
            return None
        loc, L = bl
        ds = [(k, m) for k, m in decls(L, False) if m.group(4) == name and m.group(5)]
        if len(ds) != 1 or not (ds[0][1].group(3) or '*' in ds[0][1].group(2)):
            return None
        k, m = ds[0]
        x = m.group(5).split('=', 1)[1].strip()
        body = '\n'.join(L)
        if re.search(r'\b' + name + r'\s*(=[^=]|\+\+|--|\+=|-=)', body.replace(L[k], '')):
            return None
        for ident in set(re.findall(r'[A-Za-z_]\w*', x)):
            if re.search(r'(?<![=!<>])\b' + ident + r'\b\s*(=[^=]|\+\+|--|\+=|-=)', body.replace(L[k], '')) \
                    or re.search(r'(\+\+|--)\s*' + ident + r'\b', body):
                return None
        rest = '\n'.join(L[:k] + L[k + 1:])
        if x.startswith('&'):
            inner = x[1:].strip()
            rest = re.sub(r'\b' + name + r'\s*->', '(%s).' % inner if not re.match(r'^[\w\[\]\.]+$', inner) else inner + '.', rest)
        rest = re.sub(r'\b' + name + r'\b', '(%s)' % x, rest)
        return rebuild(text, loc, rest.split('\n'))
    return f


# ------------------------------------------------------------------ culprit lines (regwhere.py) + expression types
def culprit_lines(text, limit=6):
    """source lines (stripped text) that define the values sitting in wrong registers, most frequent first"""
    d = SCRATCH + '/w%d' % os.getpid()
    os.makedirs(d, exist_ok=True)
    p = '%s/%s' % (d, os.path.basename(src_path_orig))
    open(p, 'w', encoding='latin-1', newline='').write(text.replace('\n', '\r\n') if CRLF else text)
    r = subprocess.run([sys.executable, os.path.join(os.path.dirname(H), 'regwhere.py'), unit, func, '--src', p],
                       capture_output=True, text=True)
    cnt = {}
    for l in r.stdout.splitlines():
        m = re.search(r' x(\d+)\s+def #\d+ .* line (\d+)\s+(.*)$', l)
        if m:
            cnt[m.group(3).strip()] = cnt.get(m.group(3).strip(), 0) + int(m.group(1))
    return [t for t, _ in sorted(cnt.items(), key=lambda kv: -kv[1])][:limit]


CC_ARGV = None


def probe_type(text, line_txt, expr):
    """C type of expr at the statement line_txt, read from the compiler's conversion error (None if unknown)"""
    global CC_ARGV
    if CC_ARGV is None:
        cmd = subprocess.run(['ninja', '-t', 'commands', 'build/%s/src/%s.o' % (VER, unit)], capture_output=True,
                             text=True).stdout.strip().splitlines()[-1]
        a = shlex.split(cmd.replace('\\', '/'), posix=True)
        k = a.index('-c')
        CC_ARGV = [x for x in a[:k] if x != '-MMD']
        CC_ARGV[0] = os.path.abspath(CC_ARGV[0])
    loc = locate(text)
    hs, s, e = loc
    body = text[s:e]
    i = body.find(line_txt)
    if i < 0:
        return None
    ls = body.rfind('\n', 0, i) + 1
    probe = '{ struct __rsprobe { int z; } __rsx = (%s); }\n' % expr
    t2 = text[:s] + body[:ls] + probe + body[ls:] + text[e:]
    d = SCRATCH + '/p%d_%d' % (os.getpid(), threading.get_ident() % 100000)
    os.makedirs(d, exist_ok=True)
    p = '%s/%s' % (d, os.path.basename(src_path_orig))
    open(p, 'w', encoding='latin-1', newline='').write(t2)
    r = subprocess.run(CC_ARGV + ['-c', p, '-o', d + '/p.o'], capture_output=True, text=True, errors='replace')
    o = re.sub(r'\s*\n#\s*', ' ', r.stdout + r.stderr)
    m = re.search(r"illegal implicit conversion from '([^']+)' to", o)
    if not m or '@' in m.group(1) or '{' in m.group(1) or '(' in m.group(1):
        return None
    return m.group(1)


def line_exprs(line):
    """candidate sub-expressions of a statement line: member/index chains (and their prefixes), simple binary
    expressions inside parentheses"""
    out = []
    for m in re.finditer(r'[A-Za-z_]\w*(?:\s*(?:->|\.)\s*[A-Za-z_]\w*|\[[^\[\]]+\])+', line):
        ch = m.group(0)
        after = line[m.end():]
        if re.match(r'\s*\(', after):          # method call: not a value
            ch = re.sub(r'\s*(->|\.)\s*\w+$', '', ch)
            if not re.search(r'->|\.|\[', ch):
                continue
        parts = re.findall(r'->|\.|\[[^\[\]]+\]', ch)
        cur = re.match(r'[A-Za-z_]\w*', ch).group(0)
        rest = ch[len(cur):]
        while rest:
            m2 = re.match(r'\s*(->|\.)\s*[A-Za-z_]\w*|\[[^\[\]]+\]', rest)
            if not m2:
                break
            cur += m2.group(0)
            rest = rest[len(m2.group(0)):]
            out.append(cur)
    for m in re.finditer(r'\(([^()]*[^()\s][+\-*/|&^][^()]*)\)', line):
        inner = m.group(1).strip()
        if re.search(r'[A-Za-z_0-9)\]]\s*[+\-*/|&^]\s*[A-Za-z_0-9(]', inner) and '=' not in inner:
            out.append(inner)
    # nested parenthesis groups too (`f32 d = (0.8f * (w / s)) / t;`), and the LEFT PREFIXES of a binary chain inside a group (`A / t` of `A / t * scale`)
    stack = []
    for k, ch in enumerate(line):
        if ch == '(':
            stack.append(k)
        elif ch == ')' and stack:
            a = stack.pop()
            if a > 0 and re.match(r'[\w\]]', line[a - 1]):
                continue                                   # call / cast-free check: f(...) is not a value group
            inner = line[a + 1:k].strip()
            if '=' in inner.replace('==', '') or re.search(r'\b[A-Za-z_]\w*\s*\(', inner):
                continue
            ops, d, cur = [], 0, ''
            for c in inner:
                d += {'(': 1, ')': -1}.get(c, 0)
                if d == 0 and c in '+-*/' and cur.strip() and not re.search(r'[eE]$', cur.strip()):
                    ops.append(cur)
                    cur = ''
                    ops.append(c)
                    continue
                cur += c
            ops.append(cur)
            terms = ops[0::2]
            if len(terms) >= 2 and all(t.strip() for t in terms):
                out.append(inner)
                acc = terms[0]
                for n in range(1, len(terms) - 1):
                    acc = acc + ops[2 * n - 1] + terms[n]
                    out.append(acc.strip())
    # call arguments ("argument locals"): every non-trivial top-level argument of every call
    for m in re.finditer(r'\b([A-Za-z_]\w*)\s*\(', line):
        if m.group(1) in KEYW:
            continue
        depth, k = 1, m.end()
        while k < len(line) and depth:
            depth += {'(': 1, ')': -1}.get(line[k], 0)
            k += 1
        if depth:
            continue
        for arg in split_top(line[m.end():k - 1]):
            if arg and not re.match(r'^(?:[A-Za-z_]\w*|-?[0-9][\w.]*|"[^"]*"|\'[^\']*\')$', arg) and \
                    '=' not in arg.replace('==', '').replace('!=', '').replace('<=', '').replace('>=', ''):
                out.append(arg)
    seen, res = set(), []
    for x in out:
        if x not in seen and not re.match(r'^(this|self)$', x):
            seen.add(x)
            res.append(x)
    return res


def stmt_ok(line):
    st = line.strip()
    return not (re.match(r'(for|while|do|else|case|default|\}|switch|return\s*;)', st) or '&&' in line or
                '||' in line or '?' in line or not st.endswith((';', '{')))


def op_name(line_txt, expr, ty, k, top_decl):
    """hold `expr` (on the statement line_txt) in a named local just before that statement"""
    def f(text):
        loc = locate(text)
        if not loc:
            return None
        hs, s, e = loc
        body = text[s:e]
        i = body.find(line_txt)
        if i < 0 or not stmt_ok(line_txt):
            return None
        ls = body.rfind('\n', 0, i) + 1
        le = body.find('\n', i)
        line = body[ls:le if le >= 0 else len(body)]
        j = line.find(expr)
        if j < 0:
            return None
        # no assignment to the expression itself, no other nested calls that could run before it
        if re.match(r'\s*(=[^=]|\+\+|--|[+\-*/|&^]=)', line[j + len(expr):]):
            return None
        rest = line[:j] + line[j + len(expr):]
        calls = [c for c in re.findall(r'\b([A-Za-z_]\w*)\s*\(', rest) if c not in KEYW]
        if len(calls) > 1 or re.search(r'\b[A-Za-z_]\w*\s*\(', expr):
            return None
        ind = line[:len(line) - len(line.lstrip())]
        v = '_rn%d' % k
        newline = line[:j] + v + line[j + len(expr):]
        if top_decl or IS_C:
            body2 = '\n    %s %s;' % (ty, v) + body[:ls] + '%s%s = %s;\n' % (ind, v, expr) + newline + body[ls + len(line):]
        else:
            body2 = body[:ls] + '%s%s %s = %s;\n' % (ind, ty, v, expr) + newline + body[ls + len(line):]
        return text[:s] + body2 + text[e:]
    return f


def op_tempcast(name, nth):
    """`(T)f(...)` -> `T _rcN = f(...);` before the statement, use _rcN (memcardUnlockWaitGBA exact this way)"""
    def f(text):
        loc = locate(text)
        if not loc:
            return None
        hs, s, e = loc
        body = text[s:e]
        cs = [c for c in calls_in(body) if c[0] == name]
        if nth >= len(cs):
            return None
        _, a, b, argt, member = cs[nth]
        cm = re.search(r'\(\s*((?:const\s+)?(?:unsigned\s+|signed\s+)?[A-Za-z_]\w*)\s*\)\s*$', body[:a])
        if not cm or cm.group(1) in KEYW or cm.group(1) == 'void':
            return None
        ty = cm.group(1)
        ls = body.rfind('\n', 0, a) + 1
        le = body.find('\n', b)
        line = body[ls:le if le >= 0 else len(body)]
        if not stmt_ok(line) or IS_C and not line.strip():
            return None
        ind = line[:len(line) - len(line.lstrip())]
        v = '_rc%d' % nth
        ca = cm.start() - ls
        newline = line[:ca] + v + line[b - ls:]
        if IS_C:
            body2 = '\n    %s %s;' % (ty, v) + body[:ls] + '%s%s = %s;\n' % (ind, v, body[a:b]) + newline + body[ls + len(line):]
        else:
            body2 = body[:ls] + '%s%s %s = %s;\n' % (ind, ty, v, body[a:b]) + newline + body[ls + len(line):]
        return text[:s] + body2 + text[e:]
    return f


def op_access(line_txt, expr, ty, bty, k):
    """read `base->field` (on line_txt) through `static inline TY _rsaccK(BTY p) { return p->field; }`
    (a value from an inline accessor outranks loop-created pointers)"""
    m0 = re.match(r'^(.*\S)\s*->\s*([A-Za-z_]\w*)$', expr)

    def f(text):
        if not m0:
            return None
        base, field = m0.group(1), m0.group(2)
        loc = locate(text)
        if not loc:
            return None
        hs, s, e = loc
        body = text[s:e]
        i = body.find(line_txt)
        if i < 0:
            return None
        j = line_txt.find(expr)
        if j < 0 or re.match(r'\s*(=[^=]|\+\+|--|[+\-*/|&^]=)', line_txt[j + len(expr):]):
            return None
        hid = '_rsacc%d' % k
        helper = 'static inline %s %s(%s p) { return p->%s; }\n\n' % (ty, hid, bty, field)
        new = line_txt[:j] + '%s(%s)' % (hid, base) + line_txt[j + len(expr):]
        body2 = body[:i] + new + body[i + len(line_txt):]
        return text[:hs] + helper + text[hs:s] + body2 + text[e:]
    return f


FLIP = {'==': '==', '!=': '!=', '+': '+', '*': '*', '<': '>', '>': '<', '<=': '>=', '>=': '<='}


def op_flip(line_txt, nth):
    """swap the operands of the nth simple `A op B` (op in + * == != < > <= >=) on the line"""
    atom = r'[A-Za-z_0-9][\w]*(?:\s*(?:->|\.)\s*\w+|\[[^\[\]]+\])*'
    pat = re.compile(r'(?<=[(=,?:\s])(' + atom + r')\s*(==|!=|<=|>=|<|>|\+|\*)\s*(' + atom + r')(?=\s*[),;?:])')

    def f(text):
        loc = locate(text)
        if not loc:
            return None
        hs, s, e = loc
        body = text[s:e]
        i = body.find(line_txt)
        if i < 0:
            return None
        ms = list(pat.finditer(line_txt))
        if nth >= len(ms):
            return None
        m = ms[nth]
        pre = line_txt[:m.start()].rstrip()
        if pre.endswith(('-', '/', '%', '*', '<<', '>>', '!', '~', '&', '|', '^')) or \
                (m.group(2) in '+*' and pre.endswith(('+',))):
            return None
        new = line_txt[:m.start()] + '%s %s %s' % (m.group(3), FLIP[m.group(2)], m.group(1)) + line_txt[m.end():]
        return text[:s] + body[:i] + new + body[i + len(line_txt):] + text[e:]
    return f


def helper_defs(text, names):
    """inline helpers defined in this source file: name -> (start, end) of the body text between the braces"""
    out = {}
    for nm in names:
        for m in re.finditer(r'(?m)^[ \t]*(?:static\s+)?(?:inline\s+)?[A-Za-z_][\w\s\*&:<>]*?\b' + re.escape(nm) +
                             r'\s*\([^;{)]*\)\s*(?:const\s*)?\{', text):
            hs = locate(text)
            if hs and hs[0] <= m.start() < hs[2]:
                continue
            d, i = 1, m.end()
            while d and i < len(text):
                d += {'{': 1, '}': -1}.get(text[i], 0)
                i += 1
            if i - m.end() < 600:   # small helpers only
                out.setdefault(nm, (m.end(), i - 1))
    return out


def op_unalias(helper, k):
    """inside inline helper `helper`: replace the kth `T* p = X;` alias by X itself (only uses before X changes)"""
    def f(text):
        hd = helper_defs(text, [helper]).get(helper)
        if not hd:
            return None
        a, b = hd
        body = text[a:b]
        ms = list(re.finditer(r'\b[A-Za-z_]\w*\s*\*+\s*([A-Za-z_]\w*)\s*=\s*([A-Za-z_][\w\->\.]*)\s*;\s*', body))
        if k >= len(ms):
            return None
        m = ms[k]
        p, x = m.group(1), m.group(2)
        rest = body[m.end():]
        if re.search(r'\b' + p + r'\s*(=[^=]|\+\+|--|\+=|-=)|(\+\+|--)\s*' + p + r'\b', rest):
            return None
        mods = [mm.start() for ident in set(re.findall(r'[A-Za-z_]\w*', x))
                for mm in re.finditer(r'\b' + ident + r'\s*(=[^=]|\+\+|--|\+=|-=)|(\+\+|--)\s*' + ident + r'\b', rest)]
        uses = [mm.start() for mm in re.finditer(r'\b' + p + r'\b', rest)]
        if not uses or (mods and max(uses) > min(mods)):
            return None
        new = body[:m.start()] + re.sub(r'\b' + p + r'\b', x, rest)
        return text[:a] + new + text[b:]
    return f


def make_ops(text):
    loc, L = body_lines(text)
    ops = []
    if OPS & {'name', 'flip', 'helper'}:
        lines = culprit_lines(text)
        if 'helper' in OPS:
            called = set()
            for l in lines:
                called |= set(c for c in re.findall(r'\b([A-Za-z_]\w*)\s*\(', l) if c not in KEYW)
            for nm, (a, b) in helper_defs(text, sorted(called)).items():
                n = len(re.findall(r'\b[A-Za-z_]\w*\s*\*+\s*[A-Za-z_]\w*\s*=\s*[A-Za-z_][\w\->\.]*\s*;', text[a:b]))
                for k in range(n):
                    ops.append(('unalias %s#%d' % (nm, k), op_unalias(nm, k)))
                HL = text[a:b].split(NL)
                hn = [m.group(4) for _, m in decls(HL, True)]
                for n1 in hn:
                    for n2 in hn + [None]:
                        if n2 != n1:
                            ops.append(('hmove %s.%s before %s' % (nm, n1, n2 or 'END'), op_hmove(nm, n1, n2)))
                for _, m in decls(HL, False):
                    if not m.group(3) and m.group(2) in HTYPES:
                        for nt in HTYPES[m.group(2)]:
                            ops.append(('htype %s.%s %s' % (nm, m.group(4), nt), op_htype(nm, m.group(4), m.group(2), nt)))
                            if m.group(2) in ('u8', 'u16', 's8', 's16') and nt in ('int', 's32'):
                                ops.append(('htypecast %s.%s %s' % (nm, m.group(4), nt),
                                            op_htype(nm, m.group(4), m.group(2), nt, True)))
        print('  culprit lines: ' + ' | '.join(l[:60] for l in lines))
        k = 0
        probes = [(l, x) for l in lines for x in line_exprs(l)] if OPS & {'name', 'access'} else []
        with ThreadPoolExecutor(J) as ex:
            tys = list(ex.map(lambda lx: probe_type(text, lx[0], lx[1]), probes))
        for (l, x), ty in zip(probes, tys):
            if ty:
                k += 1
                ops.append(('name %s : %s' % (x[:40], ty), op_name(l, x, ty, k, False)))
                if not IS_C:
                    ops.append(('nametop %s : %s' % (x[:40], ty), op_name(l, x, ty, k, True)))
        if 'access' in OPS:
            # base->field reads on culprit lines: type of the field (from the name probes) and of the base
            acc = [(l, x, ty) for (l, x), ty in zip(probes, tys) if ty and re.search(r'->\s*\w+$', x)]
            bprobes = [(l, re.sub(r'\s*->\s*\w+$', '', x)) for l, x, ty in acc]
            with ThreadPoolExecutor(J) as ex:
                btys = list(ex.map(lambda lx: probe_type(text, lx[0], lx[1]), bprobes))
            for (l, x, ty), bty in zip(acc, btys):
                if bty and bty.endswith('*'):
                    k += 1
                    ops.append(('access %s' % x[:40], op_access(l, x, ty, bty, k)))
        if 'flip' in OPS:
            for l in lines:
                for n in range(6):
                    ops.append(('flip#%d %s' % (n, l[:40]), op_flip(l, n)))
    top = decls(L, True)
    names = [m.group(4) for _, m in top]
    if 'move' in OPS:
        for n in names:
            for b in names + [None]:
                if b != n:
                    ops.append(('move %s before %s' % (n, b or 'END'), op_move(n, b)))
    alld = decls(L, False)
    if 'hoist' in OPS:
        for _, m in alld:
            if len(m.group(1).expandtabs()) != 4:
                ops.append(('hoist %s' % m.group(4), op_hoist(m.group(4))))
    if 'type' in OPS:
        for _, m in alld:
            if m.group(2) in ('int', 's32') and not m.group(3):
                ops.append(('type %s %s' % (m.group(4), 's32' if m.group(2) == 'int' else 'int'),
                            op_type(m.group(4), 's32' if m.group(2) == 'int' else 'int')))
    if 'width' in OPS:
        for _, m in alld:
            if not m.group(3) and m.group(2) in WIDTHS + ['s32']:
                for nt in WIDTHS:
                    if nt != m.group(2) and not (m.group(2) == 's32' and nt == 'int'):
                        ops.append(('width %s %s' % (m.group(4), nt), op_width(m.group(4), nt)))
    if 'ptrin' in OPS:
        for _, m in alld:
            if m.group(5) and (m.group(3) or '*' in m.group(2)):
                ops.append(('ptrin %s' % m.group(4), op_ptrin(m.group(4))))
    if 'inarg' in OPS:
        for _, m in alld:
            ops.append(('inarg %s' % m.group(4), op_inarg(m.group(4))))
    if 'tern' in OPS:
        hs, s, e = loc
        bl_ = text[s:e].split('\n')
        nt = sum(1 for k in range(len(bl_)) if _tern_at(bl_, k)) + sum(1 for l in bl_ if TERN_RE.match(l))
        for k in range(nt):
            ops.append(('tern #%d' % k, op_tern(k)))
    hs, s, e = loc
    seen = {}
    for name, a, b, argt, member in calls_in(text[s:e]):
        nth = seen.get(name, 0)
        seen[name] = nth + 1
        if 'wrap' in OPS:
            ops.append(('wrap %s#%d' % (name, nth), op_wrap(name, nth, None)))
        if 'temp' in OPS:
            ops.append(('temp %s#%d' % (name, nth), op_temp(name, nth, False)))
            ops.append(('temptop %s#%d' % (name, nth), op_temp(name, nth, True)))
            ops.append(('tempcast %s#%d' % (name, nth), op_tempcast(name, nth)))
    if 'expr' in OPS:   # expression-level edits (regsolve_expr.py)
        sys.path.insert(0, H)
        import regsolve_expr
        ops += regsolve_expr.make(text, regsolve_expr.Ctx(locate, decls, probe_type, IS_C))
    return ops


# ------------------------------------------------------------------ scoring
def body_asm(path, name):
    out, on = [], False
    for ln in open(path, encoding='utf-8', errors='replace'):
        ln = ln.rstrip('\n')
        if ln.startswith('.fn '):
            on = re.sub(r'@[0-9]+', '@N', ln.split()[1].rstrip(',').strip('"')) == re.sub(r'@[0-9]+', '@N', name)
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


EXACT0 = [None]   # functions (other than func) that are exact in the base source; set by the first score()
# --seeds N [--seeds-dir D]: also write up to N variants that compile to DIFFERENT code than the base
# (and each other) and score at most a little worse = new starting points for another search
SIGS = {}   # source text -> function body asm (registers included)
POOL = []   # (score, edit names, text) of every compiled variant


def norm_name(n):
    return re.sub(r'@[0-9]+', '@N', n)


def all_asm(path):
    """{function: normalised instruction lines} for every function of a disassembly"""
    out, cur = {}, None
    for ln in open(path, encoding='utf-8', errors='replace'):
        ln = ln.rstrip('\n')
        if ln.startswith('.fn '):
            cur = norm_name(ln.split()[1].rstrip(',').strip('"'))
            out[cur] = []
            continue
        if cur is not None:
            if ln.startswith('.endfn'):
                cur = None
                continue
            ln = re.sub(r'^/\* \S+ \S+\s+(\S\S ){4}\*/\s*', '', ln).strip()
            ln = re.sub(r'\.L_[0-9A-F]+', '.L', ln)
            ln = re.sub(r'"?@[0-9]+"?', '@N', ln)
            if ln and not ln.startswith('.L'):
                out[cur].append(ln)
    return out


def struct_dist(t, m):
    nr = lambda s: re.sub(r'\b[rf]\d+\b', 'R', s)
    a, b = Counter(nr(x) for x in t), Counter(nr(x) for x in m)
    return max(sum((a - b).values()), sum((b - a).values()))


def diff_count(t, m):
    sm = difflib.SequenceMatcher(None, t, m, autojunk=False)
    return sum(max(i2 - i1, j2 - j1) for op, i1, i2, j1, j2 in sm.get_opcodes() if op != 'equal')


slots = queue.Queue()
for i in range(J):
    slots.put(i)
os.makedirs(SCRATCH, exist_ok=True)
cache = {}
lock = threading.Lock()


def score(text):
    with lock:
        if text in cache:
            return cache[text]
    if DEADLINE and time.time() > DEADLINE:
        TIMED_OUT[0] = True
        return None
    slot = slots.get()
    try:
        # per-process folders/tags: several regsolve runs may work at once
        sd = SCRATCH + '/s%d_%d' % (os.getpid(), slot)
        os.makedirs(sd, exist_ok=True)
        path = '%s/%s' % (sd, os.path.basename(src_path_orig))
        open(path, 'w', encoding='latin-1', newline='').write(text.replace('\n', '\r\n') if CRLF else text)
        tag = '%sregsolve%d_%d' % (TAGP, os.getpid(), slot)
        r = subprocess.run([sys.executable, FCHECK, unit, '--tag', tag, '--src', path, '--diff', func],
                           capture_output=True, text=True)
        o = r.stdout + r.stderr
        d = os.path.join('build/scratch/fcheck', tag)
        if 'COMPILE FAILED' in o or not os.path.exists(os.path.join(d, 'M.s')) \
                or not os.path.exists(os.path.join(d, 'T.s')):
            res = None
        else:
            t = body_asm(os.path.join(d, 'T.s'), func)
            m = body_asm(os.path.join(d, 'M.s'), func)
            res = diff_count(t, m) if m else None
            if res is not None:
                with lock:
                    RAW[text] = res
            if m:
                with lock:
                    SIGS[text] = tuple(m)
            # shape changes (instructions only one side has, registers ignored) cost 10 each: the
            # line count alone preferred type edits that add an extsb/clrlwi and objdiff then got worse
            if res is not None:
                res += 10 * struct_dist(t, m)
            # helper edits change every function that inlines the helper. Functions that were exact
            # in the base source must stay exact (+100 per broken function).
            TA, MA = all_asm(os.path.join(d, 'T.s')), all_asm(os.path.join(d, 'M.s'))
            ex = set(k for k in TA if k != norm_name(func) and TA[k] == MA.get(k))
            with lock:
                if EXACT0[0] is None:
                    EXACT0[0] = ex
            if res is not None:
                res += 100 * len(EXACT0[0] - ex)
            for f_ in ('T.s', 'M.s'):
                try:
                    os.remove(os.path.join(d, f_))
                except OSError:
                    pass
    finally:
        slots.put(slot)
    with lock:
        cache[text] = res
    return res


def apply_all(text, fs):
    for f_ in fs:
        text = f_(text) if text is not None else None
    return text


def search(SRC0, base):
    """one solver pass from SRC0: singles, pairs of the best, greedy growth -> (score, names, text)"""
    ops = make_ops(SRC0)
    print('round 1: %d single edits' % len(ops))

    def evaluate(combo):
        names = [n for n, _ in combo]
        t = apply_all(SRC0, [f_ for _, f_ in combo])
        if t is None or t == SRC0:
            return names, None, None
        return names, score(t), t

    with ThreadPoolExecutor(J) as ex:
        r1 = list(ex.map(lambda op: evaluate([op]), ops))
    ok = sorted([(s_, n, t) for n, s_, t in r1 if s_ is not None], key=lambda x: x[0])
    POOL.extend(ok)
    fails = sum(1 for n, s_, t in r1 if s_ is None and t is not None)
    print('  compiled %d, compile failures %d' % (len(ok), fails))
    for s_, n, t in ok[:8]:
        print('  %4d  %s%s' % (s_, ' + '.join(n), '   EXACT' if s_ == 0 else ''))
    best = (base, [], SRC0)
    if ok and ok[0][0] < base:
        best = ok[0]
    if best[0] == 0:
        return best
    good = [n for s_, n, t in ok if s_ < base][:TOP] or [n for s_, n, t in ok if s_ == base][:TOP]
    byname = dict(ops)
    pairs = [[(a[0], byname[a[0]]), (b[0], byname[b[0]])] for a, b in itertools.combinations(good, 2)]
    print('round 2: %d pairs of the best %d singles' % (len(pairs), len(good)))
    with ThreadPoolExecutor(J) as ex:
        r2 = sorted([(s_, n, t) for n, s_, t in ex.map(evaluate, pairs) if s_ is not None], key=lambda x: x[0])
    POOL.extend(r2)
    for s_, n, t in r2[:5]:
        print('  %4d  %s%s' % (s_, ' + '.join(n), '   EXACT' if s_ == 0 else ''))
    if r2 and r2[0][0] < best[0]:
        best = r2[0]
    rounds = 0
    while best[0] and best[1] and rounds < 4:
        rounds += 1
        cur = [(n, byname[n]) for n in best[1]]
        cand = [cur + [(g[0], byname[g[0]])] for g in good if g[0] not in best[1]]
        with ThreadPoolExecutor(J) as ex:
            r3 = sorted([(s_, n, t) for n, s_, t in ex.map(evaluate, cand) if s_ is not None], key=lambda x: x[0])
        if not r3 or r3[0][0] >= best[0]:
            break
        best = r3[0]
        print('round 3.%d: %4d  %s' % (rounds, best[0], ' + '.join(best[1])))
    return best


base = score(SRC)
print('%s %s: base %s differing lines' % (unit, func, base))
if base is None:
    print('base does not compile / function not found / timed out')
    print('RESULT %s %s -1 -1 NONE' % (unit, func))
    sys.exit(2)
cur, steps = SRC, []
cur_score = base
for it in range(int(opt('--iter', 4))):
    # every pass restarts from the best text so far (new single edits become possible after each win)
    if it:
        print('\n-- pass %d from %d lines' % (it + 1, cur_score))
    s_, names, text = search(cur, cur_score)
    if not names or s_ >= cur_score:
        break
    cur, cur_score = text, s_
    steps += names
    if s_ == 0:
        break

def objdiff_pct(text):
    """objdiff fuzzy % of the function for a full source text (the line count can disagree with objdiff)"""
    d = SCRATCH + '/o%d' % os.getpid()
    os.makedirs(d, exist_ok=True)
    p = '%s/%s' % (d, os.path.basename(src_path_orig))
    open(p, 'w', encoding='latin-1', newline='').write(text.replace('\n', '\r\n') if CRLF else text)
    r = subprocess.run([sys.executable, FCHECK, unit, '--all', '--tag', '%sregsolveo%d' % (TAGP, os.getpid()), '--src', p],
                       capture_output=True, text=True)
    for m in re.finditer(r'^\s*([\d.]+)%\s+\d+\s+(\S+)', r.stdout, re.M):
        if re.sub(r'@[0-9]+', '@N', m.group(2)) == re.sub(r'@[0-9]+', '@N', func):
            return float(m.group(1))
    return 100.0 if 'COMPILE FAILED' not in r.stdout + r.stderr else None


pct = ''
if steps:
    p0, p1 = objdiff_pct(SRC), objdiff_pct(cur)
    pct = '   [objdiff %s%% -> %s%%%s]' % (p0, p1, '' if (p1 or 0) > (p0 or 0) else '  NOT BETTER')
print('\nBEST %s -> %s : %s%s%s' % (base, cur_score, ' + '.join(steps) or '(no improvement)', pct,
                                '   (stopped by --timeout)' if TIMED_OUT[0] else ''))
if steps:
    out = opt('--write') or SCRATCH + '/best_%s%s' % (re.sub(r'\W', '_', func)[:40], ext)
    open(out, 'w', encoding='latin-1', newline='').write(cur.replace('\n', '\r\n') if CRLF else cur)
    sm = difflib.unified_diff(SRC.split('\n'), cur.split('\n'), lineterm='', n=1)
    print('\n'.join(list(sm)[2:80]))
    print('written: %s' % out)

if opt('--seeds'):
    # the best text so far is seed 0 when it improved; then distinct-code variants within the margin
    nseeds = int(opt('--seeds'))
    sdir = opt('--seeds-dir') or SCRATCH + '/seeds_%s' % re.sub(r'\W', '_', func)[:40]
    os.makedirs(sdir, exist_ok=True)
    for old in os.listdir(sdir):
        os.remove(os.path.join(sdir, old))
    margin = max(10, cur_score // 3)
    used = {SIGS.get(SRC)}
    picked = []
    for s_, n, t in sorted(([(cur_score, steps, cur)] if steps else []) + POOL, key=lambda x: x[0]):
        sg = SIGS.get(t)
        if sg is None or sg in used or s_ > cur_score + margin:
            continue
        used.add(sg)
        picked.append((s_, n, t))
        if len(picked) >= nseeds:
            break
    for k, (s_, n, t) in enumerate(picked):
        p = os.path.join(sdir, 'seed%d_%d%s' % (k, s_, ext))
        open(p, 'w', encoding='latin-1', newline='').write(t.replace('\n', '\r\n') if CRLF else t)
        print('SEED %s  score %d  (%s)' % (p, s_, ' + '.join(n)))
    print('%d seeds written (margin +%d over %d)' % (len(picked), margin, cur_score))

raw_base = RAW.get(SRC, base)
raw_best = RAW.get(cur, cur_score) if steps else raw_base
best_path = (out if steps else 'NONE')
print('RESULT %s %s %s %s %s' % (unit, func, raw_base, raw_best, best_path.replace('\\', '/') if steps else 'NONE'))
