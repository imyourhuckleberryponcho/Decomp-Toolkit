"""Reverse the order of function definitions from a start line to the end of a C++ file.
Each function keeps its linkage (innermost extern "C"/"C++" block) and an optimize_for_size pragma pair around it.
usage: python revfuncs.py FILE START_SIGNATURE"""
from dtkproj import *   # REPO, VER, EXE, HERE; chdir to the project root
import re, sys

path, startsig = sys.argv[1], sys.argv[2]
raw = open(path, 'rb').read().decode('latin-1')
nl = '\r\n' if '\r\n' in raw else '\n'
lines = raw.replace('\r\n', '\n').split('\n')
start = next(i for i, l in enumerate(lines) if l.startswith(startsig))


def ext_of(l):
    return re.match(r'extern "(C|C\+\+)"\s*\{$', l.split('//', 1)[0].strip())


def code_of(l):
    l = re.sub(r'"(\\.|[^"\\])*"', '""', l)
    l = re.sub(r"'(\\.|[^'\\])*'", "''", l)
    return l.split('//', 1)[0]


# linkage stack at the start line
stack = []
for l in lines[:start]:
    c = code_of(l).strip()
    m = ext_of(l)
    if m:
        stack.append(('ext', m.group(1)))
        continue
    for ch in c:
        if ch == '{':
            stack.append(('brace', None))
        elif ch == '}':
            stack.pop()
outer = [s for s in stack]
assert all(k == 'ext' for k, _ in outer), outer

funcs = []
i = start
pending_pragma = False
hoisted = []
comments = []
while i < len(lines):
    l = lines[i]
    c = code_of(l).strip()
    m = ext_of(l)
    if m:
        stack.append(('ext', m.group(1)))
        i += 1
        continue
    if c == '}' and stack and stack[-1][0] == 'ext':
        stack.pop()
        i += 1
        continue
    if c == '#pragma optimize_for_size on':
        pending_pragma = True
        i += 1
        continue
    if c == '#pragma optimize_for_size reset':
        assert funcs and funcs[-1][2]
        i += 1
        continue
    if c == '':
        i += 1
        continue
    if l.strip().startswith('//'):
        comments.append(l)
        i += 1
        continue
    link = next((v for k, v in reversed(stack) if k == 'ext'), 'C++')
    if c.startswith('#'):
        hoisted.append((link, comments + [l]))
        comments = []
        i += 1
        continue
    is_type = c.startswith('struct ') or c.startswith('union ') or c.startswith('typedef ')
    is_inline = ' inline ' in ' ' + c
    if not ('(' in c and not c.endswith(';')) and not is_type:
        raise SystemExit('unexpected line %d: %s' % (i + 1, l))
    depth = 0
    opened = False
    body = []
    while True:
        body.append(lines[i])
        cc = code_of(lines[i])
        depth += cc.count('{') - cc.count('}')
        opened = opened or '{' in cc
        i += 1
        if opened and depth == 0:
            break
    body = comments + body
    comments = []
    if is_type or is_inline:
        hoisted.append((link, body))
        continue
    funcs.append((link, body, pending_pragma))
    pending_pragma = False
assert not stack, stack

out = lines[:start]
# close the outer blocks, emit reversed functions each in its own block
out += ['}'] * len(outer)
for link, body in hoisted:
    out.append('')
    out.append('extern "%s" {' % link)
    out += body
    out.append('}')
if '--protos' in sys.argv:
    for link, body, prag in funcs:
        code = [l for l in body if not l.strip().startswith('//')]
        k = next(n for n, l in enumerate(code) if l.rstrip().endswith('{'))
        sig = ' '.join(l.strip() for l in code[:k + 1])
        if '::' in sig.split('(', 1)[0]:
            continue  # member functions are declared by their class
        out.append('extern "%s" %s;' % (link, sig.rstrip()[:-1].rstrip()))
for link, body, prag in reversed(funcs):
    out.append('')
    out.append('extern "%s" {' % link)
    if prag:
        out.append('#pragma optimize_for_size on')
    out += body
    if prag:
        out.append('#pragma optimize_for_size reset')
    out.append('}')
out.append('')
open(path, 'wb').write(nl.join(out).encode('latin-1'))
print(len(funcs), 'functions reversed; outer blocks', outer)
