"""fixrules.py - turn mined matching edits into rewrite rules for the source search.

usage (project root):
  python tools/learn/fixrules.py build        build/scratch/learn/*.json -> build/scratch/learn/fix_rules.json
  python tools/learn/fixrules.py show [N]     the N most frequent rules

Input: what trickminer.py / refmine.py wrote. Edits of 1-3 lines become rules with ONE identifier mapping across the lines
(`x = f(y); g(x);` -> `$1 = $2($3); $4($1);`): if / for headers, statement order, casts, types, operand order, a call
result held in a new local.
A rule is kept only when it cannot change behaviour by itself: same numbers, same called functions, same identifiers (new
ones only as a freshly DECLARED local), same braces, no comments / strings / preprocessor / volatile / goto.
Rules are SEARCH HINTS. Use them in the sweep with the plug-in next to this file:
  python tools/search/tricksweep.py UNIT FUNC --plugins tools/learn/fixrule_tricks.py
(or SEARCH_TRICK_PLUGINS=tools/learn/fixrule_tricks.py for every run, also inside the runner). When the rules change,
re-open functions your runner has parked: a new rule set is a new chance for them.
"""
import glob
import os
import re
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import unitlib as U   # noqa: E402

RULES = 'build/scratch/learn/fix_rules.json'
KEEP = set('if else return for while switch case break default do sizeof goto continue u8 s8 u16 s16 u32 s32 '
           'int bool f32 f64 void char short long unsigned signed const volatile static true false NULL nullptr '
           'TRUE FALSE this'.split())
MAXL = 3
IDENT = re.compile(r'(?<![\w$@])[A-Za-z_]\w*')
NUM = re.compile(r'(?<![\w$@])\d[\w.]*')
CALL = re.compile(r'\b([A-Za-z_]\w*)\s*\(')
DECL = r'(?:^|[;{(,]\s*|^\s*(?:const\s+)?)(?:[A-Za-z_][\w:]*)(?:\s*\*+\s*|\s+)%s\s*(?:=|;|\[|,)'
BAD = ('//', '/*', '"', "'", '#', 'volatile', 'goto', 'M2C_', '(0,', 'asm')


def abstract_lines(lines):
    """joint abstraction: ['x = f(y);', 'g(x);'] -> (['$1 = $2($3);', '$4($1);'], ['x','f','y','g'])"""
    ids = []

    def rep(m):
        w = m.group(0)
        if w in KEEP:
            return w
        if w not in ids:
            ids.append(w)
        return '$%d' % (ids.index(w) + 1)
    return [IDENT.sub(rep, l.strip()) for l in lines], ids


def rkey(tpl):
    return re.sub(r'\s+', '', tpl)


def key_of(tpls):
    return '\n'.join(rkey(t) for t in tpls)


def make_rule(before, after):
    b = [l.strip() for l in before if l.strip()]
    a = [l.strip() for l in after if l.strip()]
    if not b or not a or len(b) > MAXL or len(a) > MAXL + 1:
        return None
    text_b, text_a = '\n'.join(b), '\n'.join(a)
    if any(x in text_b + text_a for x in BAD):
        return None
    if Counter(NUM.findall(text_b)) != Counter(NUM.findall(text_a)):
        return None
    if Counter(n for n in CALL.findall(text_b) if n not in KEEP) != Counter(n for n in CALL.findall(text_a) if n not in KEEP):
        return None
    for ch in '{}()[]':
        if text_b.count(ch) != text_a.count(ch) and ch in '{}':
            return None
    tb, ids = abstract_lines(b)
    mapping = {w: '$%d' % (i + 1) for i, w in enumerate(ids)}
    new_ids = [w for w in dict.fromkeys(IDENT.findall(text_a)) if w not in KEEP and w not in mapping]
    fresh = {}
    for w in new_ids:
        if not re.search(DECL % re.escape(w), text_a, re.M):
            return None          # a new identifier that is not a freshly declared local = other code, not a trick
        fresh[w] = '@%d' % (len(fresh) + 1)
    a_ids = {w for w in IDENT.findall(text_a) if w not in KEEP and w not in fresh}
    if a_ids != set(ids):
        return None              # an identifier vanished or appeared: behaviour change
    ta = [IDENT.sub(lambda m: mapping.get(m.group(0), fresh.get(m.group(0), m.group(0))), l) for l in a]
    if key_of(tb).replace(chr(10), '') == key_of(ta).replace(chr(10), ''):      # layout only
        return None
    return tb, ta, sorted(fresh)


def records():
    out = []
    for path in sorted(glob.glob('build/scratch/learn/*.json')):
        if os.path.basename(path) in ('fix_rules.json', 'mined_heads.json'):
            continue
        for r in U.load_json(path, []):
            out.append((r.get('removed', []), r.get('added', []), r.get('commit', '?'), r.get('file', '?')))
    return out


def build():
    rules = defaultdict(lambda: defaultdict(lambda: {'n': 0, 'ex': [], 'names': []}))
    recs = records()
    kept = 0
    for before, after, sha, fname in recs:
        r = make_rule(before, after)
        if not r:
            continue
        tb, ta, names = r
        k = key_of(tb)
        slot = rules[k]['\n'.join(ta)]
        if sha not in [e[0] for e in slot['ex']]:
            slot['n'] += 1
            slot['ex'].append((sha, fname))
            slot['names'] = slot['names'] or names
            kept += 1
    out = {}
    for k, alts in rules.items():
        items = sorted(alts.items(), key=lambda kv: -kv[1]['n'])[:6]
        out[k] = [{'after': a.split('\n'), 'n': v['n'], 'names': v['names'], 'ex': v['ex'][:3]} for a, v in items]
    U.save_json(RULES, out)
    sizes = Counter(k.count('\n') + 1 for k in out)
    print('fixrules: %d records -> %d usable fixes -> %d rule keys (%s lines) -> %s' % (
        len(recs), kept, len(out), ', '.join('%d: %d' % kv for kv in sorted(sizes.items())), RULES))


def show(n):
    rules = U.load_json(RULES, {})
    flat = [(alt['n'], k, alt) for k, alts in rules.items() for alt in alts]
    flat.sort(key=lambda x: -x[0])
    for cnt, k, alt in flat[:n]:
        print('%3d x  %s\n       -> %s' % (cnt, k.replace('\n', ' | '), ' | '.join(alt['after'])))


if __name__ == '__main__':
    if any(x in ('-h', '--help') for x in sys.argv[1:]):
        print(__doc__)
    elif (sys.argv[1] if len(sys.argv) > 1 else 'build') == 'build':
        build()
    else:
        show(int(sys.argv[2]) if len(sys.argv) > 2 else 30)
