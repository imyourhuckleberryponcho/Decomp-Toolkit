"""permfix.py - turn EXACT permuter results (score 0) into natural source, verified, and install them.

usage (project root): python tools/permuter/permfix.py [--only FUNC] [--dry] [--fresh] [-j 6] [--runs DIR]

permcollect.py refuses score-0 results that use permuter idioms (`int new_var;` passed uninitialised, dead stores, stray
`;`) and cannot map results whose function uses macros (they are expanded in the permuter's base.c). Both are clues the
compiler can finish. For each such result this takes the permuter's version of the function, puts it into the CURRENT
source file and cleans it greedily, recompiling after EVERY step and keeping a step only while the function stays
identical (unitlib.strict_exact) and nothing else in the unit gets worse or starts differing:
  1. delete lines: stray `;`, empty `if (x) {}`, unused/dead permuter temps (`new_var*`)
  2. inline alias temps: `new_var = E; ... f(new_var)` -> `f(E)`
  3. uninitialised temps: replace their reads by real values (parameters, locals, M2C_FIELD expressions)
  4. rename what is left (`new_var2` -> `temp2`): a local is natural code, the name is cosmetic
  5. re-sugar `(*((T *) (((s8 *) x) + off)))` -> `M2C_FIELD(x, T *, off)` (the m2c macro used by drafts)
The result is installed only if it is free of hacks: no added volatile / goto, no `(0, x)`, no `if (1)`, no read of an
uninitialised local, no assignment inside an expression. Otherwise it goes to build/scratch/permfix/hints/.
Results: <runs>/<func>*/output-0-*/source.c. Every installed function is also kept (before / after) in
build/scratch/permfix/installed/ for review: an identical function can still be ugly, read the diff.
Limits: C symbol names. Tried candidates are remembered (build/scratch/permfix/tried.json; --fresh ignores it).
"""
import argparse
import glob
import hashlib
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pcommon as P   # noqa: E402
import unitlib as U   # noqa: E402

OUT = 'build/scratch/permfix'
CACHE_F = OUT + '/tried.json'
log = U.Log(OUT + '/permfix.log')
IDV = r'\bnew_var\d*\b'
MAX_TRIES = 400


# ---- what is not acceptable source -----------------------------------------------------------------------------------
def is_hack(new, orig):
    cnt = lambda pat, t: len(re.findall(pat, t or ''))
    for kw in (r'\bvolatile\b', r'\bgoto\b'):
        if cnt(kw, new) > cnt(kw, orig):
            return 'volatile/goto'
    if re.search(r'\((?:u8|u16|u32|s32|int)\)\s*this\b', new):
        return '(int)this'
    for l in new.split('\n'):
        if re.search(r'^\s*(\w+)\s*=\s*\1\s*;', l) or re.search(r'\bsaved_reg_\w+', l):
            return 'uninitialised read: ' + l.strip()[:60]
        if re.search(r'(?:^|[^\w\s\]])\s*\(\s*0\s*,', l) or re.search(IDV, l) or re.search(r'\bif\s*\(\s*[01]\s*\)', l) \
                or re.search(r'\bif\s*\([^;{}]*\)\s*\{\s*\}', l):
            return 'idiom: ' + l.strip()[:60]
    return None


DECL_RE = re.compile(r'^\s+(?:(?:const|unsigned|signed|struct|volatile)\s+)*[A-Za-z_]\w*[\s\*]+([A-Za-z_]\w*)\s*;\s*$', re.M)


def uninit_reads(d):
    """locals whose first use after the declaration (in text order) is a READ: the trick survives renaming, so it is
    checked on the code, not on the name"""
    out = set()
    for m in DECL_RE.finditer(d):
        x = m.group(1)
        if x in ('return', 'break', 'continue'):
            continue
        u = re.search(r'(&\s*)?\b%s\b(\s*(?:=(?!=)|\+\+|--))?' % re.escape(x), d[m.end():])
        if u and not u.group(1) and not u.group(2):
            out.add(x)
    return out


def expr_assigns(d):
    """assignments inside expressions: `f((t = x))`, `a = (b = c)`, `g(x, t = y)`"""
    return len(re.findall(r'[(,=]\s*\(*\s*[A-Za-z_]\w*\s*(?:[-+*/&|^]|<<|>>)?=(?!=)', d))


def quality(d, orig):
    u = uninit_reads(d) - uninit_reads(orig)
    if u:
        return 'reads %s uninitialised' % ', '.join(sorted(u))
    if expr_assigns(d) > expr_assigns(orig):
        return 'assignment inside an expression'
    return None


# ---- candidates --------------------------------------------------------------------------------------------------------
def run_function(folder):
    """the function a run folder is about: the symbol in perm_meta.json / settings.toml, else the folder name"""
    meta = U.load_json(os.path.join(folder, 'perm_meta.json'), {})
    if meta.get('symbol'):
        return meta['symbol']
    try:
        return P.run_dir_function(folder)
    except (OSError, AttributeError):
        return re.sub(r'__(tandem|tfocus|twhole|focus|whole)$', '', os.path.basename(folder))


def all_candidates(runs):
    """{function: [(label, definition text, diff text)]} of score-0 outputs, newest first"""
    out = {}
    for d in glob.glob(os.path.join(runs, '*')):
        zeros = sorted(glob.glob(d + '/output-0-*/source.c'), key=os.path.getmtime, reverse=True)
        if not zeros:
            continue
        fn = run_function(d)
        for o in zeros:
            t = P.read(o)
            sp = U.definition(t, fn)
            if not sp:
                continue
            df = os.path.join(os.path.dirname(o), 'diff.txt')
            c = (os.path.relpath(o, runs), t[sp[0]:sp[1]], P.read(df) if os.path.exists(df) else '')
            if c[1] not in [x[1] for x in out.get(fn, [])]:
                out.setdefault(fn, []).append(c)
    return {fn: cs[:4] for fn, cs in out.items()}


# ---- mapping the permuter's text back onto ours -------------------------------------------------------------------------
def expand_m2c(s):
    """M2C_FIELD(e, T, o) -> (*(T)((s8 *)(e) + (o))), innermost (last) first"""
    for _ in range(20):
        ms = list(re.finditer(r'\bM2C_FIELD\s*\(', s))
        if not ms:
            return s
        m = ms[-1]
        i, depth, args, start = m.end(), 1, [], m.end()
        while i < len(s) and depth:
            c = s[i]
            if c == '(':
                depth += 1
            elif c == ')':
                depth -= 1
                if depth == 0:
                    args.append(s[start:i])
            elif c == ',' and depth == 1:
                args.append(s[start:i])
                start = i + 1
            i += 1
        if len(args) != 3:
            return s
        s = s[:m.start()] + '(*(%s)((s8 *)(%s) + (%s)))' % (args[1], args[0], args[2]) + s[i:]
    return s


def canon(line):
    s = re.sub(r'[\s()]', '', expand_m2c(line))
    return s[:-1] if s.endswith('{') else s


# *((u32 *) (((s8 *) arg0) + 0xC))  ->  M2C_FIELD(arg0, u32 *, 0xC)
FIELD_RE = re.compile(r'\*\s*\(\s*\(\s*([A-Za-z_][\w ]*?\s*\*+)\s*\)\s*\(\s*\(\s*\(\s*s8\s*\*\s*\)\s*'
                      r'(?:\(\s*([A-Za-z_]\w*)\s*\)|([A-Za-z_]\w*))\s*\)'
                      r'\s*\+\s*(0x[0-9A-Fa-f]+|\d+)\s*\)\s*\)')


def sugar(s):
    for _ in range(8):
        n = FIELD_RE.sub(lambda m: 'M2C_FIELD(%s, %s, %s)' % (
            m.group(2) or m.group(3), re.sub(r'\s*(\*+)$', r' \1', m.group(1).strip()), m.group(4)), s)
        n = re.sub(r'\((M2C_FIELD\((?:[^()]|\([^()]*\))*\))\)', r'\1', n)
        if n == s:
            break
        s = n
    return s


def patch_onto(orig, dtext):
    """the permuter's -/+ lines (pretty-printed, macro-expanded) applied to OUR definition by canonical line match
    (macros expanded, spaces and parentheses ignored). None if a removed line is not found or braces change."""
    lines = orig.split('\n')
    can = [canon(l) for l in lines]
    ptr, dels, ins = 0, set(), {}
    for l in dtext.split('\n'):
        if l.startswith(('---', '+++', '@@')) or not l or l[0] not in ' -+':
            continue
        body = l[1:]
        if body.strip() in ('{', '}'):
            if l[0] != ' ':
                return None
            continue
        c = canon(body)
        if not c:
            continue
        if l[0] in ' -':
            j = next((k for k in range(ptr, len(lines)) if can[k] == c and k not in dels), None)
            if j is None:
                if l[0] == '-':
                    return None
                continue
            if l[0] == '-':
                dels.add(j)
            ptr = j + 1
        else:
            ins.setdefault(ptr, []).append(body)
    if not dels and not ins:
        return None
    out = []
    for k in range(len(lines) + 1):
        if k in ins:
            prev = [x for x in dels if x < k]
            ref = lines[max(prev)] if prev else (lines[k] if k < len(lines) else '')
            indent = re.match(r'\s*', ref).group(0) or '    '
            brace = ref.rstrip().endswith('{') and max(prev, default=-1) == k - 1
            for t in ins[k]:
                t = sugar(t.strip())
                if brace and not t.endswith(('{', ';')):
                    t += ' {'
                    brace = False
                out.append(indent + t)
        if k < len(lines) and k not in dels:
            out.append(lines[k])
    return '\n'.join(out)


def reformat(d):
    """the permuter's layout -> `{` on the statement line, 4-space indents, no blank lines"""
    out = []
    for l in d.split('\n'):
        if not l.strip():
            continue
        ind = len(l) - len(l.lstrip(' '))
        t = l.strip()
        if t == '{' and out and not out[-1].rstrip().endswith((';', '{', '}', ':')):
            out[-1] += ' {'
            continue
        out.append(' ' * (ind * 2) + t)
    return sugar('\n'.join(out))


# ---- the cleaner ---------------------------------------------------------------------------------------------------------
class Fixer:
    def __init__(self, unit, src, tag):
        self.unit, self.src, self.tag = unit, src, tag
        self.tmp = '%s/%s/%s' % (OUT, tag, os.path.basename(src))
        os.makedirs(os.path.dirname(self.tmp), exist_ok=True)
        self.text = U.read_source(src)
        self.base, self.basebad, _ = U.scores(unit, None, tag)
        self.tries = 0

    def with_def(self, fn, d):
        sp = U.definition(self.text, fn)
        return self.text[:sp[0]] + d + self.text[sp[1]:] if sp else None

    def exact(self, fn, d):
        new = self.with_def(fn, d)
        if new is None or self.tries >= MAX_TRIES:
            return False
        self.tries += 1
        U.write_source(self.tmp, new)
        after, bad, obj = U.scores(self.unit, self.tmp, self.tag)
        if not after or after.get(fn, 0) < 100 - 1e-9 or not U.strict_exact(self.unit, fn, obj):
            return False
        if U.no_worse(self.base, after, fn) or (bad - self.basebad):
            return False
        self.last = (after, bad)
        return True

    def clean(self, fn, d):
        changed = True
        while changed and self.tries < MAX_TRIES:
            changed = False
            lines = d.split('\n')
            for i in range(len(lines) - 2, 0, -1):
                l = lines[i]
                if re.match(r'^\s*;\s*$', l) or re.search(IDV, l) or re.search(r'\bif\s*\([^;{}]*\)\s*\{\s*\}', l):
                    nd = '\n'.join(lines[:i] + lines[i + 1:])
                    if self.exact(fn, nd):
                        d, lines, changed = nd, nd.split('\n'), True
            for i, l in enumerate(lines):
                m = re.match(r'^(\s*)(new_var\d*)\s*=\s*(.+);\s*$', l)
                if not m:
                    continue
                x, e = m.group(2), m.group(3).strip()
                body = '\n'.join(lines[:i] + lines[i + 1:])
                simple = re.fullmatch(r'[\w\.\->\[\]\s\*\(\)]+', e) and '(' not in e.replace('(void *)', '')
                # a function as the replacement: the expression may contain backslashes
                nd = re.sub(r'\b%s\b(?!\s*=[^=])' % x, lambda _: e if simple else '(%s)' % e, body)
                nd = re.sub(r'^\s*[\w\s\*]+\b%s\s*;\s*\n' % x, '', nd, flags=re.M)
                if nd != d and self.exact(fn, nd):
                    d, changed = nd, True
                    break
            if changed:
                continue
            sp = U.func_span(d, fn)
            if sp:
                body, plist = d[sp[1]:sp[2]], sp[3]
                for x in sorted(set(re.findall(IDV, d))):
                    for rep, _ in U.value_pool(body, plist, 'r3'):
                        if re.search(IDV, rep):
                            continue
                        nd = re.sub(r'(?<![\w.>])%s\b(?!\s*=[^=])' % x, lambda _: rep, d)
                        nd = re.sub(r'^\s*[\w\s\*]+\b%s\s*;\s*\n' % x, '', nd, flags=re.M)
                        if nd != d and self.exact(fn, nd):
                            d, changed = nd, True
                            break
                    if changed:
                        break
        names = sorted(set(re.findall(IDV, d)), key=lambda s: (len(s), s))
        if names:
            nd, used = d, set(re.findall(r'\b\w+\b', d))
            for k, x in enumerate(names):
                n, j = 'temp' if k == 0 else 'temp%d' % (k + 1), k + 1
                while n in used:
                    j += 1
                    n = 'temp%d' % j
                used.add(n)
                nd = re.sub(r'\b%s\b' % x, n, nd)
            if self.exact(fn, nd):
                d = nd
        nd = sugar(d)
        if nd != d and self.exact(fn, nd):
            d = nd
        return d

    def run(self, fn, cands, dry):
        if not self.base:
            return 'base compile failed'
        if self.base.get(fn, 0) >= 100:
            return 'already exact'
        sp = U.definition(self.text, fn)
        if not sp:
            return 'definition not found in the source'
        orig = self.text[sp[0]:sp[1]]
        why = 'no candidate stays exact in the current file'
        for lab, d, dtext in cands:
            self.tries = 0
            # first choice: only the permuter's changed lines, carried onto OUR text (keeps layout and macros)
            pd = patch_onto(orig, dtext) if dtext else None
            if pd and self.exact(fn, pd):
                d, lab = pd, lab + ', patched'
            elif not self.exact(fn, d):
                sd, so = U.func_span(d, fn), U.func_span(orig, fn)   # keep our signature, take its body
                if not (sd and so):
                    continue
                d = orig[:so[1]] + d[sd[1]:]
                if not self.exact(fn, d):
                    continue
            else:
                rd = reformat(d)
                if self.exact(fn, rd):
                    d = rd
            d = self.clean(fn, d)
            hack = is_hack(d, orig) or quality(d, orig)
            if hack:
                why = 'cleaned version still has %s (%s)' % (hack, lab)
                os.makedirs(OUT + '/hints', exist_ok=True)
                U.write_source('%s/hints/%s.c' % (OUT, re.sub(r'\W', '_', fn)), d)
                continue
            if not self.exact(fn, d):   # refresh self.last for the final text
                continue
            self.text = self.with_def(fn, d)
            self.base, self.basebad = self.last
            os.makedirs(OUT + '/installed', exist_ok=True)
            U.write_source('%s/installed/%s.c' % (OUT, re.sub(r'\W', '_', fn)),
                           '// was:\n' + orig + '\n\n// now (%s):\n' % lab + d + '\n')
            if not dry:
                U.write_source(self.src, self.text)
            return 'INSTALLED (%s, %d compiles)' % (lab, self.tries)
        return why


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--only', default='')
    ap.add_argument('--dry', action='store_true', help='clean and verify, but do not edit src/')
    ap.add_argument('--fresh', action='store_true')
    ap.add_argument('-j', type=int, default=6)
    ap.add_argument('--runs', default=os.environ.get('DECOMP_PERM_RUNS'))
    a = ap.parse_args()
    cache = {} if a.fresh else U.load_json(CACHE_F, {})
    rep = U.report()
    todo = {}
    for fn, cands in sorted(all_candidates(P.runs_dir(a.runs)).items()):
        if fn not in rep or rep[fn][1] >= 100 or a.only not in fn:
            continue
        unit = rep[fn][0]
        src = U.source_of(unit)
        if not src:
            continue
        text = U.read_source(src)
        sp = U.definition(text, fn)
        key = hashlib.md5(('\0'.join(c[1] for c in cands) + '\0' + str(sp and text[sp[0]:sp[1]])).encode('utf-8', 'surrogateescape')).hexdigest()[:12]
        if cache.get(fn) != key:
            todo.setdefault(unit, []).append((fn, cands, key))
    log('permfix: %d functions with score-0 results not in src/ yet, %d units%s' % (
        sum(len(v) for v in todo.values()), len(todo), ' (dry)' if a.dry else ''))

    def do_unit(item):
        unit, fns = item
        fx = Fixer(unit, U.source_of(unit), 'permfix_' + re.sub(r'\W', '_', unit)[-60:])
        res = []
        for fn, cands, key in fns:
            try:
                r = fx.run(fn, cands, a.dry)
            except Exception as e:   # one odd candidate must not stop the others
                r = 'error: %r' % e
            log('  %-9s %s %s: %s' % ('installed' if r.startswith('INSTALLED') else 'skip', unit, fn, r))
            res.append(r)
            if not a.dry:
                cache[fn] = key
        return res
    with ThreadPoolExecutor(a.j) as ex:
        results = sum(ex.map(do_unit, todo.items()), [])
    if not a.dry:
        U.save_json(CACHE_F, cache, indent=0)
    log('permfix: installed %d of %d; now run ninja' % (sum(r.startswith('INSTALLED') for r in results), len(results)))


if __name__ == '__main__':
    main()
