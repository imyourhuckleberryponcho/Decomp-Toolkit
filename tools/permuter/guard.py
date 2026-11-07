"""guard.py - decide whether a permuter/search candidate may be kept as a natural, behaviour-preserving rewrite.

reasons(old_function_text, new_function_text, context='') -> list of strings; empty = looks like a pure rewrite.

Checks (all text based, deliberately conservative - a flagged candidate goes to hints/ for a person to look at):
  * semguard.risks: changed constants, swapped call arguments, another existing variable used, moved statements across a
    dependency, changed number of returns / branches, narrowing casts, local enum type-pun
  * hack patterns that no human would write: volatile, goto (unless the old code had one), (int)this casts, comma
    expressions `(0, x)`, empty `if (x) {}` blocks, `x ^ 0`, `x * 0`, `x & -1`-like no-ops, sizable padding variables
  * the new text must not be dramatically longer or shorter than the old one (default: more than 40 changed tokens)
"""
import difflib
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import semguard   # noqa: E402

HACKS = [
    (r'\bvolatile\b', 'adds volatile'),
    (r'\bgoto\b', 'adds goto'),
    (r'\(\s*(?:int|s32|u32)\s*\)\s*this\b', 'adds (int)this'),
    (r'\(\s*0\s*,\s*[A-Za-z_(]', 'adds a (0, x) comma expression'),
    (r'\bif\s*\([^()]*\)\s*\{\s*\}', 'adds an empty if block'),
    (r'\^\s*0\b(?!x)', 'adds ^ 0'),
    (r'\*\s*0\b(?!x|\.)', 'adds * 0'),
    (r'\bpad\w*\s*\[', 'adds a padding variable'),
]
TOKEN = re.compile(r'0[xX][0-9a-fA-F]+|\d+\.\d*f?|\d+|[A-Za-z_]\w*|->|<<|>>|\S')


def unified(old, new):
    return '\n'.join(difflib.unified_diff(old.splitlines(), new.splitlines(), 'old', 'new', lineterm='', n=2))


def token_changes(old, new):
    a, b = TOKEN.findall(old), TOKEN.findall(new)
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    return sum(max(i2 - i1, j2 - j1) for op, i1, i2, j1, j2 in sm.get_opcodes() if op != 'equal')


def normalize(text):
    """tokens joined by single spaces, one line per statement/brace: a pretty-printed copy (the permuter re-prints
    its output) then diffs line by line like the raw original"""
    out, cur = [], []
    for t in TOKEN.findall(text):
        cur.append(t)
        if t in (';', '{', '}'):
            out.append(' '.join(cur))
            cur = []
    if cur:
        out.append(' '.join(cur))
    return chr(10).join(out)


def reasons(old_fn, new_fn, context='', max_changes=40):
    old_n, new_n = normalize(old_fn), normalize(new_fn)
    out = []
    for pat, why in HACKS:
        if re.search(pat, new_n) and not re.search(pat, old_n):
            out.append(why)
    n = token_changes(old_n, new_n)
    if n == 0:
        out.append('no token change')
    elif n > max_changes:
        out.append('too many token changes (%d)' % n)
    out += semguard.risks(unified(old_n, new_n), context)
    return out


if __name__ == '__main__':
    if len(sys.argv) < 3 or sys.argv[1] in ('-h', '--help'):
        print('usage: guard.py OLD_FUNCTION_FILE NEW_FUNCTION_FILE   (prints the reasons, nothing = looks natural)')
        sys.exit(0)
    for r in reasons(open(sys.argv[1], encoding='utf-8', errors='replace').read(),
                     open(sys.argv[2], encoding='utf-8', errors='replace').read()):
        print(r)
