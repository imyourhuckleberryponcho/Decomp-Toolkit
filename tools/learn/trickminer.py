"""trickminer.py - learn which small source edits make functions match, from a git history.

usage (project root):
  python tools/learn/trickminer.py --tag mine                           this project (functions exact in report.json)
  python tools/learn/trickminer.py --repo PATH --tag NAME --matching-from-configure     another project, bare mirrors too
  python tools/learn/trickminer.py --repo PATH --tag NAME --assume-exact | --exact-path TEXT

For every commit each changed C/C++ function is compared before / after. An edit is kept when it is small (--maxlines),
the function existed before, and the function is known to match today. Only the LAST such edit of a function counts:
normally the one that made it match. Bulk mechanical edits and renames are labelled, not counted as tricks.
--matching-from-configure: only files that the project's HEAD configure.py lists as Object(Matching...) / MatchingFor(...);
  a Makefile project whose build is checked by a hash counts every C file under src/.
Output: build/scratch/learn/<tag>.md (ranked catalogue with examples) and <tag>.json (input of fixrules.py).
Only git data is read; nothing of the other project is executed.
"""
import argparse
import difflib
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import unitlib as U   # noqa: E402

TYPES = r'(?:s8|u8|s16|u16|s32|u32|s64|u64|f32|f64|int|long|short|char|bool|BOOL|float|double|unsigned int|unsigned|signed char|unsigned char)'
DEF = re.compile(r'^(?!\s)(?!(?:if|for|while|switch|return|else|do)\b)[\w][\w\s\*&:<>,~]*?\b([\w:~]+)\s*\(([^;{}]*)\)\s*(?:const\s*)?\{?\s*$', re.M)


def git(repo, *args):
    return subprocess.run(['git', '-C', repo] + list(args), capture_output=True, text=True,
                          encoding='utf-8', errors='replace').stdout


def functions(text):
    """name -> body text (from the definition line to the closing brace)."""
    out = {}
    if not text:
        return out
    for m in DEF.finditer(text):
        name = m.group(1)
        brace = text.find('{', m.start())
        if brace < 0 or text.find(';', m.start(), brace) >= 0:
            continue
        depth = 0
        for i in range(brace, len(text)):
            c = text[i]
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    out.setdefault(name, text[m.start():i + 1])
                    break
    return out


def norm(l):
    return re.sub(r'\s+', ' ', l.strip())


def classify(before, after):
    b = [norm(l) for l in before.split('\n') if norm(l)]
    a = [norm(l) for l in after.split('\n') if norm(l)]
    sm = difflib.SequenceMatcher(None, b, a)
    removed, added = [], []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op != 'equal':
            removed += b[i1:i2]
            added += a[j1:j2]
    cats = set()
    if sorted(b) == sorted(a) and b != a:
        cats.add('reorder lines (declaration/statement order)')
        return cats, removed, added
    rem, add = ' '.join(removed), ' '.join(added)
    tre = re.compile(r'\b' + TYPES + r'\b')
    if Counter(tre.findall(rem)) != Counter(tre.findall(add)):
        if re.sub(TYPES, 'T', rem) == re.sub(TYPES, 'T', add):
            cats.add('type change only')
        else:
            cats.add('type change')
    cast = re.compile(r'\((?:const\s+)?' + TYPES + r'\s*\**\)|\((?:\w+)\s*\*+\)')
    if len(cast.findall(add)) > len(cast.findall(rem)):
        cats.add('cast added')
    if len(cast.findall(add)) < len(cast.findall(rem)):
        cats.add('cast removed')
    declre = re.compile(r'^(?:const |volatile |static )*[\w:]+\s*\**\s+\w+(?:\[[^\]]*\])?(?:\s*=.*)?;$')
    nd_a = [l for l in added if declre.match(l)]
    nd_r = [l for l in removed if declre.match(l)]
    if len(nd_a) > len(nd_r):
        cats.add('temporary/local variable added')
    if len(nd_a) < len(nd_r):
        cats.add('local variable removed (expression inlined)')
    if re.search(r'!= 0|\(bool\)|\? 1 : 0|\? true : false|!\w', rem + ' ' + add) and \
            Counter(re.findall(r'!= 0|\(bool\)|\? 1 : 0|\? true : false', rem)) != Counter(re.findall(r'!= 0|\(bool\)|\? 1 : 0|\? true : false', add)):
        cats.add('boolean form (!= 0 / (bool) / ?: / !x)')
    for kw, label in (('volatile', 'volatile'), ('inline', 'inline helper'), ('static', 'static'),
                      ('#pragma', 'pragma'), ('goto', 'goto')):
        if rem.count(kw) != add.count(kw):
            cats.add(label)
    for kw in ('if', 'else', 'return', 'break', 'continue', 'while', 'for', 'switch', 'case', 'do'):
        if len(re.findall(r'\b%s\b' % kw, rem)) != len(re.findall(r'\b%s\b' % kw, add)):
            cats.add('control flow (if/return/break/loop/switch shape)')
            break
    if re.sub(r'0x[0-9A-Fa-f]+|\b\d+(\.\d+)?f?\b', 'N', rem) == re.sub(r'0x[0-9A-Fa-f]+|\b\d+(\.\d+)?f?\b', 'N', add) and rem != add:
        cats.add('constant/literal change')
    ops = re.compile(r'(\w[\w\.\->\[\]]*)\s*(==|!=|\+|\*|&|\||<|>)\s*(\w[\w\.\->\[\]]*)')
    rset = {(x, o, y) for x, o, y in ops.findall(rem)}
    for x, o, y in ops.findall(add):
        if (y, {'<': '>', '>': '<'}.get(o, o), x) in rset and (x, o, y) not in rset:
            cats.add('operand order swapped')
            break
    idn = lambda x: re.sub(r'0x[0-9A-Fa-f]+|\b\d+(\.\d+)?f?\b', 'N', re.sub(r'\b[A-Za-z_]\w*\b', 'I', x))
    if not cats and idn(rem) == idn(add):
        calls_r = re.findall(r'\b([A-Za-z_]\w*)\s*\(', rem)
        calls_a = re.findall(r'\b([A-Za-z_]\w*)\s*\(', add)
        if Counter(calls_r) != Counter(calls_a) and len(calls_r) == len(calls_a):
            cats.add('different callee (wrong function was called)')
        elif re.sub(r'0x[0-9A-Fa-f]+|\b\d+\b', 'N', rem) == re.sub(r'0x[0-9A-Fa-f]+|\b\d+\b', 'N', add):
            cats.add('constant/offset change')
        else:
            words_r = set(re.findall(r'\b[A-Za-z_]\w*\b', rem)) - set(re.findall(r'\b[A-Za-z_]\w*\b', add))
            if any(w[0].isupper() or w.endswith('_t') for w in words_r):
                cats.add('struct/pointer type change')
            else:
                cats.add('rename only (not a trick)')
    if not cats:
        cats.add('other (expression rewritten)')
    return cats, removed, added


class CatFile:
    """`git cat-file --batch`: file contents at <rev>:<path> without one process per file ('' when missing)"""
    def __init__(self, repo):
        self.p = subprocess.Popen(['git', '-C', repo, 'cat-file', '--batch'], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE)

    def show(self, spec):
        self.p.stdin.write(spec.encode('utf-8') + b'\n')
        self.p.stdin.flush()
        head = self.p.stdout.readline().split()
        if len(head) < 3 or head[1] != b'blob':
            if len(head) >= 3:   # a tree/commit: skip its body
                self.p.stdout.read(int(head[2]) + 1)
            return ''
        data = self.p.stdout.read(int(head[2]))
        self.p.stdout.read(1)
        return data.decode('utf-8', 'replace').replace('\r\n', '\n')


def matching_files(repo):
    """source paths (as written in configure.py) of objects marked Matching at HEAD"""
    cfg = git(repo, 'show', 'HEAD:configure.py')
    out = set()
    for m in re.finditer(r'Object\(\s*(Matching|MatchingFor\([^)]*\)|Equivalent)\s*,\s*"([^"]+)"', cfg):
        if m.group(1) != 'Equivalent':
            out.add(m.group(2))
    if not out and not cfg:
        # pre-dtk Makefile projects: the build is sha1-checked against the game and
        # unmatched code stays in asm/, so every C file under src/ matches (except explicit nonmatching dirs)
        top = git(repo, 'ls-tree', '--name-only', 'HEAD').split()
        if 'Makefile' in top and any(t.endswith('.sha1') or t == 'sha1' for t in top) or 'nonmatchings' in top:
            for f in git(repo, 'ls-tree', '-r', '--name-only', 'HEAD', 'src').split('\n'):
                if re.search(r'\.(c|cpp|cp)$', f) and 'nonmatching' not in f.lower():
                    out.add(f)
    return out


def exact_names_here():
    """{(unit, function)} exact in this project; mangled C++ names also by their plain method name"""
    ex = set()
    for unit, fs in U.units().items():
        for f in fs:
            if float(f.get('fuzzy_match_percent') or 0) >= 100:
                ex.add((unit, f['name']))
                ex.add((unit, re.split(r'__(?=[0-9A-ZFCQ])', f['name'])[0]))
    return ex


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--repo', default='.')
    ap.add_argument('--tag', default='mine')
    ap.add_argument('--assume-exact', action='store_true')
    ap.add_argument('--exact-path', help='only files whose path contains this text count as exact')
    ap.add_argument('--maxlines', type=int, default=8)
    ap.add_argument('--max-commits', type=int, default=5000)
    ap.add_argument('--matching-from-configure', action='store_true')
    ap.add_argument('--out-dir', default='build/scratch/learn')
    a = ap.parse_args()
    matched = matching_files(a.repo) if a.matching_from_configure else None
    if matched is not None and not matched:
        print('no Matching objects found in %s configure.py: nothing to mine' % a.repo)
        os.makedirs(a.out_dir, exist_ok=True)
        U.save_json(os.path.join(a.out_dir, '%s.json' % a.tag), [])
        return
    here = os.path.abspath(a.repo) == os.path.abspath('.')
    exact = exact_names_here() if here and not a.assume_exact else None
    # one git log for every commit's subject + changed files, and one long-lived `git cat-file --batch`
    # for file contents.
    log = git(a.repo, 'log', '--no-merges', '--format=\x01%H\x02%s', '--name-only', '-n', str(a.max_commits))
    commits, subjects, files_of = [], {}, {}
    for chunk in log.split('\x01')[1:]:
        head, _, rest = chunk.partition('\n')
        c, _, subj = head.partition('\x02')
        commits.append(c)
        subjects[c] = subj
        files_of[c] = [f for f in rest.split('\n') if re.search(r'\.(c|cpp|cp|h)$', f)]
    cat = CatFile(a.repo)
    last = {}  # (path, func) -> record; commits are newest first, so keep the first seen
    for n, c in enumerate(commits):
        for path in files_of[c]:
            if a.exact_path and a.exact_path not in path:
                continue
            if matched is not None and not any(path.endswith(m) for m in matched):
                continue
            new = cat.show('%s:%s' % (c, path))
            old = cat.show('%s~1:%s' % (c, path))
            if not old or not new:
                continue
            fo, fn = functions(old), functions(new)
            nchanged = sum(1 for k, v in fn.items() if k in fo and fo[k] != v)
            for name, body in fn.items():
                if name not in fo or fo[name] == body:
                    continue
                key = (path, name)
                if key in last:
                    continue
                before, after = fo[name], body
                b = [norm(l) for l in before.split('\n')]
                aa = [norm(l) for l in after.split('\n')]
                changed = sum(1 for op in difflib.ndiff(b, aa) if op[:1] in '+-')
                if changed == 0 or changed > a.maxlines * 2:
                    last[key] = None  # big rewrite: not a "trick"
                    continue
                if exact is not None:
                    unit = re.sub(r'^src/|\.(c|cpp|cp)$', '', path)
                    if (unit, name) not in exact and (unit, name.split('::')[-1]) not in exact:
                        last[key] = None
                        continue
                cats, rem, add = classify(before, after)
                if nchanged > 10 and cats & {'rename only (not a trick)', 'constant/offset change',
                                             'different callee (wrong function was called)',
                                             'struct/pointer type change', 'other (expression rewritten)'}:
                    cats = {'bulk mechanical edit (not a trick)'}
                subj = subjects.get(c, '').strip()
                last[key] = {'commit': c[:9], 'subject': subj[:100], 'file': path, 'func': name,
                             'cats': sorted(cats), 'removed': rem[:6], 'added': add[:6]}
        if n % 50 == 0:
            print('commit %d/%d, %d tricks so far' % (n, len(commits), sum(1 for v in last.values() if v)), file=sys.stderr)
    recs = [v for v in last.values() if v]
    count = Counter(c for r in recs for c in r['cats'])
    os.makedirs(a.out_dir, exist_ok=True)
    U.save_json(os.path.join(a.out_dir, '%s.json' % a.tag), recs, indent=1)
    out = ['# Trick catalog (%s): %d matching edits from %d commits' % (a.tag, len(recs), len(commits)), '']
    by = defaultdict(list)
    for r in recs:
        for c in r['cats']:
            by[c].append(r)
    for cat, k in count.most_common():
        out.append('## %s: %d' % (cat, k))
        for r in by[cat][:6]:
            out.append('- `%s` %s (%s)' % (r['func'], r['file'], r['commit']))
            for l in r['removed']:
                out.append('    - `%s`' % l)
            for l in r['added']:
                out.append('    + `%s`' % l)
        out.append('')
    with open(os.path.join(a.out_dir, '%s.md' % a.tag), 'w', encoding='utf-8') as f:
        f.write('\n'.join(out))
    print('\n'.join(out[:2] + ['%5d  %s' % (k, c) for c, k in count.most_common()]))


if __name__ == '__main__':
    main()
