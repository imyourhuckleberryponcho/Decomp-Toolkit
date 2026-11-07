"""tricks.py - library of small, behaviour-preserving source rewrites for matching decompilation.

Every trick is a pure function  body_text -> iterator of (description, new_body_text)  that yields every place it can be
applied, one variant per place. `body_text` is the text between the braces of ONE function. Nothing here compiles
anything: tricksweep.py (the sweep) and your own scripts do that. A trick must keep the program's values the same;
what it changes is the shape the compiler sees (declaration order, statement order, operand order, loop form,
integer type spelling ...), which moves register allocation and instruction order. Tricks are guarded against the
obvious semantic traps (side effects, narrowing, precedence) but they are text rewrites, not a proof: read the diff
before installing a winner. Never: volatile, comma operator hacks, `(0, x)`, empty ifs.

Use from Python:
    import tricks
    for desc, new_text in tricks.variants(file_text, 'MyClass::method'):  # whole-file text in, whole-file text out
        ...
    tricks.TRICKS                      # [Trick(name, kind, description, fn), ...]
    tricks.source_names('func__5ClassFi')   # spellings to look for in the source
"""
import re
from collections import Counter
from typing import NamedTuple

NL = chr(10)


DECL = re.compile(r'^(\s+)((?:const\s+|volatile\s+|unsigned\s+|signed\s+|struct\s+)*[A-Za-z_]\w*(?:\s*\*+)?)\s+'
                  r'(\**[A-Za-z_]\w*)(\[[^\]]*\])?(\s*=\s*[^;]+)?;\s*$')


# spellings of the SAME 32-bit type on the target compiler (long is 32 bits there): swapping them never changes values
TYPE_SWAPS = [('s32', 'int'), ('int', 's32'), ('u32', 'unsigned int'), ('unsigned int', 'u32'),
              ('int', 'long'), ('long', 'int'), ('s32', 'long'), ('long', 's32'),
              ('u32', 'unsigned long'), ('unsigned long', 'u32')]


def _words(text):
    return set(re.findall(r'[A-Za-z_]\w*', text))


def _lvalue(stmt):
    # (lvalue text, rhs text) of a plain `lv = rhs;` / `lv op= rhs;` statement, else None
    m = re.match(r'^\s*([A-Za-z_][\w.\->\[\]]*)\s*(?:[-+*/|&^]|<<|>>)?=(?!=)\s*(.*);\s*$', stmt)
    if m:
        return (m.group(1), m.group(2)) if not re.search(r'\+\+|--', m.group(2)) else None
    m = re.match(r'^\s*(?:(\+\+|--)\s*([A-Za-z_][\w.\->\[\]]*)|([A-Za-z_][\w.\->\[\]]*)\s*(\+\+|--))\s*;\s*$', stmt)
    if m:
        return (m.group(2) or m.group(3), '')
    return None


def _independent(a, b):
    # True when statements a and b can swap places without changing values (conservative; memory aliasing between
    # different pointers is NOT analysed)
    for t in (a, b):
        if re.search(r'\bgoto\b|\breturn\b', t):
            return False
    ca = re.search(r'[A-Za-z_]\w*\s*\(', a) and not re.match(r'^\s*(?:\(|sizeof)', a)
    cb = re.search(r'[A-Za-z_]\w*\s*\(', b) and not re.match(r'^\s*(?:\(|sizeof)', b)
    if ca and cb:
        return False
    la, lb = _lvalue(a), _lvalue(b)
    if la is None and lb is None:
        return False
    for mine, other_stmt in ((la, b), (lb, a)):
        if mine is None:
            # a bare call can change anything the other statement touches
            return False
        lv = mine[0]
        if re.search(r'(?<![\w.>])' + re.escape(lv) + r'(?![\w])', other_stmt):
            return False
        root = re.match(r'[A-Za-z_]\w*', lv).group(0)
        if lv == root and root in _words(other_stmt):
            return False
    return True


def _has_store_or_call(text):
    if re.search(r'(?:->|\.|\])\s*\w*\s*(?:[-+*/|&^]|<<|>>)?=(?!=)|\*\s*\w+\s*(?:[-+*/|&^])?=(?!=)', text):
        return True
    for m in re.finditer(r'\b([A-Za-z_]\w*)\s*\(', text):
        if m.group(1) not in ('if', 'while', 'for', 'switch', 'return', 'sizeof'):
            return True
    return False


def split_decls(body):
    lines = body.split('\n')
    runs, cur = [], []
    for i, l in enumerate(lines):
        if DECL.match(l) and '(' not in l.split('=')[0] and not l.strip().startswith('return'):
            cur.append(i)
        else:
            if len(cur) >= 1:
                runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)
    return lines, runs


_OPND = r'(?:[A-Za-z_][\w:]*\([^()]*\)|[A-Za-z_][\w\.]*(?:->[\w]+)*(?:\[[^\[\]]*\])?|0x[0-9A-Fa-f]+|\d+)'


_FLIP = {'<': '>', '>': '<', '<=': '>=', '>=': '<=', '==': '==', '!=': '!='}


def _split_cmp(expr):
    depth = 0
    hits = []
    i = 0
    while i < len(expr):
        c = expr[i]
        if c in '([':
            depth += 1
        elif c in ')]':
            depth -= 1
        elif depth == 0:
            two = expr[i:i + 2]
            if two in ('&&', '||'):
                return None
            if two in ('==', '!=', '<=', '>='):
                hits.append((i, two))
                i += 2
                continue
            if c in '<>' and expr[i + 1:i + 2] not in ('<', '>') and expr[i - 1:i] not in ('<', '>', '-'):
                hits.append((i, c))
        i += 1
    if len(hits) != 1:
        return None
    pos, op = hits[0]
    return expr[:pos].strip(), op, expr[pos + len(op):].strip()


def _split_top(expr, ops):
    parts, depth, cur, i = [], 0, '', 0
    while i < len(expr):
        ch = expr[i]
        if ch in '([':
            depth += 1
        elif ch in ')]':
            depth -= 1
        if depth == 0 and ch in ops and expr[i - 1:i] == ' ' and expr[i + 1:i + 2] == ' ':
            parts.append(cur.strip())
            parts.append(ch)
            cur = ''
        else:
            cur += ch
        i += 1
    parts.append(cur.strip())
    if len(parts) < 3:
        return None
    for p in parts[0:-1:2]:
        if _split_low(p):
            return None
    return parts


def _split_low(expr):
    depth = 0
    for i, ch in enumerate(expr):
        if ch in '([':
            depth += 1
        elif ch in ')]':
            depth -= 1
        elif depth == 0 and (ch in '+-<>=&|^?' and expr[i - 1:i] == ' ' and expr[i + 1:i + 2] in (' ', '=')):
            return True
    return False


_NEG = {'==': '!=', '!=': '==', '<': '>=', '>=': '<', '>': '<=', '<=': '>'}


def _close(body, i):
    depth, k, n = 0, i, len(body)
    while k < n:
        c = body[k]
        if c in '"\'':
            q, k = c, k + 1
            while k < n and body[k] != q:
                k += 2 if body[k] == '\\' else 1
        elif c == '/' and body[k + 1:k + 2] == '/':
            k = body.find('\n', k)
            if k < 0:
                return None
            continue
        elif c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                return k
        k += 1
    return None


def _balanced(e):
    return e.count('(') == e.count(')') and e.count('[') == e.count(']')


def _neg_cond(c):
    c = c.strip()
    if c.startswith('(') and c.endswith(')') and _balanced(c[1:-1]):
        c = c[1:-1].strip()
    if '&&' not in c and '||' not in c and '?' not in c:
        for m in re.finditer(r'(?<![<>=!-])(==|!=|<=|>=|<|>)(?![<>=])', c):
            a, b = c[:m.start()].strip(), c[m.end():].strip()
            if a and b and _balanced(a) and _balanced(b):
                return '%s %s %s' % (a, _NEG[m.group(1)], b)
    m = re.match(r'^!\((.*)\)$', c)
    if m and _balanced(m.group(1)):
        return m.group(1)
    if re.match(r'^![\w.\->\[\]]+$', c):
        return c[1:]
    if re.match(r'^[\w.\->\[\]]+$', c):
        return '!' + c
    return '!(%s)' % c


def _reindent(block, delta):
    out = []
    for l in block.split('\n'):
        if not l.strip():
            out.append(l)
        elif delta >= 0:
            out.append(' ' * delta + l)
        else:
            k = len(l) - len(l.lstrip(' '))
            out.append(l[min(k, -delta):])
    return '\n'.join(out)


def _top_level(e, ops):
    parts, depth, cur, i = [], 0, '', 0
    while i < len(e):
        c = e[i]
        if c in '([':
            depth += 1
        elif c in ')]':
            depth -= 1
        if depth == 0:
            hit = next((o for o in ops if e.startswith(o, i)), None)
            if hit:
                parts.append(cur)
                cur, i = '', i + len(hit)
                continue
        cur += c
        i += 1
    parts.append(cur)
    return parts


def _block_end(s, i):
    depth = 0
    for k in range(i, len(s)):
        if s[k] == '{':
            depth += 1
        elif s[k] == '}':
            depth -= 1
            if not depth:
                return k + 1
    return -1


_LOAD_LOCAL = re.compile(r'^(\s+)(?:const\s+)?(?:f32|f64|s8|u8|s16|u16|s32|u32|int|float|[A-Z]\w*\*?)\s+(\w+) = '
                         r'([A-Za-z_(][^;=]*(?:\[[^\]]*\]|->\w+|\.\w+)[^;=]*);\s*$', re.M)


_T = r'(?:int|s8|u8|s16|u16|s32|u32|f32|bool|BOOL)'


_INT = r'(?:int|s8|u8|s16|u16|s32|u32|long|bool|BOOL)'


def _decl_type(body, name):
    m = re.search(r'\b((?:unsigned\s+)?(?:int|s8|u8|s16|u16|s32|u32|f32|f64|long|char|short|float|double))\s+'
                  + re.escape(name) + r'\b\s*[=;,)]', body)
    return m.group(1) if m else None


_ATOM = r'(?:[A-Za-z_][\w]*(?:(?:->|\.)\w+|\[[^\[\]]*\])*|0x[0-9A-Fa-f]+|\d+)'


_PAREN = r'\((?:[^()]|\([^()]*\))*\)'


def _decl_pair_ok(a, b):
    # two declaration lines may change places when neither initialiser uses the other's variable and the
    # evaluation order of two initialising calls is not changed
    ma, mb = DECL.match(a), DECL.match(b)
    if not ma or not mb:
        return False
    ia, ib = ma.group(5), mb.group(5)
    na, nb = ma.group(3).lstrip('*'), mb.group(3).lstrip('*')
    if ia and re.search(r'\b%s\b' % re.escape(nb), ia):
        return False
    if ib and re.search(r'\b%s\b' % re.escape(na), ib):
        return False
    return not (ia and ib and ('(' in ia or '(' in ib))


def t_decl_swaps(body):
    lines, runs = split_decls(body)
    for idx in runs:
        for a in range(len(idx)):
            for b in range(a + 1, len(idx)):
                span = [lines[k] for k in idx[a:b + 1]]
                if not (all(_decl_pair_ok(lines[idx[a]], l) for l in span[1:]) and
                        all(_decl_pair_ok(lines[idx[b]], l) for l in span[:-1])):
                    continue
                l2 = lines[:]
                l2[idx[a]], l2[idx[b]] = l2[idx[b]], l2[idx[a]]
                yield 'swap decl %s <-> %s' % (lines[idx[a]].strip(), lines[idx[b]].strip()), '\n'.join(l2)


def t_decl_move(body):
    lines, runs = split_decls(body)
    for idx in runs:
        for a in idx:
            for dest in (idx[0], idx[-1]):
                if dest == a:
                    continue
                crossed = [lines[k] for k in idx if min(a, dest) <= k <= max(a, dest) and k != a]
                if not all(_decl_pair_ok(lines[a], l) for l in crossed):
                    continue
                l2 = lines[:]
                line = l2.pop(a)
                l2.insert(dest, line)
                yield 'move decl %s to %s' % (line.strip(), 'top' if dest == idx[0] else 'bottom'), '\n'.join(l2)



def t_decl_hoist(body):
    lines, runs = split_decls(body)
    if not runs:
        return
    top = runs[0]
    base_indent = len(lines[top[0]]) - len(lines[top[0]].lstrip())
    for i, l in enumerate(lines):
        m = DECL.match(l)
        if not m or i in top or '(' in l.split('=')[0] or l.strip().startswith('return'):
            continue
        if len(m.group(1)) <= base_indent or m.group(4):
            continue
        typ, name, init = m.group(2), m.group(3), m.group(5)
        if name.startswith('*'):
            typ, name = typ + ' ' + name[:len(name) - len(name.lstrip('*'))], name.lstrip('*')
        decl = ' ' * base_indent + '%s %s;' % (typ, name)
        for k, dest in enumerate(top + [top[-1] + 1]):
            l2 = lines[:]
            if init:
                l2[i] = m.group(1) + name + init.rstrip() + ';'
            else:
                l2.pop(i)
            l2.insert(dest, decl)
            yield 'hoist decl %s to top #%d' % (l.strip()[:50], k), NL.join(l2)


def t_types(body):
    lines, runs = split_decls(body)
    for i in [i for r in runs for i in r]:
        m = DECL.match(lines[i])
        t = m.group(2).strip()
        for a, b in TYPE_SWAPS:
            if t == a:
                l2 = lines[:]
                l2[i] = lines[i].replace(a + ' ', b + ' ', 1)
                yield 'type %s: %s -> %s' % (m.group(3), a, b), '\n'.join(l2)


def t_bool_forms(body):
    for m in re.finditer(r'(\w[\w\.\->\[\]]*) != 0(?=\s*[,)])', body):
        yield '(bool)%s instead of != 0' % m.group(1), body[:m.start()] + '(bool)' + m.group(1) + body[m.end():]
    for m in re.finditer(r'\(bool\)(\w[\w\.\->\[\]]*)', body):
        yield '%s != 0 instead of (bool)' % m.group(1), body[:m.start()] + m.group(1) + ' != 0' + body[m.end():]
    for m in re.finditer(r'if \(!(\w[\w\.\->\[\]]*)\)', body):
        yield '%s == 0 instead of !' % m.group(1), body[:m.start()] + 'if (%s == 0)' % m.group(1) + body[m.end():]
    for m in re.finditer(r'if \((\w[\w\.\->\[\]]*) == 0\)', body):
        yield '!%s instead of == 0' % m.group(1), body[:m.start()] + 'if (!%s)' % m.group(1) + body[m.end():]
    for m in re.finditer(r'\((\w[\w\.\->\[\]]*) & (0x[0-9A-Fa-f]+|\d+)\) == 0', body):
        yield '!(%s & %s) instead of == 0' % m.group(1, 2), body[:m.start()] + '!(%s & %s)' % m.group(1, 2) + body[m.end():]
    for m in re.finditer(r'!\((\w[\w\.\->\[\]]*) & (0x[0-9A-Fa-f]+|\d+)\)', body):
        yield '(%s & %s) == 0 instead of !' % m.group(1, 2), body[:m.start()] + '(%s & %s) == 0' % m.group(1, 2) + body[m.end():]


def t_cmp_swap(body):
    for m in re.finditer(r'(?<![\w\.\]>])([A-Za-z_][\w\.\->]*) (==|!=) ([A-Za-z_][\w\.\->]*|0x[0-9A-Fa-f]+|\d+)\b(?!\s*[\(\[\.])', body):
        a, op, b = m.groups()
        yield 'swap %s %s %s' % (a, op, b), body[:m.start()] + '%s %s %s' % (b, op, a) + body[m.end():]


def t_commute(body):
    for m in re.finditer(r'(?<![\w\.\]>)])(%s) (\+|\*|\||&|\^|<=|>=|==|!=|<|>) (%s)(?![\w\(\[\.]|->)' % (_OPND, _OPND), body):
        a, op, b = m.groups()
        if '(' in a and '(' in b:
            continue
        pre = body[:m.start()].rstrip()
        post = body[m.end():].lstrip()
        if not (pre.endswith(('(', ',', '=', '{', ';', '?', ':', '&&', '||', 'return')) and
                post.startswith((')', ';', ',', '&&', '||', '?', ':'))):
            continue
        new_op = _FLIP.get(op, op)
        yield 'commute %s %s %s' % (a, op, b), body[:m.start()] + '%s %s %s' % (b, new_op, a) + body[m.end():]


def t_inline_cmp(body):
    for m in re.finditer(r'^(\s+)(?:(?:const\s+)?(?:unsigned\s+)?[A-Za-z_][\w:]*\s*\**\s+)?([A-Za-z_]\w*) = ([^;\n]+);\n(\s+)(if|while) \(\2(==|!=|<=|>=|<|>) ([^()\n]*(?:\([^()]*\))?[^()\n]*)\) \{',
                         body, re.M):
        ind, x, expr, ind2, kw, op, y = m.groups()
        if '(' in y:
            continue
        rest = body[:m.start()] + body[m.end():]
        if re.search(r'(?<![\w.>])%s(?!\w)' % re.escape(x), re.sub(r'^\s+[\w:<>*\s]+[\s*]%s\s*;\s*$' % re.escape(x), '',
                                                                  rest, flags=re.M)):
            continue
        for new in ('%s%s (%s %s %s) {' % (ind2, kw, y, _FLIP.get(op, op), expr), '%s%s (%s %s %s) {' % (ind2, kw, expr, op, y)):
            yield 'inline %s into compare: %s' % (x, new.strip()[:60]), body[:m.start()] + new + body[m.end():]


def t_call_local_left(body):
    for m in re.finditer(r'^(\s+)(if|while) \((.+)\) \{$', body, re.M):
        ind, kw, inner = m.groups()
        sp = _split_cmp(inner)
        if not sp:
            continue
        a, op, b = sp
        for call, other, nop in ((a, b, op), (b, a, _FLIP.get(op, op))):
            cm = re.match(r'^(?:\((?:u8|u16|u32|s8|s16|s32|int|bool)\))?[A-Za-z_][\w:]*(?:->|\.)?[\w]*\(.*\)$', call)
            if not cm or not other or '(' in other and other.count('(') != other.count(')'):
                continue
            for ty in ('s32', 'u32'):
                if kw == 'while':
                    continue
                new = '%s%s cmpv = %s;\n%sif (cmpv %s %s) {' % (ind, ty, call, ind, nop, other)
                yield 'call into %s local on the left: %s' % (ty, inner[:50]), body[:m.start()] + new + body[m.end():]


def t_sink_invariant(body):
    for m in re.finditer(r'^(\s+)([A-Za-z_]\w*) = ([^;\n]+?) ([-+*]) ([\w.]+);$', body, re.M):
        ind, x, expr, op, c = m.groups()
        xt = _decl_type(body[:m.start()], x)
        if xt not in ('int', 's32', 'u32', 'f32', 'f64', 'long', 'float', 'double', 'unsigned int'):
            continue
        after = body[m.end():]
        uses = list(re.finditer(r'(?<![\w.>])%s(?![\w(])' % re.escape(x), after))
        if len(uses) != 1 or re.match(r'\s*=[^=]', after[uses[0].end():]):
            continue
        u = m.end() + uses[0].start()
        nb = body[:m.start()] + '%s%s = %s;' % (ind, x, expr) + body[m.end():u] + '(%s %s %s)' % (x, op, c) + body[u + len(x):]
        yield 'sink "%s %s" into the use of %s' % (op, c, x), nb


def t_stmt_swap(body):
    lines = body.split(NL)
    simple = re.compile(r'^\s+[^\s{}][^{}]*;\s*$')
    for i in range(len(lines) - 1):
        a, b = lines[i], lines[i + 1]
        if not (simple.match(a) and simple.match(b)):
            continue
        if DECL.match(a) or DECL.match(b) or a.strip().startswith(('return', 'break', 'continue', 'goto')) \
                or b.strip().startswith(('return', 'break', 'continue', 'goto')):
            continue
        if len(a) - len(a.lstrip()) != len(b) - len(b.lstrip()):
            continue
        if not _independent(a, b):
            continue
        l2 = lines[:]
        l2[i], l2[i + 1] = b, a
        yield 'swap statements: %s <-> %s' % (a.strip()[:50], b.strip()[:50]), NL.join(l2)


def t_stmt_move(body):
    lines = body.split(NL)
    simple = re.compile(r'^\s+[^\s{}][^{}]*;\s*$')
    def ok(l):
        return simple.match(l) and not DECL.match(l) and not l.strip().startswith(('return', 'break', 'continue', 'goto', 'static', 'const '))
    for i in range(len(lines)):
        if not ok(lines[i]):
            continue
        ind = len(lines[i]) - len(lines[i].lstrip())
        for d in (-6, -5, -4, -3, -2, 2, 3, 4, 5, 6):
            j = i + d
            lo, hi = min(i, j), max(i, j)
            if lo < 0 or hi >= len(lines):
                continue
            span = lines[lo:hi + 1]
            if not all(ok(l) and len(l) - len(l.lstrip()) == ind for l in span):
                continue
            if not all(_independent(lines[i], l) for k, l in enumerate(span, lo) if k != i):
                continue
            l2 = lines[:]
            s = l2.pop(i)
            l2.insert(j, s)
            yield 'move statement %+d: %s' % (d, lines[i].strip()[:50]), NL.join(l2)


def t_hoist_call_arg(body):
    for m in re.finditer(r'^(\s+)((?:[\w.\->\[\]]+\s*=\s*)?[A-Za-z_][\w:]*\()([^;\n]*)\);\s*$', body, re.M):
        ind, head, args_s = m.groups()
        args, depth, cur = [], 0, ''
        for ch in args_s:
            depth += {'(': 1, ')': -1}.get(ch, 0)
            if ch == ',' and depth == 0:
                args.append(cur); cur = ''
            else:
                cur += ch
        args.append(cur)
        if depth != 0:
            continue
        for k in range(1, len(args)):
            a = args[k].strip()
            if not re.match(r'^(\([\w\s\*]+\)\s*)?[A-Za-z_]\w*\(.*\)$', a):
                continue
            for ty in ('s32', 'u32'):
                na = args[:k] + [' tmp%d' % k] + args[k + 1:]
                new = '%s%s tmp%d = %s;\n%s%s%s);' % (ind, ty, k, a, ind, head, ','.join(na).replace(', tmp', ', tmp'))
                yield 'hoist arg %d of %s into %s local' % (k, head.strip()[:30], ty), body[:m.start()] + new + body[m.end():]


def t_inline_arg(body):
    for m in re.finditer(r'^(\s+)(?:((?:const\s+)?[\w:]+\s*\**)\s+)?(\w+) = ([^;\n]+);\n([^\n]*)\n', body, re.M):
        ty, x, expr, nxt = m.group(2), m.group(3), m.group(4), m.group(5)
        if ty in ('return', 'else', 'case') or '(' not in nxt:
            continue
        pat = r'(?<![\w.>])%s(?![\w(])' % re.escape(x)
        if len(re.findall(pat, nxt)) != 1 or re.search(r'%s\s*=[^=]' % pat, nxt):
            continue
        before, after = body[:m.start()], body[m.end():]
        decl = None
        if not ty:
            dm = list(re.finditer(r'^\s+((?:const\s+)?[\w:]+\s*\**)\s*%s;\n' % re.escape(x), before, re.M))
            if not dm:
                continue
            decl, ty = dm[-1], dm[-1].group(1)
        others = re.sub(r'^\s+[\w:\s*]+[\s*]%s\s*;\s*$' % re.escape(x), '', before + after, flags=re.M)
        if re.search(pat, others):
            continue
        u = re.search(pat, nxt)
        new_nxt = nxt[:u.start()] + '(%s)(%s)' % (ty.strip(), expr) + nxt[u.end():]
        nb = before + m.group(1) + new_nxt.lstrip() + '\n' + after
        if decl is not None:
            nb = nb[:decl.start()] + nb[decl.end():]
        yield 'inline temp %s into the next call' % x, nb


def t_split_chain(body):
    for m in re.finditer(r'^(\s+)(\w+) = \2 ([*/]) ([^;\n]+);$', body, re.M):
        ind, v, op1, rest = m.groups()
        parts = _split_top(rest, '*/')
        if not parts or _split_low(parts[-1]):
            continue
        stmts =['%s%s %s= %s;' % (ind, v, op1, parts[0])]
        for k in range(1, len(parts), 2):
            stmts.append('%s%s %s= %s;' % (ind, v, parts[k], parts[k + 1]))
        yield 'split chain on %s into %d steps' % (v, len(stmts)), body[:m.start()] + '\n'.join(stmts) + body[m.end():]
    for m in re.finditer(r'^(\s+)return (\w+) ([*/]) ([^;\n]+);$', body, re.M):
        ind, v, op1, rest = m.groups()
        parts = _split_top(rest, '*/')
        if not parts:
            continue
        new = '%s%s %s= %s;\n%sreturn %s %s %s;' % (ind, v, op1, parts[0], ind, v, parts[1], ' '.join(parts[2:]))
        yield 'split return chain on %s' % v, body[:m.start()] + new + body[m.end():]


def t_for_init_merge(body):
    for m in re.finditer(r'^(\s+)(?:((?:const\s+)?[\w:]+\s*\**)\s+)?(\w+) = ([^;\n,]+);\n\1for \((?:((?:const\s+)?[\w:]+\s*\**)\s+)?(\w+) = ([^;\n,]+);',
                         body, re.M):
        ind, t1, x, v1, t2, i, v2 = m.groups()
        decls = ''
        if t1:
            decls += '%s%s %s;\n' % (ind, t1.strip(), x)
        if t2:
            decls += '%s%s %s;\n' % (ind, t2.strip(), i)
        for init in ('%s = %s, %s = %s' % (i, v2, x, v1), '%s = %s, %s = %s' % (x, v1, i, v2)):
            yield 'merge %s into for init (%s)' % (x, init), body[:m.start()] + decls + '%sfor (%s;' % (ind, init) + body[m.end():]
        if t1 and t2:
            d2 = '%s%s %s;\n%s%s %s;\n' % (ind, t2.strip(), i, ind, t1.strip(), x)
            yield 'merge %s into for init, decls swapped' % x, body[:m.start()] + d2 + '%sfor (%s = %s, %s = %s;' % (ind, i, v2, x, v1) + body[m.end():]


def t_chained_init(body):
    pat = (r'^(\s+)(?:((?:const\s+)?[\w:]+\s*\**)\s+)?(\w+) = (0|0\.0f?|NULL);\n'
           r'((?:\1[^\n]*\n){0,2}?)\1for \((?:((?:const\s+)?[\w:]+\s*\**)\s+)?(\w+) = (0|0\.0f?|NULL);')
    for m in re.finditer(pat, body, re.M):
        ind, t1, x, v1, mid, t2, i, v2 = m.groups()
        if x == i:
            continue
        decls = ''
        if t1:
            decls += '%s%s %s;\n' % (ind, t1.strip(), x)
        if t2:
            decls += '%s%s %s;\n' % (ind, t2.strip(), i)
        for init in ('%s = %s = 0' % (i, x), '%s = %s = 0' % (x, i)):
            yield 'chained for init (%s)' % init, body[:m.start()] + decls + mid + '%sfor (%s;' % (ind, init) + body[m.end():]
    for m in re.finditer(r'for \((\w+) = 0, (\w+) = 0;', body):
        a, b = m.groups()
        for init in ('%s = %s = 0' % (a, b), '%s = %s = 0' % (b, a)):
            yield 'chained for init (%s)' % init, body[:m.start()] + 'for (%s;' % init + body[m.end():]


def t_return_ifelse(body):
    for m in re.finditer(r'^(\s+)return (\w[\w.\->\[\]]*) (==|!=) 0;', body, re.M):
        ind, x, op = m.groups()
        t, f = ('false', 'true') if op == '==' else ('true', 'false')
        new = '%sif (%s) {\n%s    return %s;\n%s} else {\n%s    return %s;\n%s}' % (ind, x, ind, t, ind, ind, f, ind)
        yield 'return %s %s 0 -> if (%s) else' % (x, op, x), body[:m.start()] + new + body[m.end():]
    for m in re.finditer(r'^(\s+)return (\w[\w.\->\[\]]*) (==|!=) 0 \? (true|false) : (true|false);', body, re.M):
        ind, x, op, a, b = m.groups()
        t, f = (b, a) if op == '==' else (a, b)
        new = '%sif (%s) {\n%s    return %s;\n%s} else {\n%s    return %s;\n%s}' % (ind, x, ind, t, ind, ind, f, ind)
        yield 'ternary return -> if (%s) else' % x, body[:m.start()] + new + body[m.end():]


def t_ptr_to_index(body):
    for m in re.finditer(r'^(\s+)((?:const\s+)?[\w:]+\s*\*)\s*(\w+) = ([A-Za-z_][\w.\->]*);\n', body, re.M):
        ind, ty, p, arr = m.groups()
        rest = body[m.end():]
        f = re.search(r'for \(([^;]*);([^;]*);\s*(\w+)\+\+,\s*%s\+\+\)\s*\{\n' % re.escape(p), rest)
        if not f or re.search(r'(?<![\w.>])%s(?!\w)' % re.escape(p), rest[:f.start()]):
            continue
        i = f.group(3)
        lend = _block_end(rest, f.end() - 1)
        if lend < 0 or re.search(r'(\+\+|--)\s*%s\b|\b%s\s*(\+\+|--|[-+*/|&^]?=(?!=))' % (re.escape(p), re.escape(p)), rest[f.end():lend]):
            continue
        ind2 = re.match(r'(\s*)', rest[f.end():]).group(1)
        new_for = 'for (%s;%s; %s++) {\n%s%s %s = &%s[%s];\n' % (f.group(1), f.group(2), i, ind2, ty.strip(), p, arr, i)
        nb = body[:m.start()] + rest[:f.start()] + new_for + rest[f.end():]
        yield 'pointer walk %s -> &%s[%s]' % (p, arr, i), nb


def t_if_to_switch(body):
    for m in re.finditer(r'^(\s+)if \(([^()\n]*(?:\([^()\n]*\)[^()\n]*)*) == ((?:0x)?[0-9A-Fa-f]+)\) \{\n', body, re.M):
        ind, expr, k = m.groups()
        i, depth = m.end() - 2, 0
        for j in range(i, len(body)):
            if body[j] == '{':
                depth += 1
            elif body[j] == '}':
                depth -= 1
                if depth == 0:
                    break
        else:
            continue
        if re.match(r'\s*else\b', body[j + 1:]):
            continue
        inner = body[m.end():j]
        if re.search(r'\b(?:break|continue)\b', inner):
            continue
        expr2 = re.sub(r'^\(s32\)\s*', '', expr.strip())
        lines = [('    ' + l if l.strip() else l) for l in inner.rstrip('\n ').split('\n')]
        new = '%sswitch (%s) {\n%scase %s:\n%s\n%s    break;\n%s}' % (ind, expr2, ind, k, '\n'.join(lines), ind, ind)
        yield 'if (%s == %s) -> one-case switch' % (expr2[:30], k), body[:m.start()] + new + body[j + 1:]


def t_return_via_local(body):
    lines, runs = split_decls(body)
    if not runs:
        return
    names = []
    for k in runs[0]:
        m = DECL.match(lines[k])
        if m and not m.group(4) and (m.group(2).strip() in ('int', 's32', 'u32', 'long', 'unsigned int')
                                     or '*' in m.group(2) or m.group(3).startswith('*')):
            names.append(m.group(3).lstrip('*'))
    for m in re.finditer(r'^(\s+)return ([A-Za-z_][\w:]*\([^;\n]*\));\s*$', body, re.M):
        for v in names:
            new = '%s%s = %s;\n%sreturn %s;' % (m.group(1), v, m.group(2), m.group(1), v)
            yield 'return %s via local %s' % (m.group(2)[:30], v), body[:m.start()] + new + body[m.end():]


def t_postinc_index(body):
    for m in re.finditer(r'^(\s+)([A-Za-z_][\w\.\->]*)\[([A-Za-z_]\w*)\] = ([^;\n]+);\n\s+\3(\+\+|\s*\+= 1);\n', body, re.M):
        ind, arr, i, v = m.group(1), m.group(2), m.group(3), m.group(4)
        if re.search(r'\b%s\b' % re.escape(i), v):
            continue
        yield '%s[%s++] = ...' % (arr[:20], i), body[:m.start()] + '%s%s[%s++] = %s;\n' % (ind, arr, i, v) + body[m.end():]
    for m in re.finditer(r'^(\s+)([A-Za-z_][\w\.\->]*)\[([A-Za-z_]\w*)\+\+\] = ([^;\n]+);\n', body, re.M):
        ind, arr, i, v = m.group(1), m.group(2), m.group(3), m.group(4)
        if re.search(r'\b%s\b' % re.escape(i), v):
            continue
        yield '%s[%s] = ...; %s++' % (arr[:20], i, i), body[:m.start()] + '%s%s[%s] = %s;\n%s%s++;\n' % (ind, arr, i, v, ind, i) + body[m.end():]


def t_if_to_ternary(body):
    rx1 = re.compile(r'^(\s+)if \((.+)\) \{\n\s+([A-Za-z_][\w\.\->\[\]]*) = ([^;\n]+);\n\s+\}\n', re.M)
    for m in rx1.finditer(body):
        ind, cond, x, y = m.groups()
        mc = re.match(r'^(.+?) (==|!=|<=|>=|<|>) ([^&|?]+)$', cond)
        ncond = '%s %s %s' % (mc.group(1), _NEG[mc.group(2)], mc.group(3)) if mc and '(' not in mc.group(1)[:1] else '!(%s)' % cond
        new = '%s%s = %s ? %s : %s;\n' % (ind, x, ncond, x, y)
        yield 'if-assign %s -> ternary' % x[:20], body[:m.start()] + new + body[m.end():]


def t_chain_reverse(body):
    for m in re.finditer(r'^(\s*)((?:[A-Za-z_][\w\.\->\[\]]*\s*=\s*){2,})([^=;]+);', body, re.M):
        if m.group(3).strip() not in ('0', '0.0f', '0.0', 'NULL', 'false'):
            continue
        targets = [x.strip() for x in m.group(2).split('=') if x.strip()]
        if len(targets) < 2:
            continue
        new = m.group(1) + ' = '.join(reversed(targets)) + ' = ' + m.group(3).strip() + ';'
        yield 'reverse assignment chain %s' % ' = '.join(targets)[:40], body[:m.start()] + new + body[m.end():]


def t_unconst(body):
    ms = list(re.finditer(r'^([ 	]+)const ([A-Za-z_]\w*\s*\*\s*[A-Za-z_]\w*\s*[;=,])', body, re.M))
    for m in ms:
        yield 'drop const on %s' % m.group(2)[:40], body[:m.start()] + m.group(1) + m.group(2) + body[m.end():]
    if len(ms) > 1:
        yield 'drop const on all local pointers', re.sub(r'^([ 	]+)const ([A-Za-z_]\w*\s*\*\s*[A-Za-z_]\w*\s*[;=,])', lambda m: m.group(1) + m.group(2), body, flags=re.M)


def t_for_init_all(body):
    for m in re.finditer(r'^([ \t]*)for \(\s*;([^;\n]*);([^)\n]*)\)', body, re.M):
        ind = m.group(1)
        incr = m.group(3)
        stepped = set(re.findall(r'\b(\w+)\s*(?:\+\+|--|\+=|-=)', incr)) | set(re.findall(r'(?:\+\+|--)\s*(\w+)', incr))
        if not stepped:
            continue
        pre = body[:m.start()]
        lines = pre.split('\n')
        if lines and lines[-1].strip() == '':
            lines = lines[:-1]
        decls = []
        k = len(lines) - 1
        while k >= 0:
            dm = re.match(r'^%s((?:const\s+)?[A-Za-z_][\w:<>]*\s*\**)\s*(\w+)\s*=\s*([^;]+);\s*$' % re.escape(ind), lines[k])
            if not dm:
                break
            decls.insert(0, (dm.group(1).strip(), dm.group(2), dm.group(3).strip()))
            k -= 1
        moved = [d for d in decls if d[1] in stepped]
        if not moved:
            continue
        keep = lines[:k + 1]
        newdecls = [('%s%s %s;' % (ind, ty, v)) if (ty, v, e) in moved else ('%s%s %s = %s;' % (ind, ty, v, e))
                    for ty, v, e in decls]
        orders = [moved]
        counters = [d for d in moved if re.match(r'^-?\d+[uUlL]*$', d[2])]
        others = [d for d in moved if d not in counters]
        if counters and others:
            orders.append(counters + others)
        for o in orders:
            init = ', '.join('%s = %s' % (v, e) for _, v, e in o)
            new = '\n'.join(keep + newdecls) + '\n' + '%sfor (%s;%s;%s)' % (ind, init, m.group(2), incr)
            yield 'for-header init of %s' % ','.join(v for _, v, _ in o), new + body[m.end():]


def t_compound(body):
    for m in re.finditer(r'^(\s+)([A-Za-z_][\w.\->\[\]]*) = \2 (\+|-|\||&|\^|\*|<<|>>) ([\w.\->\[\]()]+);', body, re.M):
        ind, lv, op, rhs = m.groups()
        yield 'compound %s %s=' % (lv, op), body[:m.start()] + '%s%s %s= %s;' % (ind, lv, op, rhs) + body[m.end():]
    for m in re.finditer(r'^(\s+)([A-Za-z_][\w.\->\[\]]*) (\+|-|\||&|\^|\*|<<|>>)= ([\w.\->\[\]()]+);', body, re.M):
        ind, lv, op, rhs = m.groups()
        yield 'expand %s %s=' % (lv, op), body[:m.start()] + '%s%s = %s %s %s;' % (ind, lv, lv, op, rhs) + body[m.end():]


def t_bool_invert(body):
    for m in re.finditer(r'^(\s*)if \((.+)\) \{\n\s*return (true|false|1|0);\n\1\}\n\1return (true|false|1|0);', body, re.M):
        ind, c, a, b = m.groups()
        yield 'invert bool return if (%s)' % c[:40], body[:m.start()] + \
            '%sif (!(%s)) {\n%s    return %s;\n%s}\n%sreturn %s;' % (ind, c, ind, b, ind, ind, a) + body[m.end():]


def t_split_compound(body):
    for m in re.finditer(r'^([ \t]+)((?:f32|f64|float|double|s32|u32|int|s16|u16|s8|u8|short|long|[A-Za-z_]\w*\s*\*+) ?)?(\w+) = ([^;\n]+);$', body, re.M):
        ind, ty, v, rhs = m.group(1), m.group(2) or '', m.group(3), m.group(4)
        depth, splits, mul = 0, [], []
        for i, c in enumerate(rhs):
            if c in '([':
                depth += 1
            elif c in ')]':
                depth -= 1
            elif depth == 0 and c in '+-' and rhs[i - 1:i] == ' ' and rhs[i + 1:i + 2] == ' ':
                splits.append(i)
            elif depth == 0 and c == '*' and rhs[i - 1:i] == ' ' and rhs[i + 1:i + 2] == ' ':
                mul.append(i)
        if not splits:
            splits = mul
        if len(splits) != 1:
            continue
        i = splits[0]
        a, op, b = rhs[:i].strip(), rhs[i], rhs[i + 1:].strip()
        if re.search(r'(?<![\w.>])%s(?!\w)' % re.escape(v), a + ' ' + b):
            continue
        orders = ((a, b),) if op == '-' else ((b, a), (a, b))
        for first, second in orders:
            new = '%s%s%s = %s;\n%s%s %s= %s;' % (ind, ty, v, first, ind, v, op, second)
            yield 'split %s = %s into %s= (first %s)' % (v, rhs[:30], op, first[:20]), body[:m.start()] + new + body[m.end():]
            if second == '1' and op in '+-':
                new = '%s%s%s = %s;\n%s%s%s;' % (ind, ty, v, first, ind, v, op * 2)
                yield 'split %s = %s into %s%s' % (v, rhs[:30], v, op * 2), body[:m.start()] + new + body[m.end():]


def t_sink_init(body):
    lines = body.split('\n')
    for i, l in enumerate(lines):
        m = re.match(r'^([ \t]+)((?:const )?[A-Za-z_][\w:<>]*\**) (\w+) = (-?(?:0x[0-9A-Fa-f]+|\d+(?:\.\d*)?f?|true|false|0));\s*$', l)
        if not m:
            continue
        ind, ty, v, val = m.groups()
        pat = re.compile(r'(?<![\w.>])%s(?!\w)' % re.escape(v))
        stmt_start = None
        for j in range(i + 1, min(len(lines), i + 60)):
            lj = lines[j]
            if not lj.strip():
                continue
            lind = len(re.match(r'^([ \t]*)', lj).group(1))
            if lind < len(ind):
                break
            if lind == len(ind) and not lj.strip().startswith('}'):
                stmt_start = j
            if pat.search(lj):
                k = stmt_start if stmt_start is not None else j
                between = lines[i + 1:k]
                if any(re.search(r'\w\s*\(', b) and not re.match(r'^\s*(if|while|for|switch)\b', b) for b in between):
                    new = lines[:i] + ['%s%s %s;' % (ind, ty, v)] + lines[i + 1:k] + ['%s%s = %s;' % (ind, v, val)] + lines[k:]
                    yield 'sink init of %s past a call' % v, '\n'.join(new)
                break


def t_reverse_if_else(body):
    pattern = r'^([ \t]+)if \(([^\n;{}]+)\) \{\n([^{}]*?)^\1\} else \{\n([^{}]*?)^\1\}'
    for m in re.finditer(pattern, body, re.M):
        ind, cond, yes, no = m.groups()
        if cond.count('(') != cond.count(')') or re.search(r'[\"\']|//|/\*|\bgoto\b|^\s*#', m.group(), re.M):
            continue
        replacement = '%sif (!(%s)) {\n%s%s} else {\n%s%s}' % (ind, cond, no, ind, yes, ind)
        yield 'reverse if/else physical order', body[:m.start()] + replacement + body[m.end():]


def t_array_offset_group(body):
    pattern = r'&([A-Za-z_]\w*)\[\s*(0[xX][0-9a-fA-F]+|[1-9][0-9]*)\s*\+\s*([^\[\]\n;]+)\]'
    for m in re.finditer(pattern, body):
        array, offset, index = m.groups()
        if any(c in index for c in '=,{}') or index.count('(') != index.count(')'):
            continue
        replacement = '(%s + %s + (%s))' % (array, offset, index.strip())
        yield 'separate table offset from index', body[:m.start()] + replacement + body[m.end():]


def t_scoped_for_counter(body):
    pattern = r'for\s*\(\s*([A-Za-z_]\w*)\s*=\s*0\s*;[^;\n]+;[^{}\n]+\)\s*\{'
    for m in re.finditer(pattern, body):
        name = m.group(1)
        declarations = list(re.finditer(r'^([ \t]+)(int|s32|u32|u16|u8)\s+' + re.escape(name) + r'\s*;[ \t]*$', body[:m.start()], re.M))
        if not declarations:
            continue
        declaration = declarations[-1]
        start = m.end() - 1
        depth, end = 1, start + 1
        while depth and end < len(body):
            depth += (body[end] == '{') - (body[end] == '}')
            end += 1
        if depth or re.search(r'\b' + re.escape(name) + r'\b', body[end:]):
            continue
        segment = body[m.start():end]
        if re.search(r'\bgoto\b|[\"\']|//|/\*', segment):
            continue
        fresh = name + '_loop'
        if re.search(r'\b' + re.escape(fresh) + r'\b', body):
            continue
        segment = re.sub(r'\b' + re.escape(name) + r'\b', fresh, segment)
        segment = re.sub(r'(for\s*\(\s*)' + re.escape(fresh), r'\g<1>' + declaration.group(2) + ' ' + fresh, segment, count=1)
        yield 'fresh scoped counter ' + name, body[:m.start()] + segment + body[end:]


def t_ternary_to_if(body):
    rx = re.compile(r'^([ \t]+)((?:(?:const )?[A-Za-z_][\w:<>]*\s*\**\s+)?)([A-Za-z_][\w.\->\[\]]*) = ([^;?\n]+?) \? ([^:;\n]+?) : ([^;\n]+);$', re.M)
    for m in rx.finditer(body):
        ind, ty, x, c, a, b = m.groups()
        if ty.strip() == 'return' or not all(_balanced(e) for e in (c, a, b)):
            continue
        c = c.strip()
        if c.startswith('(') and c.endswith(')') and _balanced(c[1:-1]):
            c = c[1:-1]
        decl = '%s%s%s;\n' % (ind, ty, x) if ty.strip() else ''
        new = '%s%sif (%s) {\n%s    %s = %s;\n%s} else {\n%s    %s = %s;\n%s}' % (
            decl, ind, c, ind, x, a.strip(), ind, ind, x, b.strip(), ind)
        yield 'ternary %s -> if/else' % x[:20], body[:m.start()] + new + body[m.end():]


def t_early_return(body):
    if re.search(r'\breturn [^;]', body):
        return
    for m in re.finditer(r'^([ \t]+)if \((.+)\) \{$', body, re.M):
        ind, c = m.groups()
        if not _balanced(c):
            continue
        cl = _close(body, m.end() - 1)
        if cl is None:
            continue
        rest = body[cl + 1:]
        if re.match(r'^\s*else\b', rest) or not re.match(r'^\s*(return;\s*)?$', rest):
            continue
        inner = _reindent(body[m.end():cl].rstrip(' \t'), -4)
        new = '%sif (%s) {\n%s    return;\n%s}%s' % (ind, _neg_cond(c), ind, ind, inner.rstrip('\n'))
        yield 'wrapping if -> early return', body[:m.start()] + new + '\n' + body[cl + 1:].lstrip(' \t').lstrip('\n')
    for m in re.finditer(r'^([ \t]+)if \((.+)\) \{?\n\s*return;\n(?:\1\}\n)?', body, re.M):
        ind, c = m.groups()
        rest = body[m.end():]
        if not _balanced(c) or not rest.strip() or re.search(r'^\s*\w+:', rest, re.M) or rest.count('{') != rest.count('}'):
            continue
        if re.match(r'^\s*\}', rest):
            continue
        yield 'early return -> wrapping if', body[:m.start()] + '%sif (%s) {\n%s%s}\n' % (
            ind, _neg_cond(c), _reindent(rest.rstrip(), 4) + '\n', ind)


def t_nest_and(body):
    for m in re.finditer(r'^([ \t]+)if \((.+)\) \{$', body, re.M):
        ind, c = m.groups()
        parts = _top_level(c, ['&&'])
        if len(parts) < 2 or not _balanced(c):
            continue
        cl = _close(body, m.end() - 1)
        if cl is None or re.match(r'^\s*else\b', body[cl + 1:]):
            continue
        a, b = parts[0].strip(), '&&'.join(parts[1:]).strip()
        new = '%sif (%s) {\n%s    if (%s) {%s    }\n%s}' % (ind, a, ind, b, _reindent(body[m.end():cl].rstrip(' \t'), 4) + ind, ind)
        yield 'split && into nested ifs', body[:m.start()] + new + body[cl + 1:]
    for m in re.finditer(r'^([ \t]+)if \((.+)\) \{\n([ \t]+)if \((.+)\) \{$', body, re.M):
        ind, a, ind2, b = m.groups()
        cl_in = _close(body, m.end() - 1)
        cl_out = _close(body, body.index('{', m.start()))
        if None in (cl_in, cl_out) or not _balanced(a) or not _balanced(b):
            continue
        if body[cl_in + 1:cl_out].strip() or re.match(r'^\s*else\b', body[cl_out + 1:]) or re.match(r'^\s*else\b', body[cl_in + 1:]):
            continue
        wrap = lambda e: e if re.match(r'^[\w.\->\[\]!]+$|^\w+\(.*\)$', e) and '||' not in e else '(%s)' % e
        yield 'merge nested ifs into &&', body[:m.start()] + '%sif (%s && %s) {%s}' % (
            ind, wrap(a.strip()), wrap(b.strip()), _reindent(body[m.end():cl_in].rstrip(' \t'), -4) + ind) + body[cl_out + 1:]


def t_compound_expr(body):
    for m in re.finditer(r'^(\s+)([A-Za-z_][\w.\->\[\]]*) = \2 (\+|-|\||&|\^) ([^;\n]+);', body, re.M):
        ind, lv, op, rhs = m.groups()
        if re.fullmatch(r'[\w.\->\[\]()]+', rhs.strip()) or _split_low(rhs) or '?' in rhs:
            continue
        yield 'compound expr %s %s=' % (lv[:20], op), body[:m.start()] + '%s%s %s= %s;' % (ind, lv, op, rhs) + body[m.end():]


def t_chain_zero(body):
    lines = body.split('\n')
    i = 0
    while i < len(lines):
        m = re.match(r'^(\s+)([A-Za-z_][\w.\->\[\]]*) = (-?\w+);\s*$', lines[i])
        if not m:
            i += 1
            continue
        ind, k = m.group(1), m.group(3)
        if k not in ('0', '0.0f', '0.0', 'NULL', 'false'):
            i += 1
            continue
        j = i + 1
        lvs = [m.group(2)]
        while j < len(lines):
            m2 = re.match(r'^' + re.escape(ind) + r'([A-Za-z_][\w.\->\[\]]*) = ' + re.escape(k) + r';\s*$', lines[j])
            if not m2:
                break
            lvs.append(m2.group(1))
            j += 1
        if len(lvs) >= 2:
            for order, tag in ((lvs[::-1], 'rev'), (lvs, 'fwd')):
                nl = lines[:i] + [ind + ' = '.join(order) + ' = ' + k + ';'] + lines[j:]
                yield 'chain %d stores of %s (%s)' % (len(lvs), k, tag), '\n'.join(nl)
        i = j


def t_small_const_cast(body):
    for m in re.finditer(r'([A-Za-z_][\w.\->\[\]]*) (\||&|\^)= (0x[0-9A-Fa-f]+|\d+);', body):
        for ty, lim in (('u8', 0xFF), ('u16', 0xFFFF)):
            if int(m.group(3), 0) > lim:
                continue
            yield '%s %s= (%s)%s' % (m.group(1)[:20], m.group(2), ty, m.group(3)), \
                body[:m.start()] + '%s %s= (%s)%s;' % (m.group(1), m.group(2), ty, m.group(3)) + body[m.end():]


def t_index_copy(body):
    idx_vars = Counter(re.findall(r'\[([A-Za-z_]\w*)\]', body))
    for v, n in idx_vars.items():
        if n < 3 or not re.search(r'\b(?:int|s32|u32|u16|u8|s16|s8)\s+' + re.escape(v) + r'\b', body):
            continue
        lines = body.split('\n')
        uses = [i for i, l in enumerate(lines) if '[%s]' % v in l]
        for k in uses[1:]:
            ind = re.match(r'\s*', lines[k]).group(0)
            if re.match(r'\s*(case|else|\}|for\b|while\b|if\b)', lines[k]):
                continue
            for ty in ('u32', 'int'):
                nl = lines[:k] + [ind + '%s %s_c = %s;' % (ty, v, v)] + [l.replace('[%s]' % v, '[%s_c]' % v) for l in lines[k:]]
                yield 'index copy %s %s from line %d' % (ty, v, k), '\n'.join(nl)


def t_walkptr_index(body):
    for f in re.finditer(r'for \(([^;]*);([^;]*);([^)]*)\)\s*\{', body):
        init, cond, inc = f.groups()
        incs = [x.strip() for x in inc.split(',')]
        ivars = [x[:-2] for x in incs if re.fullmatch(r'\w+\+\+', x)]
        if len(ivars) < 2:
            continue
        idx = ivars[0]
        end = _block_end(body, f.end() - 1)
        if end < 0:
            continue
        loop = body[f.end():end]
        for p in ivars[1:]:
            mi = re.search(r'(?:^|,)\s*%s = ([A-Za-z_][\w.\->]*)\s*(?=,|$)' % re.escape(p), init)
            pre = None
            if mi:
                arr = mi.group(1)
            else:
                pre = re.search(r'^[ \t]*%s = ([A-Za-z_][\w.\->]*);\n(?=(?:[^\n]*\n){0,3}?[ \t]*for \()' % re.escape(p),
                                body[:f.end()], re.M)
                if not pre:
                    continue
                arr = pre.group(1)
            if not re.search(r'(?:^|,)\s*%s = 0\s*(?=,|$)' % re.escape(idx), init):
                continue
            if re.search(r'(\+\+|--)\s*%s\b|\b%s\s*(\+\+|--|[-+*/|&^]?=(?!=))' % (re.escape(p), re.escape(p)), loop):
                continue
            uses = re.findall(r'(?<![\w.>])%s(?!\w)' % re.escape(p), loop)
            forms = re.findall(r'\*%s(?!\w)|(?<![\w.>])%s\[' % (re.escape(p), re.escape(p)), loop)
            if len(uses) != len(forms) or re.search(r'(?<![\w.>])%s(?!\w)' % re.escape(p), body[end:]):
                continue
            nl = re.sub(r'\*%s(?!\w)' % re.escape(p), '%s[%s]' % (arr, idx), loop)
            nl = re.sub(r'(?<![\w.>])%s\[\s*([^\]]+?)\s*\]' % re.escape(p), lambda m: '%s[%s + %s]' % (arr, idx, m.group(1)), nl)
            ninit = re.sub(r',?\s*%s = [A-Za-z_][\w.\->]*' % re.escape(p), '', init) if mi else init
            ninc = ', '.join(x for x in incs if x != p + '++')
            head = 'for (%s;%s; %s) {' % (ninit.strip().rstrip(','), cond, ninc)
            before = body[:f.start()]
            if pre:
                before = before[:pre.start()] + before[pre.end():]
            nb = before + head + nl + body[end:]
            nb = re.sub(r'^[ \t]*[\w:]+\s*\*+\s*%s;\n' % re.escape(p), '', nb, count=1, flags=re.M)
            yield 'walking pointer %s -> %s[%s]' % (p, arr, idx), nb


def t_abs_ternary(body):
    for m in re.finditer(r'if \((\w+) (<=|<) 0\) \{?\s*\1 = -\1;\s*\}?', body):
        v, op = m.group(1), m.group(2)
        keep = '>' if op == '<=' else '>='
        yield 'abs %s as ternary' % v, body[:m.start()] + '%s = (%s %s 0) ? %s : -%s;' % (v, v, keep, v, v) + body[m.end():]
    for m in re.finditer(r'(\w+) = \(?(\w+) (>|>=) 0\)? \? \2 : -\2;', body):
        v, x, op = m.group(1), m.group(2), m.group(3)
        if v != x:
            continue
        neg = '<=' if op == '>' else '<'
        yield 'abs %s as if' % v, body[:m.start()] + 'if (%s %s 0) {\n        %s = -%s;\n    }' % (v, neg, v, v) + body[m.end():]


def t_reread_local(body):
    for m in _LOAD_LOCAL.finditer(body):
        ind, v, expr = m.group(1), m.group(2), m.group(3).strip()
        calls = [c for c in re.findall(r'\b([A-Za-z_]\w*)\s*\(', expr)
                 if not re.fullmatch(r'[A-Z][A-Z0-9_]*', c) and c != 'sizeof' and not re.fullmatch(r'[su]\d+|f32|f64', c)]
        if calls:
            continue
        if '++' in expr or '--' in expr:
            continue
        rest = body[m.end():]
        end = rest.find('\n' + ind[:-4] + '}') if len(ind) >= 4 else -1
        scope = rest if end < 0 else rest[:end]
        uses = len(re.findall(r'\b%s\b' % re.escape(v), scope))
        if uses == 0 or uses > 4 or re.search(r'\b%s\s*(=[^=]|\+=|-=|\+\+|--|\*=|/=|&=|\|=)' % re.escape(v), scope) or \
                re.search(r'&\s*%s\b' % re.escape(v), scope):
            continue
        last = max(x.start() for x in re.finditer(r'\b%s\b' % re.escape(v), scope))
        if _has_store_or_call(scope[:last]):
            continue
        new_scope = re.sub(r'\b%s\b' % re.escape(v), lambda _: expr, scope)
        yield 'reread %s = %s' % (v, expr[:30]), body[:m.start()] + body[m.end() + 1:m.end() + 1] + \
            new_scope + (rest[end:] if end >= 0 else '')


def t_counter_reuse(body):
    decls = [(m.group(1), m.group(2)) for m in re.finditer(r'^[ \t]+(' + _T + r')\s+(\w+)\s*;', body, re.M)]
    for m in re.finditer(r'for\s*\(\s*(' + _T + r')\s+(\w+)\s*=\s*0\s*;', body):
        typ, k = m.group(1), m.group(2)
        start = body.find('{', m.end())
        if start < 0:
            continue
        depth, end = 1, start + 1
        while depth and end < len(body):
            depth += (body[end] == '{') - (body[end] == '}')
            end += 1
        seg = body[m.start():end]
        if re.search(r'\bgoto\b|[\"\']', seg):
            continue
        for t2, v in decls:
            if v == k or (t2 != typ and not ({t2, typ} <= {'int', 's32'})):
                continue
            if re.search(r'\b' + re.escape(v) + r'\b', seg):
                continue
            if not re.search(r'\b' + re.escape(v) + r'\b', body[:m.start()]):
                continue
            if re.search(r'\b' + re.escape(v) + r'\b', body[end:]):
                continue
            new = re.sub(r'\b' + re.escape(k) + r'\b', v, seg)
            new = re.sub(r'for\s*\(\s*' + _T + r'\s+' + re.escape(v), 'for (' + v, new, count=1)
            yield 'loop counter %s reuses %s' % (k, v), body[:m.start()] + new + body[end:]


def t_mask_to_ternary(body):
    pat = r'\b(0x[0-9A-Fa-f]+|\d+)\s*&\s*-\(([^()]*(?:\([^()]*\))?[^()]*&\s*1)\)'
    for m in re.finditer(pat, body):
        yield 'K & -(x & 1) -> (x & 1) ? K : 0', body[:m.start()] + '((%s) ? %s : 0)' % (m.group(2), m.group(1)) + body[m.end():]
    pat2 = r'-\(([^()]*(?:\([^()]*\))?[^()]*&\s*1)\)\s*&\s*(0x[0-9A-Fa-f]+|\d+)\b'
    for m in re.finditer(pat2, body):
        yield '-(x & 1) & K -> (x & 1) ? K : 0', body[:m.start()] + '((%s) ? %s : 0)' % (m.group(1), m.group(2)) + body[m.end():]


def t_split_const_add(body):
    pat = r'^([ \t]+)(\w+)\s*=\s*(\w+(?:->\w+|\.\w+)?)\s*\+\s*(0x[0-9A-Fa-f]+|\d+)\s*\+\s*(\w+(?:->\w+|\.\w+)?)\s*;'
    for m in re.finditer(pat, body, re.M):
        ind, lhs, a, k, g = m.groups()
        new = '%s%s = %s + %s;\n%s%s += %s;' % (ind, lhs, a, k, ind, lhs, g)
        yield 'split %s = a + K + g' % lhs, body[:m.start()] + new + body[m.end():]


def t_neg_call_temp(body):
    for m in re.finditer(r'^([ \t]+)(\w+)\s*=\s*-\s*(\w+\([^;\n]*\))\s*;', body, re.M):
        ind, lhs, call = m.groups()
        t = _decl_type(body[:m.start()], lhs)
        if not t:
            continue
        new = '%s%s t_neg = %s;\n%s%s = -t_neg;' % (ind, t, call, ind, lhs)
        if re.search(r'\bt_neg\b', body):
            continue
        yield 'neg via temp: %s' % lhs, body[:m.start()] + new + body[m.end():]


def t_preinc_in_test(body):
    pat = (r'^([ \t]+)' + _INT + r'\s+(\w+)\s*=\s*([\w.>\-\[\]]+)\s*([+-])\s*1\s*;[ \t]*\n'
           r'[ \t]+\3\s*=\s*\2\s*;[ \t]*\n([ \t]+)if\s*\((.*)\b\2\b(.*)\)\s*\{')
    for m in re.finditer(pat, body, re.M):
        ind, t, x, op, ind2, pre, post = m.groups()
        rest = body[m.end():]
        redecl = re.search(_INT + r'\s+' + re.escape(t) + r'\b', rest)
        if re.search(r'\b' + re.escape(t) + r'\b', rest[:redecl.start()] if redecl else rest):
            continue
        incr = ('++' if op == '+' else '--') + x
        yield 'pre%s in test: %s' % ('inc' if op == '+' else 'dec', x), \
            body[:m.start()] + '%sif (%s%s%s) {' % (ind2, pre, incr, post) + body[m.end():]


def t_for_init_order_swap(body):
    for m in re.finditer(r'for\s*\(\s*(\w+\s*=\s*[^,;()]+),\s*(\w+\s*=\s*[^,;()]+);', body):
        a, b = m.group(1).strip(), m.group(2).strip()
        yield 'for init swapped: %s, %s' % (b, a), body[:m.start()] + 'for (%s, %s;' % (b, a) + body[m.end():]


def t_unroll_small_fill(body):
    pat = (r'^([ \t]+)for\s*\(\s*(\w+)\s*=\s*0\s*;\s*\2\s*<\s*([1-4])\s*;\s*\2\+\+\s*\)\s*\{\s*\n'
           r'[ \t]+(\w+)\[\2\]\s*=\s*([^;\n]+);\s*\n[ \t]*\}[ \t]*\n')
    for m in re.finditer(pat, body, re.M):
        ind, i, n, arr, val = m.groups()
        n = int(n)
        rest = body[m.end():]
        fix = list(re.finditer(r'^[ \t]+' + re.escape(arr) + r'\[(\d+)\][^;\n]*;[ \t]*\n', rest, re.M))
        fixes, pos = [], 0
        for f in fix:
            if f.start() != pos:
                break
            fixes.append(f)
            pos = f.end()
        out = ''
        for k in range(n):
            out += '%s%s[%d] = %s;\n' % (ind, arr, k, val)
            out += ''.join(f.group(0) for f in fixes if int(f.group(1)) == k)
        yield 'unrolled %s fill' % arr, body[:m.start()] + out + rest[pos:]
        if fixes:
            yield 'unrolled %s fill (fix-ups after)' % arr, body[:m.start()] + ''.join(
                '%s%s[%d] = %s;\n' % (ind, arr, k, val) for k in range(n)) + rest


def t_separate_loop_counter(body):
    for m in re.finditer(r'for\s*\(\s*;\s*(\w+)\s*(<=?|>=?|!=)\s*([^;]+);\s*\1\s*(\+\+|--)([^)]*)\)\s*\{', body):
        v = m.group(1)
        start = m.end() - 1
        depth, end = 1, start + 1
        while depth and end < len(body):
            depth += (body[end] == '{') - (body[end] == '}')
            end += 1
        if re.search(r'\b' + re.escape(v) + r'\b', body[end:]):
            continue
        if 'i_lc' in body:
            continue
        loop = body[m.start():end]
        new_loop = re.sub(r'\b' + re.escape(v) + r'\b', 'i_lc', loop)
        new_loop = new_loop.replace('for (;', 'for (i_lc = %s;' % v, 1).replace('for(;', 'for (i_lc = %s;' % v, 1)
        if 'i_lc = ' not in new_loop:
            new_loop = re.sub(r'for\s*\(\s*;', 'for (i_lc = %s;' % v, new_loop, count=1)
        brace = body.find('{') if re.match(r'^[^\n{;]*\(', body) else -1
        yield 'separate loop counter for %s' % v, body[:brace + 1] + '\n    int i_lc;' + body[brace + 1:m.start()] + new_loop + body[end:]


def t_continue_else(body):
    for m in re.finditer(r'^([ \t]+)if\s*\(((?:[^()\n]|\((?:[^()\n]|\([^()\n]*\))*\))*)\)\s*\{[ \t]*\n', body, re.M):
        ind, cond = m.group(1), m.group(2)
        end = _block_end(body, m.end() - 2)
        after = body[end:]
        if re.match(r'\s*else\b', after) or not re.match(r'\s*\}', after):
            continue
        close = end + re.match(r'\s*', after).end()
        depth, i = 1, close - 1
        while i >= 0 and depth:
            depth += (body[i] == '}') - (body[i] == '{')
            i -= 1
        head = body[:i + 1].rstrip()
        line = head[head.rfind('\n') + 1:]
        if not re.match(r'\s*(for|while)\s*\(', line) and not line.strip().startswith('do'):
            continue
        inner = body[m.end():end - 1].rstrip(' 	')
        new = '%sif (!(%s)) {\n%s    continue;\n%s} else {\n%s%s}' % (ind, cond, ind, ind, inner, ind)
        yield 'continue+else for if (%s)' % cond[:30], body[:m.start()] + new + body[end:]


def t_commute_paren(body):
    pat = r'(?:(?<=[=(,])|(?<=return ))\s*(%s|%s)\s*([+*|&^])\s*(%s|%s)\s*(?=[;),])' % (_ATOM, _PAREN, _ATOM, _PAREN)
    for m in re.finditer(pat, body):
        a, op, b = m.group(1), m.group(2), m.group(3)
        if not (a.startswith('(') or b.startswith('(')) or a == b:
            continue
        lead = body[m.start():m.start(1)]
        yield 'commute (paren) %s %s %s' % (a[:20], op, b[:20]), body[:m.start()] + lead + '%s %s %s' % (b, op, a) + body[m.end(3):]


def t_goto_loop_to_while(body):
    for m in re.finditer(r'^([ \t]*)(\w+):[ \t]*\n([ \t]*)if\s*\(((?:[^()\n]|\((?:[^()\n]|\([^()\n]*\))*\))*)\)\s*\{[ \t]*\n',
                         body, re.M):
        label = m.group(2)
        if len(re.findall(r'\b%s\b' % re.escape(label), body)) != 2:
            continue
        end = _block_end(body, m.end() - 2)
        inner = body[m.end():end - 1]
        g = re.search(r'^[ \t]*goto\s+%s\s*;[ \t]*\n\s*$' % re.escape(label), inner, re.M)
        if not g:
            continue
        new = '%swhile (%s) {\n%s' % (m.group(3), m.group(4), inner[:g.start()]) + inner[g.end():]
        yield 'goto loop %s -> while' % label, body[:m.start()] + new + m.group(3) + '}' + body[end:]


# ----------------------------------------------------------------------------------------------------------------
# registry + driver
class Trick(NamedTuple):
    name: str
    description: str
    fn: object


_DESC = [
    ('t_decl_swaps', 'swap two local declarations of the same declaration run (colouring order of locals)'),
    ('t_decl_move', 'move one local declaration to the top / bottom of its declaration run'),
    ('t_decl_hoist', 'hoist a block-scoped `T x = e;` to the function top (`T x;` there, `x = e;` stays)'),
    ('t_types', 'respell a local integer type with an equivalent 32-bit spelling (int / s32 / long, u32 / unsigned int)'),
    ('t_bool_forms', '`x != 0` <-> `(bool)x`, `!x` <-> `x == 0`, `!(x & K)` <-> `(x & K) == 0`'),
    ('t_cmp_swap', 'swap the operands of ==/!= between plain names and constants'),
    ('t_commute', 'swap the operands of a commutative operator or compare (not when both sides are calls)'),
    ('t_inline_cmp', '`x = f(a); if (x op y)` -> `if (y op f(a))` when x is used nowhere else'),
    ('t_call_local_left', 'hold a compared call result in a typed local on the left of the compare'),
    ('t_sink_invariant', 'move a trailing `op constant` from an assignment into the only use of the variable'),
    ('t_stmt_swap', 'swap two adjacent independent simple statements'),
    ('t_stmt_move', 'move one simple statement 2..6 lines up/down across independent statements'),
    ('t_hoist_call_arg', 'hold a nested call argument in a typed local before the call'),
    ('t_inline_arg', 'write a once-used temp straight into the next call as `(T)(expr)` and drop the temp'),
    ('t_split_chain', '`v = v * a * b;` -> `v *= a; v *= b;` (same operator family)'),
    ('t_for_init_merge', 'merge a preceding `x = V;` into the for initialiser, both orders'),
    ('t_chained_init', '`x = 0; for (i = 0;...)` <-> `for (i = x = 0;...)`'),
    ('t_return_ifelse', '`return x == 0;` -> `if (x) return false; else return true;`'),
    ('t_ptr_to_index', 'pointer walked together with an index -> `T* p = &arr[i];` inside the loop'),
    ('t_walkptr_index', 'walking pointer in a for header -> `arr[i]` (index must start at 0)'),
    ('t_if_to_switch', '`if (x == K) {..}` -> one-case switch (no break/continue inside)'),
    ('t_return_via_local', '`return f(x);` -> `v = f(x); return v;` through an existing local'),
    ('t_postinc_index', '`a[i] = v; i++;` <-> `a[i++] = v;`'),
    ('t_if_to_ternary', '`if (C) { x = Y; }` -> `x = !C ? x : Y;`'),
    ('t_ternary_to_if', '`x = C ? a : b;` -> if/else assignment'),
    ('t_chain_reverse', 'reverse a chained assignment of zero `a = b = c = 0;`'),
    ('t_chain_zero', '`a = 0; b = 0;` -> `b = a = 0;` (both orders)'),
    ('t_unconst', 'drop `const` on local pointer variables (one at a time, then all)'),
    ('t_for_init_all', 'move zero-initialised stepped variables into the for header'),
    ('t_compound', '`x = x + k;` <-> `x += k;` (simple right-hand sides)'),
    ('t_compound_expr', '`x = x + (expr);` -> `x += expr;` (right side without lower-precedence operators)'),
    ('t_bool_invert', '`if (C) return true; return false;` <-> `if (!(C)) return false; return true;`'),
    ('t_split_compound', '`v = a + b;` -> `v = b; v += a;` (and `v = a; v += b;`); `v = a - 1;` -> `v = a; v--;`'),
    ('t_sink_init', 'split `T x = K;` and assign it just before its first use when a call lies in between'),
    ('t_reverse_if_else', 'swap the physical order of if/else arms (condition negated)'),
    ('t_array_offset_group', '`&arr[K + i]` -> `(arr + K + (i))` (keep the constant offset separate)'),
    ('t_scoped_for_counter', 'give a loop a fresh loop-scoped counter when the old one is dead after the loop'),
    ('t_early_return', 'void functions: wrapping `if` <-> early `return;`'),
    ('t_nest_and', '`if (A && B)` <-> nested ifs (no else arms)'),
    ('t_small_const_cast', '`x |= K;` -> `x |= (u8)K;` / `(u16)K` when K fits'),
    ('t_index_copy', 'copy a repeatedly used index variable into a fresh local from a later use on'),
    ('t_abs_ternary', '`if (x <= 0) x = -x;` <-> `x = x > 0 ? x : -x;`'),
    ('t_reread_local', 'replace a local that only caches a memory load by the load itself (no store/call in between)'),
    ('t_counter_reuse', 'reuse an earlier, dead-after-the-loop variable as a for-loop counter'),
    ('t_mask_to_ternary', '`K & -(x & 1)` -> `((x & 1) ? K : 0)`'),
    ('t_split_const_add', '`w = a + K + g;` -> `w = a + K; w += g;`'),
    ('t_neg_call_temp', '`x = -f(..);` -> `T t = f(..); x = -t;`'),
    ('t_preinc_in_test', '`t = x + 1; x = t; if (t < N)` -> `if (++x < N)`'),
    ('t_for_init_order_swap', 'swap the two initialisers of `for (a = 0, b = 1; ...)`'),
    ('t_unroll_small_fill', 'unroll a fill loop of 1-4 constant-bound elements'),
    ('t_separate_loop_counter', 'run a step-only loop on its own counter `for (i_lc = v; ...)`'),
    ('t_continue_else', 'last `if (C) {..}` in a loop body -> `if (!C) continue; else {..}`'),
    ('t_commute_paren', 'swap operands of a commutative op when one is parenthesised'),
    ('t_goto_loop_to_while', '`L: if (C) { ..; goto L; }` -> `while (C) { .. }`'),
]
TRICKS = [Trick(n, d, globals()[n]) for n, d in _DESC]
BY_NAME = {t.name: t for t in TRICKS}


# --- project plug-ins: your game's own tricks (see guide/11-customizing-for-your-game.md)
def load_plugins(paths):
    """Load trick plug-in files and add their tricks. A plug-in is a Python file that either defines
    TRICKS = [(name, description, fn), ...] or simply functions named t_<something>(body) (the docstring's first line
    is the description). Returns the names added. Plug-ins may import this module (`import tricks`) for helpers."""
    import importlib.util, os as _os
    added = []
    for p in paths:
        if not p:
            continue
        name = 'trickplugin_' + re.sub(r'\W', '_', _os.path.splitext(_os.path.basename(p))[0])
        spec = importlib.util.spec_from_file_location(name, p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        items = list(getattr(mod, 'TRICKS', []))
        if not items:
            for k, v in vars(mod).items():
                if k.startswith('t_') and callable(v) and getattr(v, '__module__', '') == name:
                    items.append((k, (v.__doc__ or k).strip().split('\n')[0], v))
        for it in items:
            t = it if isinstance(it, Trick) else Trick(*it)
            if t.name in BY_NAME:
                TRICKS[:] = [x for x in TRICKS if x.name != t.name]   # a plug-in may replace a built-in trick
            TRICKS.append(t)
            BY_NAME[t.name] = t
            added.append(t.name)
    return added


def _env_plugins():
    # SEARCH_TRICK_PLUGINS = plug-in files, separated like PATH (';' on Windows, ':' elsewhere)
    import os as _os
    v = _os.environ.get('SEARCH_TRICK_PLUGINS', '')
    return load_plugins([p for p in v.split(_os.pathsep) if p.strip()]) if v else []


_env_plugins()


# --- locating the function in a source file
def _demangle(fn):
    try:
        import subprocess
        from dtkproj import tool
        r = subprocess.run([tool('dtk'), 'demangle', fn], capture_output=True, text=True, stdin=subprocess.DEVNULL)
        if r.returncode == 0 and '(' in r.stdout:
            return r.stdout.strip()
    except Exception:
        pass
    return None


def _mangled_parts(f):
    # Metrowerks mangling: name__<class><F|C>... ; Q2 prefix = nested class. Returns (class or None, plain name)
    if f.startswith(('__ct__', '__dt__')):
        rest = f[6:]
    else:
        k = f.find('__', 1)
        rest = f[k + 2:] if k > 0 else ''
    comps, n = [], 1
    if re.match(r'Q\d', rest):
        n, rest = int(rest[1]), rest[2:]
    for _ in range(n):
        m = re.match(r'(\d+)', rest)
        if not m:
            return None, f
        ln = int(m.group(1))
        comps.append(rest[len(m.group(1)):len(m.group(1)) + ln])
        rest = rest[len(m.group(1)) + ln:]
    cls = comps[-1] if comps and rest[:1] in ('F', 'C') else None
    if f.startswith('__ct__') and cls:
        return cls, cls
    if f.startswith('__dt__') and cls:
        return cls, '~' + cls
    plain = f.split('__')[0] if '__' in f[1:] else f
    return cls, plain


def source_names(fn):
    """candidate spellings of an asm symbol in the source, most qualified first"""
    out = []
    d = _demangle(fn) if ('__' in fn[1:] or '@' in fn) else None
    if d:
        q = d[:d.index('(')]
        parts = [p for p in q.split('::') if p and not p.startswith('@unnamed@')]
        out += ['::'.join(parts[i:]) for i in range(len(parts))]
    cls, plain = _mangled_parts(fn)
    if cls and not cls.startswith('@unnamed@'):
        out.append(cls + '::' + plain)
    out.append(plain)
    seen = []
    for n in out:
        if n not in seen:
            seen.append(n)
    return seen


def find_body(text, name):
    """(header_start, body_start, body_end) of the definition of `name` in text, or None. Definitions start in
    column 0 (call sites are indented); indented ones (inside a namespace block) are tried second, never after a
    control keyword."""
    for pat in (r'^(?![ \t])[^\n;{}#]*\b', r'^[ \t]+(?!(?:if|while|for|switch|return|else)\b)[^\n;{}#=()]*\b'):
        rx = re.compile(pat + re.escape(name) + r'\s*\([^;{]*\)\s*(const\s*)?(?::[^;{]*)?\{', re.M)
        for m in rx.finditer(text):
            start = text.index('{', m.end() - 1)
            depth = 0
            for i in range(start, len(text)):
                if text[i] == '{':
                    depth += 1
                elif text[i] == '}':
                    depth -= 1
                    if depth == 0:
                        return m.start(), start + 1, i
    return None


def variants(text, name, tricks=None):
    """yield (description, new_whole_text) for every variant of function `name` (a source spelling such as
    'Class::method'). Round-robin over the tricks, so a cut-off budget still samples every trick. Identical texts
    are yielded once."""
    loc = find_body(text, name)
    if not loc:
        return
    hs, bs, be = loc
    body = text[bs:be]

    def gen(tr):
        for desc, nb in tr.fn(body):
            yield '%s [%s]' % (desc, tr.name), text[:bs] + nb + text[be:]
    its = [gen(t) for t in (tricks or TRICKS)]
    seen = {text}
    while its:
        alive = []
        for it in its:
            try:
                desc, nt = next(it)
            except StopIteration:
                continue
            except Exception:   # one broken trick must not stop the others
                continue
            alive.append(it)
            if nt not in seen:
                seen.add(nt)
                yield desc, nt
        its = alive


if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1 and sys.argv[1] in ('-h', '--help'):
        print(__doc__)
    for t in TRICKS:
        print('%-26s %s' % (t.name, t.description))
