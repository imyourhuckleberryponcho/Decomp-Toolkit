"""parsefix.py - make a permuter base.c readable for the permuter's C parser.

Functions or contexts that use C++ the permuter's parser (pycparser) cannot read - constructor-style initialisation
(`static Vec v(0.0f, 1.0f);`), `operator new(...)`, initialiser lists - make the permuter stop with "Syntax error in
base.c". Fix: hide each unreadable STATEMENT (or file-level declaration) behind PERM_IGNORE( ... ). The permuter passes
that text to the compiler unchanged (same generated code, same base score); it just cannot mutate that one statement.
Repeats until the file parses (max 40 statements).

usage: python parsefix.py <run dir or base.c> [--permuter DIR]    (permrun.py calls fix() before every run)
prints: "OK", "FIXED n statements", or "FAILED: <reason>"
"""
import os
import re
import sys

PERMUTER_SRC = os.environ.get('DECOMP_PERMUTER', '')
CONTROL = ('if', 'for', 'while', 'switch', 'do', 'else', 'case', 'default', 'return', 'goto')


def try_parse(source):
    """None when the permuter can parse source, else the offending (expanded) source line text."""
    if PERMUTER_SRC not in sys.path:
        sys.path.insert(0, PERMUTER_SRC)
    from src.perm.parse import perm_parse
    from src.perm.eval import perm_evaluate_one
    from src import ast_util
    from src.error import CandidateConstructionFailure
    import contextlib, io
    try:
        with contextlib.redirect_stdout(io.StringIO()):   # perm_parse prints "Will try 1 different base sources."
            text, _ = perm_evaluate_one(perm_parse(source))
        ast_util.parse_c(text)
        return None
    except CandidateConstructionFailure as e:
        msg = str(getattr(e, 'message', e))
        parts = msg.split('\n\n', 1)
        return parts[1].strip() if len(parts) == 2 and parts[1].strip() else ''
    except Exception as e:   # perm_parse errors etc.: nothing we can repair line-wise
        return '!' + str(e)[:200]


def statement_span(src, pos):
    """[start, end) of the statement/declaration containing offset pos, or None when it is not a plain statement."""
    # start: after the previous ; { } at the same level, or after a preprocessor / PERM_ line
    i = pos
    while i > 0:
        c = src[i - 1]
        if c in ';{}':
            break
        if c == '\n':
            ls = src.rfind('\n', 0, i - 1) + 1
            prev = src[ls:i - 1].strip()
            if prev.startswith('#') or prev.startswith('PERM_') or prev.endswith(')') and prev.startswith('PERM'):
                break
        i -= 1
    start = i
    # end: the ; that closes it at depth 0 (an initialiser's { } is part of it); scan from the start, not from pos
    # (pos may sit inside parentheses)
    depth, j = 0, start
    while j < len(src):
        c = src[j]
        if c in '({[':
            depth += 1
        elif c in ')}]':
            depth -= 1
            if depth < 0:
                return None
        elif c == ';' and depth == 0:
            break
        j += 1
    if j >= len(src):
        return None
    stmt = src[start:j + 1]
    first = re.match(r'\s*(\w+)', stmt)
    if first and first.group(1) in CONTROL:
        return None
    # a '{' that is not an initialiser means we swallowed a block / function body: refuse
    for m in re.finditer(r'\{', stmt):
        before = stmt[:m.start()].rstrip()
        if not (before.endswith('=') or before.endswith(',') or before.endswith('{')):
            return None
    if 'PERM_' in stmt:
        return None
    return start, j + 1


def fix(path, max_fixes=40, verbose=False):
    base = os.path.join(path, 'base.c') if os.path.isdir(path) else path
    src = open(base, encoding='utf-8', errors='replace').read()
    n = 0
    for _ in range(max_fixes + 1):
        bad = try_parse(src)
        if bad is None:
            if n:
                open(base, 'w', encoding='utf-8').write(src)
            return 'FIXED %d statements' % n if n else 'OK'
        if n >= max_fixes:
            break
        if not bad or bad.startswith('!'):
            return 'FAILED: %s' % (bad[1:] if bad else 'no error line')
        # find the offending line in base.c (outside PERM_IGNORE text: those lines never reach the parser)
        hits = [m.start() for m in re.finditer(re.escape(bad), src)]
        hits = [h for h in hits if not src[src.rfind('\n', 0, h) + 1:h].lstrip().startswith('PERM_')]
        span = None
        for h in hits:
            span = statement_span(src, h + len(bad) // 2)
            if span:
                break
        if not span:
            return 'FAILED: cannot isolate the statement of: %s' % bad[:120]
        a, b = span
        lead = len(src[a:b]) - len(src[a:b].lstrip())
        a += lead
        if verbose:
            print('  hide: ' + ' '.join(src[a:b].split())[:140])
        src = src[:a] + 'PERM_IGNORE(' + src[a:b].replace(',', '(,)') + ')' + src[b:]
        n += 1
    return 'FAILED: still unreadable after %d statements' % n


if __name__ == '__main__':
    if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help'):
        print(__doc__)
        sys.exit(0)
    if '--permuter' in sys.argv:
        PERMUTER_SRC = sys.argv[sys.argv.index('--permuter') + 1]
    print(fix(sys.argv[1], verbose='-v' in sys.argv))
