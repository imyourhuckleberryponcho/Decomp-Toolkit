"""fixrule_tricks.py - trick plug-in for tricksweep.py: applies the rules learned by fixrules.py.

    python tools/search/tricksweep.py UNIT FUNC --plugins tools/learn/fixrule_tricks.py
1-3 consecutive lines with the same shape as a mined fix's BEFORE lines get that fix's AFTER lines, identifiers mapped. A
learned fix can be right in its own function and wrong here, so rewrites that semguard.py says change constants, control
flow or variables are never tried.
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
for _d in (HERE, os.path.join(os.path.dirname(HERE), 'permuter')):
    if _d not in sys.path:
        sys.path.insert(0, _d)
import fixrules   # noqa: E402
import semguard   # noqa: E402

_STATE = {'stamp': None, 'rules': {}}


def _rules():
    try:
        st = os.stat(fixrules.RULES)
    except OSError:
        return {}
    stamp = (st.st_mtime_ns, st.st_size)
    if stamp != _STATE['stamp']:
        _STATE['rules'], _STATE['stamp'] = fixrules.U.load_json(fixrules.RULES, {}), stamp
    return _STATE['rules']


def t_fixrule(body):
    """a small fix learned from matching histories (fixrules.py build)"""
    rules = _rules()
    if not rules:
        return
    lines, cands = body.split('\n'), []
    for k in (3, 2, 1):
        for i in range(0, len(lines) - k + 1):
            win = lines[i:i + k]
            if any(not l.strip() for l in win):
                continue
            tpls, ids = fixrules.abstract_lines(win)
            ind = re.match(r'[ \t]*', win[0]).group(0)
            for alt in rules.get(fixrules.key_of(tpls), [])[:3]:
                names = alt.get('names', [])
                if any(re.search(r'(?<![\w.>])%s(?!\w)' % re.escape(n), body) for n in names):
                    continue      # the learned local's name is taken here
                fresh = {'@%d' % (j + 1): n for j, n in enumerate(names)}
                out = []
                for l in alt['after']:
                    l = re.sub(r'\$(\d+)', lambda m: ids[int(m.group(1)) - 1] if int(m.group(1)) <= len(ids) else m.group(0), l)
                    out.append(re.sub(r'@\d+', lambda m: fresh.get(m.group(0), m.group(0)), l))
                if any('$' in l or '@' in l for l in out):
                    continue
                outl = [(re.match(r'[ \t]*', win[j]).group(0) if j < len(win) and len(out) == len(win) else ind) + l.strip()
                        for j, l in enumerate(out)]
                if [l.strip() for l in outl] == [l.strip() for l in win]:
                    continue
                if semguard.risks('\n'.join(['@@'] + ['-' + l for l in win] + ['+' + l for l in outl]), body):
                    continue
                cands.append((-k, -alt['n'], 'learned fix (%dx in history): %s -> %s' % (
                    alt['n'], win[0].strip()[:40], outl[0].strip()[:40]), '\n'.join(lines[:i] + outl + lines[i + k:])))
    cands.sort(key=lambda c: c[:2])
    for c in cands[:40]:
        yield c[2], c[3]
