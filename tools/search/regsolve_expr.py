"""regsolve_expr.py - EXPRESSION-level edits for regsolve.py (op family `expr`).

regsolve's other ops move/retype declarations. Many register/order near misses are fixed by rewriting expressions
inside statements instead; every one of these is behaviour-preserving:
  postinc   `... x++ ...;` -> `... x ...;` + `x++;` after it
  preinc    `... ++x ...;` / `if (++x ...)` -> `x++;` before it + `x`
  compound  `a op= b;` <-> `a = a op b;`, `x++;` <-> `x += 1;` <-> `x = x + 1;`
  reuse     after `v = E;` (E a plain field/array read, same type as v) read v instead of E on a later line
            (no call / store in between)
  defup     move a call-free `v = E;` one to three statements up
  assoc     integer sums of three terms in every order/grouping
  loadtemp  `x = E;` (E a field/array read) -> `T tmp = E; x = tmp;`
  forwhile  `for (i; c; x = E) {..}` without `continue` -> `i; while (c) { ..; T tmp = E; x = tmp; }`
`reuse~` = v is an exact float copy of a small integer field: same value only where the use is already in float
arithmetic - check that line before installing. Float sums are never reassociated (probe_type says f32/f64 -> skipped).
Used through regsolve.py (`--ops expr`, part of the default op list).
"""
import itertools, re

KW = {'if', 'for', 'while', 'switch', 'return', 'sizeof', 'do', 'else', 'case', 'new', 'delete', 'goto', 'default'}
PATH = r'[A-Za-z_]\w*(?:\s*(?:->|\.)\s*\w+|\[\s*\w+\s*\])*'
PURE_READ = re.compile(r'^[A-Za-z_]\w*(?:\s*(?:->|\.)\s*\w+|\[\s*\w+\s*\])+$')
CALL = re.compile(r'\b([A-Za-z_]\w*)\s*\(')
ATOM = r'(?:' + PATH + r'|0x[0-9A-Fa-f]+|\d+)'
FLOATY = ('f32', 'f64', 'float', 'double')


def has_call(s):
    return any(m.group(1) not in KW for m in CALL.finditer(s))


def indent(l):
    return l[:len(l) - len(l.lstrip())]


def is_stmt(l):
    """a plain expression statement (no control keyword, label, declaration test is not needed)"""
    t = l.strip()
    if not t.endswith(';') or t.startswith(('//', '/*', '#')):
        return False
    first = re.match(r'[A-Za-z_]\w*', t)
    if first and first.group(0) in KW:
        return False
    return t.count(';') == 1


def assigns(l, name):
    """does line l store to name (=, op=, ++, --) ?"""
    n = re.escape(name)
    return bool(re.search(r'(?<![\w>.])' + n + r'\s*(?:[-+*/%|&^]|<<|>>)?=(?!=)', l) or
                re.search(r'(?:\+\+|--)\s*' + n + r'\b|(?<![\w>.])' + n + r'\s*(?:\+\+|--)', l))


def word_count(l, name):
    return len(re.findall(r'(?<![\w>.])' + re.escape(name) + r'(?!\w)', l))


class Ctx:
    def __init__(self, locate, decls, probe_type, is_c):
        self.locate, self.decls, self.probe_type, self.is_c = locate, decls, probe_type, is_c


def line_op(ctx, line_txt, occ, fn):
    """op that finds the occ-th body line equal to line_txt and lets fn(lines, k) rewrite the body"""
    def f(text):
        loc = ctx.locate(text)
        if not loc:
            return None
        hs, s, e = loc
        L = text[s:e].split('\n')
        ks = [k for k, l in enumerate(L) if l == line_txt]
        if occ >= len(ks):
            return None
        NL2 = fn(list(L), ks[occ])
        if NL2 is None:
            return None
        return text[:s] + '\n'.join(NL2) + text[e:]
    return f


def occs(L):
    seen, out = {}, []
    for k, l in enumerate(L):
        n = seen.get(l, 0)
        seen[l] = n + 1
        out.append(n)
    return out


def stmt_start(L, k):
    """text of the line where the statement containing line k begins (probes must go before a whole statement)"""
    j = k
    while j > 0:
        p = L[j - 1].strip()
        if not p or p.endswith((';', '{', '}', ':')) or p.startswith(('#', '//')):
            break
        j -= 1
    return L[j].strip()


INTS_NARROW = ('u8', 's8', 'u16', 's16', 'unsignedchar', 'signedchar', 'char', 'short', 'unsignedshort')


def reuse_kind(vt, et):
    """'' = same value everywhere (same type / integer promotion), '~' = exact float copy of a small int (the
    use's context must already be float: check the line), None = not allowed"""
    vt, et = vt.replace(' ', ''), et.replace(' ', '')
    if vt == et:
        return ''
    if et in INTS_NARROW and vt in ('int', 's32', 'long', 'signedlong'):
        return ''
    if et in INTS_NARROW and vt in ('f32', 'f64', 'float', 'double'):
        return '~'
    return None


def var_type(ctx, L, name):
    for _, m in ctx.decls(L, False):
        if m.group(4) == name:
            return (m.group(2) + m.group(3)).replace(' ', '')
    return None


# ------------------------------------------------------------------ the edits
def op_postinc(l):
    if not is_stmt(l):
        return []
    out = []
    for m in re.finditer(r'(?<![\w\]\)>.+-])(' + PATH + r')\s*(\+\+|--)', l):
        lv = m.group(1)
        if l.strip() == lv + m.group(2) + ';' or word_count(l, lv.split('->')[0].split('.')[0]) != 1:
            continue
        def fn(L, k, m=m, lv=lv):
            L[k] = l[:m.start()] + lv + l[m.end():]
            L.insert(k + 1, indent(l) + lv + m.group(2) + ';')
            return L
        out.append(('postinc %s' % lv, fn))
    return out


def op_preinc(l):
    t = l.strip()
    if not (is_stmt(l) or (t.startswith('if (') and t.endswith('{'))):
        return []
    out = []
    for m in re.finditer(r'(\+\+|--)\s*(' + PATH + r')', l):
        lv = m.group(2)
        if t == m.group(1) + lv + ';' or word_count(l, lv.split('->')[0].split('.')[0]) != 1:
            continue
        def fn(L, k, m=m, lv=lv):
            L[k] = l[:m.start()] + lv + l[m.end():]
            L.insert(k, indent(l) + lv + m.group(1) + ';')
            return L
        out.append(('preinc %s' % lv, fn))
    return out


def op_compound(l):
    if not is_stmt(l):
        return []
    t, ind, out = l.strip(), indent(l), []
    m = re.match(r'^(' + PATH + r')\s*(\+|-|\*|/|\||&|\^|<<|>>)=\s*(.+);$', t)
    if m and not has_call(m.group(1)):
        rhs = m.group(3)
        rhs = rhs if re.fullmatch(ATOM, rhs) else '(' + rhs + ')'
        out.append(('uncompound %s' % m.group(1), lambda L, k, m=m, rhs=rhs:
                    L[:k] + [ind + '%s = %s %s %s;' % (m.group(1), m.group(1), m.group(2), rhs)] + L[k + 1:]))
    m = re.match(r'^(' + PATH + r')\s*=\s*(' + PATH + r')\s*(\+|-|\*|/|\||&|\^|<<|>>)\s*(' + ATOM + r');$', t)
    if m and re.sub(r'\s', '', m.group(1)) == re.sub(r'\s', '', m.group(2)):
        out.append(('compound %s' % m.group(1), lambda L, k, m=m:
                    L[:k] + [ind + '%s %s= %s;' % (m.group(1), m.group(3), m.group(4))] + L[k + 1:]))
    m = re.match(r'^(' + PATH + r')\s*(\+\+|--);$', t)
    if m:
        o = '+' if m.group(2) == '++' else '-'
        out.append(('inc+= %s' % m.group(1), lambda L, k, m=m, o=o: L[:k] + [ind + '%s %s= 1;' % (m.group(1), o)] + L[k + 1:]))
        out.append(('inc=+ %s' % m.group(1), lambda L, k, m=m, o=o:
                    L[:k] + [ind + '%s = %s %s 1;' % (m.group(1), m.group(1), o)] + L[k + 1:]))
    m = re.match(r'^(' + PATH + r')\s*(\+|-)=\s*1;$', t)
    if m:
        out.append(('+=inc %s' % m.group(1), lambda L, k, m=m: L[:k] + [ind + m.group(1) + m.group(2) * 2 + ';'] + L[k + 1:]))
    return out


DEF_RE = re.compile(r'^(\s*)(?:([A-Za-z_][\w:]*\s*\**)\s+)?([A-Za-z_]\w*)\s*=\s*([^;=][^;]*);\s*$')


def op_reuse(ctx, text, L, k, probe_cache):
    l = L[k]
    m = DEF_RE.match(l)
    if not m or not PURE_READ.match(m.group(4).strip()) or m.group(2) in KW:
        return []
    v, E = m.group(3), m.group(4).strip()
    if m.group(2) and m.group(2).strip() in KW:
        return []
    vt = (m.group(2) or '').replace(' ', '') or var_type(ctx, L, v)
    if not vt:
        return []
    key = (l.strip(), E)
    if key not in probe_cache:
        probe_cache[key] = ctx.probe_type(text, l.strip(), E)
    et = probe_cache[key]
    kind = reuse_kind(vt, et) if et else None
    if kind is None:
        return []
    base = re.match(r'[A-Za-z_]\w*', E).group(0)
    pat = re.compile(r'(?<![\w>.])' + re.escape(E).replace(r'\ ', r'\s*') + r'(?![\w\[])')
    out, ind0 = [], len(indent(l).expandtabs())
    for j in range(k + 1, min(len(L), k + 13)):
        lj = L[j]
        if lj.strip() and len(indent(lj).expandtabs()) < ind0:
            break
        if assigns(lj, v) or assigns(lj, base) or has_call(lj) or re.search(re.escape(E) + r'\s*(?:[-+*/|&^]?=(?!=)|\+\+|--)', lj):
            break
        if pat.search(lj):
            def fn(L2, kk, j=j, lj=lj):
                jj = kk + (j - k)
                if jj >= len(L2) or L2[jj] != lj:
                    return None
                L2[jj] = pat.sub(v, lj)
                return L2
            out.append(('reuse%s %s for %s' % (kind, v, E[:30]), fn))
    return out


def op_defup(L, k):
    l = L[k]
    m = DEF_RE.match(l)
    if not m or has_call(m.group(4)) or m.group(2):
        return []
    v, E = m.group(3), m.group(4)
    names = set(re.findall(r'[A-Za-z_]\w*', E))
    mem = bool(re.search(r'->|\[|\.', E))
    out = []
    for n in (1, 2, 3):
        j = k - n
        if j < 0:
            break
        p = L[j]
        if not is_stmt(p) or word_count(p, v) or any(assigns(p, x) for x in names) or (mem and has_call(p)) \
                or len(indent(p)) != len(indent(l)):
            break
        out.append(('defup%d %s' % (n, v), lambda L2, kk, n=n: L2[:kk - n] + [L2[kk]] + L2[kk - n:kk] + L2[kk + 1:]))
    return out


SUM3 = re.compile(r'(?<=[(=,]|\s)(\(?)\s*(' + ATOM + r')\s*\+\s*(\(?)\s*(' + ATOM + r')\s*(\)?)\s*\+\s*(\(?)\s*(' + ATOM +
                  r')\s*(\)?)(?=\s*(?:[)<>=!;,]))')


def op_assoc(ctx, text, l, probe_cache, at):
    out = []
    m = SUM3.search(l)
    if not m:
        return out
    a, b, c = m.group(2), m.group(4), m.group(7)
    seg = m.group(0)
    if seg.count('(') != seg.count(')'):
        return out
    pre = l[:m.start()].rstrip()
    if pre.endswith(('-', '*', '/', '%', '<<', '>>', '&', '|', '^', '!', '~')):
        return out
    for x in (a, b, c):
        if re.fullmatch(r'\d+|0x[0-9A-Fa-f]+', x):
            continue
        key = (at, x)
        if key not in probe_cache:
            probe_cache[key] = ctx.probe_type(text, at, x)
        t = probe_cache[key]
        if not t or any(f in t for f in FLOATY):
            return out
    forms = set()
    for p, q, r in itertools.permutations((a, b, c)):
        forms.add('%s + %s + %s' % (p, q, r))
        forms.add('%s + (%s + %s)' % (p, q, r))
    cur = re.sub(r'\s+', ' ', seg.strip())
    for i, f in enumerate(sorted(forms)):
        if f == cur:
            continue
        lead = seg[:len(seg) - len(seg.lstrip())]
        new = l[:m.start()] + lead + f + l[m.end():]
        out.append(('assoc#%d %s' % (i, f[:40]), lambda L, k, new=new: L[:k] + [new] + L[k + 1:]))
    return out


def tmpname(E, used):
    base = re.findall(r'\w+', E)[-1]
    for n in (base, base + 'Tmp', 'tmp'):
        if n not in used:
            return n
    return None


def op_loadtemp(ctx, L, k, used):
    l = L[k]
    m = DEF_RE.match(l)
    if not m or m.group(2) or not PURE_READ.match(m.group(4).strip()):
        return []
    v, E = m.group(3), m.group(4).strip()
    vt = var_type(ctx, L, v)
    n = tmpname(E, used)
    if not vt or not n:
        return []
    ind = indent(l)
    out = [('loadtemp %s' % v, lambda L2, kk: L2[:kk] + ['%s{' % ind, '%s    %s %s = %s;' % (ind, vt, n, E),
                                                         '%s    %s = %s;' % (ind, v, n), '%s}' % ind] + L2[kk + 1:])]
    if not ctx.is_c:
        out.append(('loadtemp2 %s' % v, lambda L2, kk: L2[:kk] + ['%s%s %s = %s;' % (ind, vt, n, E),
                                                                  '%s%s = %s;' % (ind, v, n)] + L2[kk + 1:]))
    return out


FOR_RE = re.compile(r'^(\s*)for \(([^;]*); ([^;]*); ([A-Za-z_]\w*) = (' + PATH + r')\) \{\s*$')


def op_forwhile(ctx, L, k, used):
    m = FOR_RE.match(L[k])
    if not m:
        return []
    ind = m.group(1)
    end = next((j for j in range(k + 1, len(L)) if L[j] == ind + '}'), None)
    if end is None or any(re.search(r'\bcontinue\b', x) for x in L[k + 1:end]):
        return []
    v, E = m.group(4), m.group(5)
    vt = var_type(ctx, L, v)
    n = tmpname(E, used)
    out = []

    def mk(temp):
        def fn(L2, kk):
            e2 = next((j for j in range(kk + 1, len(L2)) if L2[j] == ind + '}'), None)
            if e2 is None:
                return None
            head = ([ind + m.group(2) + ';'] if m.group(2).strip() else []) + [ind + 'while (%s) {' % (m.group(3) or '1')]
            if temp:
                step = [ind + '    %s %s = %s;' % (vt, n, E), ind + '    %s = %s;' % (v, n)]
            else:
                step = [ind + '    %s = %s;' % (v, E)]
            return L2[:kk] + head + L2[kk + 1:e2] + step + L2[e2:]
        return fn
    out.append(('forwhile %s' % v, mk(False)))
    if vt and n and not ctx.is_c:
        out.append(('forwhile+temp %s' % v, mk(True)))
    return out


def make(text, ctx, limit=400):
    loc = ctx.locate(text)
    if not loc:
        return []
    hs, s, e = loc
    L = text[s:e].split('\n')
    oc = occs(L)
    used = set(re.findall(r'[A-Za-z_]\w*', text[hs:e]))
    probe_cache = {}
    ops = []
    for k, l in enumerate(L):
        cand = []
        cand += op_postinc(l) + op_preinc(l) + op_compound(l)
        cand += op_reuse(ctx, text, L, k, probe_cache)
        cand += op_defup(L, k)
        cand += op_assoc(ctx, text, l, probe_cache, stmt_start(L, k))
        cand += op_loadtemp(ctx, L, k, used)
        cand += op_forwhile(ctx, L, k, used)
        for name, fn in cand:
            ops.append(('%s @%d' % (name, k), line_op(ctx, l, oc[k], fn)))
    return ops[:limit]
