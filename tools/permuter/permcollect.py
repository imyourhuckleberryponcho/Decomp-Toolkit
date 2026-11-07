"""permcollect.py - turn permuter results into a REVIEW folder. Never edits the project's real source files.

usage: python tools/permuter/permcollect.py [RUN ...] [--all] [--out DIR] [--top 2] [--no-verify]
                                            [--permuter DIR] [--runs DIR]
  RUN      run folder names or paths (plain runs or <symbol>__tandem folders); --all = every run in --runs
  --out    review folder (default: build/scratch/perm_review/<timestamp>)
  --top    how many best outputs per run to try (default 2)

For each run it takes the best output(s) of the permuter, cuts out the function, and maps the permuter's TOKEN edits
back onto the function as written in the project source (so macros, comments and layout survive; if the preprocessed
tokens do not line up with the source, e.g. the function uses macros, the result is only a hint). Then:
  1. behaviour guard (guard.py + semguard.py): changed constants, swapped arguments, another variable, moved
     statements across a dependency, volatile/goto/(int)this, empty ifs ... -> hints/
  2. the edited source is compiled with the unit's real flags into a private folder (tools/fcheck.py --src, tagged
     tk_collect_*); the function must be exact and no other function of the unit may get worse
Result folders:
  ready/<symbol>/     verified exact: new_source.<ext> (whole unit), old_function.txt, new_function.txt, diff.txt,
                      fcheck.txt, info.json   -> apply with runner/apply.py (re-verifies)
  improved/<symbol>/  natural and better but not exact (same files)
  hints/<symbol>/     needs a hand rewrite: candidate function, diff, reasons (guard / mapping / compile / not better)
REVIEW.md in the review folder lists everything. Read each diff before applying: the guard is conservative, not a proof.
"""
import argparse
import difflib
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pcommon as P   # noqa: E402
import guard          # noqa: E402
import permtandem as T  # noqa: E402

TOK = re.compile(r'0[xX][0-9a-fA-F]+[uUlL]*|\d+\.\d*(?:[eE][-+]?\d+)?[fF]?|\d+[uUlL]*|[A-Za-z_]\w*|->|::|<<|>>|\+\+|--|\S')
PUNCT_NOSPACE_BEFORE = {';', ',', ')', ']', '.', '->', '++', '--', '::'}
NOSPACE_AFTER = {'(', '[', '!', '~', '.', '->', '::'}
FCHECK_LINE = re.compile(r'^\s*(\d+\.\d+)%\s+(\d+)\s+(\S+)')


def toks(text, base=0):
    return [(m.group(0), base + m.start(), base + m.end()) for m in TOK.finditer(text)]


def render(tokens, indent):
    """tokens -> source text (single spaces, a line break after each ';')"""
    out, prev = '', None
    for t in tokens:
        if out and not (t in PUNCT_NOSPACE_BEFORE or prev in NOSPACE_AFTER or
                        (t == '(' and re.match(r'\w', prev or '') and prev not in ('if', 'for', 'while', 'switch', 'return')) or
                        (prev == '-' and unary_minus)):
            out += ' '
        if t == '-':
            unary_minus = prev in (None, '=', '(', ',', 'return', '+', '-', '*', '/', '<', '>', '==', '!=', '&&', '||', '?', ':')
        else:
            unary_minus = False
        out += t
        prev = t
        if t == ';':
            out += chr(10) + indent
            prev = None
    return out.rstrip(' ') if not out.endswith(chr(10) + indent) else out[:len(out) - len(indent) - 1]


def restore(tokens, macros, src_has_this):
    """undo the C++ surrogate: macros (A__B -> A::B, self -> this) and bare members (this-> dropped)"""
    out = []
    for t in tokens:
        v = macros.get(t)
        if v is not None:
            out.extend(m.group(0) for m in TOK.finditer(v))
        else:
            out.append(t)
    out = ['this' if t == 'self' else t for t in out]
    if not src_has_this:
        res, i = [], 0
        while i < len(out):
            if out[i:i + 2] == ['this', '->']:
                i += 2
            else:
                res.append(out[i])
                i += 1
        out = res
    return out


def find_function(text, name):
    if '::' in name:
        pat = r'^[^\n;{}#]*\b%s\s*\([^;{]*\)\s*(?:const\s*)?\{' % re.escape(name)
        for m in re.finditer(pat, text, re.M):
            j = _match(text, m.end() - 1)
            if j > 0:
                return m.start(), j + 1, m.end() - 1
        return None
    sp = T.func_span(text, name)
    if not sp:
        return None
    return sp[0], sp[1], text.index('{', sp[0] + len(name))


def _match(text, i):
    depth = 0
    for k in range(i, len(text)):
        if text[k] == '{':
            depth += 1
        elif text[k] == '}':
            depth -= 1
            if depth == 0:
                return k
    return -1


def translate(src_text, meta, base_func, new_func, macros):
    """-> (new_source_text, old_fn_text, new_fn_text, why) ; new_source_text None on failure (why = reason)"""
    sp = find_function(src_text, meta['src_name'])
    if not sp:
        return None, None, None, 'definition of %s not found in %s' % (meta['src_name'], meta['src'])
    start, end, brace = sp
    ts = toks(src_text[brace:end], brace)
    has_this = bool(re.search(r'\bthis\s*->', src_text[brace:end]))
    bt = [t for t, _, _ in toks(base_func[base_func.index('{'):])]
    nt = [t for t, _, _ in toks(new_func[new_func.index('{'):])]
    if meta.get('method'):
        bt, nt = restore(bt, macros, has_this), restore(nt, macros, has_this)
    norm = lambda seq: ['0' if x == 'NULL' else x for x in seq]
    if norm([t for t, _, _ in ts])[:len(bt)] != norm(bt) or len(ts) != len(bt):
        return None, None, None, 'source tokens differ from the preprocessed base (macros / casts inside the function?)'
    sm = difflib.SequenceMatcher(None, bt, nt, autojunk=False)
    body = src_text[brace:end]
    pieces, last, changed = [], 0, 0
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == 'equal':
            continue
        changed += max(i2 - i1, j2 - j1)
        s = ts[i1][1] - brace if i1 < len(ts) else len(body)
        e = ts[i2 - 1][2] - brace if i2 > i1 else s
        line_start = body.rfind('\n', 0, s) + 1
        ind = re.match(r'[ \t]*', body[line_start:]).group(0)
        txt = render(nt[j1:j2], ind)
        if i1 == i2 and txt and not txt.endswith(('\n', ' ')) and re.match(r'\w', body[s:s + 1] or ' '):
            txt += ' '
        pieces.append(body[last:s] + txt)
        last = e
    pieces.append(body[last:])
    new_body = ''.join(pieces)
    new_text = src_text[:brace] + new_body + src_text[end:]
    return new_text, src_text[start:end], src_text[start:brace] + new_body, '%d token edits' % changed


def fcheck(unit, src=None, tag='tk_collect'):
    argv = [sys.executable, os.path.join(os.path.dirname(P.HERE), 'fcheck.py'), unit, '--tag', tag]
    if src:
        argv += ['--src', src, '--extra', '-i ' + os.path.dirname(src_orig_path(unit)).replace('\\', '/')]
    r = subprocess.run(argv, cwd=P.REPO, capture_output=True, text=True, encoding='utf-8', errors='replace')
    text = r.stdout + r.stderr
    rows = {m.group(3): float(m.group(1)) for l in text.splitlines() for m in [FCHECK_LINE.match(l)] if m}
    return rows, text, r.returncode == 0 and 'COMPILE FAILED' not in text


def src_orig_path(unit):
    for ext in ('.cpp', '.c', '.cp'):
        p = os.path.join(P.REPO, 'src', unit + ext)
        if os.path.exists(p):
            return p
    return os.path.join(P.REPO, 'src', unit + '.cpp')


def run_meta(run_dir):
    base = run_dir
    for suf in ('__tandem',):
        if run_dir.endswith(suf):
            base = run_dir[:-len(suf)] + '__twhole'
    return json.load(open(os.path.join(base, 'perm_meta.json'), encoding='utf-8')), base


def candidates(run_dir, top):
    """[(score, source.c path)] best first, and the original function text + macros"""
    meta, mdir = run_meta(run_dir)
    if run_dir.endswith('__tandem'):
        sc = P.read(os.path.join(run_dir, 'best', 'score.txt')).split()[0] if os.path.exists(os.path.join(run_dir, 'best', 'score.txt')) else None
        cands = [(int(sc), os.path.join(run_dir, 'best', 'source.c'))] if sc is not None else []
        orig = P.read(os.path.join(run_dir, 'orig_function.c'))
        basec = P.read(os.path.join(mdir, 'base.c'))
    else:
        cands = [(s, os.path.join(d, 'source.c')) for s, d in P.outputs(run_dir)][:top]
        basec = P.read(os.path.join(run_dir, 'base.c'))
        orig = T.split_base(basec)[1]
    macros = {}
    if meta.get('method'):
        macros = {k: v.strip() for k, v in re.findall(r'^#define (\w+) ([^\n]+)$', basec.replace('(,)', ','), re.M)
                  if k in ('self',) or '::' in v}
    return meta, cands, orig, macros


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('run_names', nargs='*', metavar='RUN')
    ap.add_argument('--all', action='store_true')
    ap.add_argument('--out')
    ap.add_argument('--top', type=int, default=2)
    ap.add_argument('--no-verify', action='store_true')
    P.add_common_args(ap)
    a = ap.parse_args()
    rdir = P.runs_dir(a.runs)
    run_names = a.run_names if not a.all else sorted(
        d for d in os.listdir(rdir) if os.path.isdir(os.path.join(rdir, d)) and not d.startswith('_')
        and not d.endswith(('__tfocus', '__twhole')))
    out = a.out or os.path.join(P.REPO, 'build', 'scratch', 'perm_review', time.strftime('%Y%m%d_%H%M%S'))
    for sub in ('ready', 'improved', 'hints'):
        os.makedirs(os.path.join(out, sub), exist_ok=True)
    review = []
    baseline = {}
    for name in run_names:
        rd = name if os.path.isdir(name) else os.path.join(rdir, name)
        try:
            meta, cands, orig, macros = candidates(rd, a.top)
        except (OSError, ValueError, TypeError, KeyError) as e:
            print('skip %s: %s' % (name, e))
            continue
        sym, unit = meta['symbol'], meta['unit']
        if not cands:
            print('%-50s no output' % sym)
            continue
        src_path = meta['src'] if os.path.exists(meta['src']) else src_orig_path(unit)
        src_text = P.read(src_path)
        done = False
        for sc, cpath in cands:
            new_c = P.read(cpath)
            nsp = T.func_span(new_c, meta['func_name'])
            if not nsp:
                continue
            new_func = new_c[nsp[0]:nsp[1]]
            reasons = guard.reasons(orig, new_func, src_text)
            new_text, old_fn, new_fn, why = translate(src_text, meta, orig, new_func, macros)
            if new_text is None:
                reasons.append(why)
            kind, info = 'hints', {'score': sc, 'symbol': sym, 'unit': unit, 'candidate': cpath, 'reasons': reasons}
            pct_after = None
            if not reasons and not a.no_verify:
                if unit not in baseline:
                    baseline[unit] = fcheck(unit, None, 'tk_collect_base')[0]
                ext = os.path.splitext(src_path)[1]
                tdir = os.path.join(P.REPO, 'build', 'scratch', 'tk_collect_src'); os.makedirs(tdir, exist_ok=True)
                tmp = os.path.join(tdir, os.path.basename(src_path))   # same basename: __sinit_<name> must match
                open(tmp, 'w', encoding='utf-8').write(new_text)
                rows, ftext, ok = fcheck(unit, tmp, 'tk_collect_new')
                os.remove(tmp)
                if not ok:
                    reasons.append('the edited source does not compile')
                else:
                    before = baseline[unit].get(sym, 100.0)
                    after = rows.get(sym, 100.0)
                    worse = [n for n, p in rows.items() if n != sym and p < baseline[unit].get(n, 100.0)]
                    info.update(percent_before=before, percent_after=after)
                    if worse:
                        reasons.append('other functions got worse: ' + ', '.join(worse[:5]))
                    elif after >= 100.0:
                        kind = 'ready'
                    elif after > before:
                        kind = 'improved'
                    else:
                        reasons.append('not better than the current source (%.2f -> %.2f)' % (before, after))
                    info['fcheck'] = ftext
            elif not reasons and a.no_verify:
                kind = 'improved' if sc else 'ready'
                info['note'] = 'not verified (--no-verify)'
            d = os.path.join(out, kind, P.safe(sym))
            os.makedirs(d, exist_ok=True)
            open(os.path.join(d, 'candidate_function.c'), 'w', encoding='utf-8').write(new_func)
            open(os.path.join(d, 'original_function.c'), 'w', encoding='utf-8').write(orig)
            open(os.path.join(d, 'diff.txt'), 'w', encoding='utf-8').write(guard.unified(guard.normalize(orig), guard.normalize(new_func)))
            if new_text is not None and kind != 'hints':
                open(os.path.join(d, 'old_function.txt'), 'w', encoding='utf-8').write(old_fn)
                open(os.path.join(d, 'new_function.txt'), 'w', encoding='utf-8').write(new_fn)
                open(os.path.join(d, 'new_source' + os.path.splitext(src_path)[1]), 'w', encoding='utf-8').write(new_text)
                fc = info.pop('fcheck', '')
                open(os.path.join(d, 'fcheck.txt'), 'w', encoding='utf-8').write(fc)
            info.pop('fcheck', None)
            info.update(kind=kind, src=src_path, src_name=meta['src_name'], reasons=reasons)
            json.dump(info, open(os.path.join(d, "info.json"), "w"), indent=1)
            line = '%-8s %-50s score %s %s' % (kind.upper(), sym, sc, ('; '.join(reasons)[:120]) if reasons else why)
            print(line)
            review.append(line)
            done = True
            if kind != 'hints':
                break
        if not done:
            print('%-50s nothing usable in the outputs' % sym)
    with open(os.path.join(out, 'REVIEW.md'), 'w', encoding='utf-8') as f:
        f.write('# Permuter results\n\nready = verified exact (apply with runner/apply.py); improved = natural, better, not exact;\n'
                'hints = needs a hand rewrite. Read every diff.\n\n')
        f.write('\n'.join('- ' + l for l in review) + '\n')
    print('review folder:', out)


if __name__ == '__main__':
    main()
