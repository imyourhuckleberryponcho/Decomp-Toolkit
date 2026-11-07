"""shadowbug.py - draft BUG finder. Lists declarations inside a function body that reuse the name of
  (a) a file-level variable of the same file  (`s32 prevPointSize = w;` meant `prevPointSize = w;`: the store to
      the static vanishes ), or
  (b) a variable of an enclosing block of the same function (`int k = 0;` inside a do-while whose condition reads
      the OUTER k ).
Not the same as shadowscan.py (that one is about loop-counter reuse for register colouring).

usage (from the project root): python shadowbug.py [--all] [path-substring ...]
  --every: also list outer-local shadows whose outer variable is never read after the block.
  default: only the NonMatching units in configure.py. Output: file:line  kind  name  | source line
  Heuristic (regex, brace counting): check each hit by eye; (b) also flags harmless reuse in sibling blocks only
  when the outer declaration is still live, so most hits are worth a look.
"""
from dtkproj import *   # REPO, VER, EXE, HERE; chdir to the project root
import os, re, sys

os.chdir(REPO)
args = [a for a in sys.argv[1:] if not a.startswith('--')]
ALL = '--all' in sys.argv
cfg = open('configure.py', encoding='utf-8').read()
units = re.findall(r'Object\((NonMatching|MatchingFor\([^)]*\))\s*,\s*"([^"]+\.(?:c|cpp))"', cfg)
files = ['src/' + u for st, u in units if ALL or st == 'NonMatching']
if args:
    files = [f for f in files if any(a in f for a in args)]

TYPE = r'(?:(?:const|volatile|unsigned|signed|static|register|struct|enum)\s+)*[A-Za-z_]\w*(?:\s*<[^>]*>)?(?:\s*\*+\s*|\s+)'
DECL = re.compile(r'^\s*' + TYPE + r'(\**)([A-Za-z_]\w*)\s*(=(?!=)|;|\[|,)')
SKIP_FIRST = {'return', 'else', 'case', 'goto', 'delete', 'new', 'typedef', 'using', 'throw', 'sizeof', 'if',
              'while', 'for', 'switch', 'do', 'break', 'continue', 'default', 'extern', 'public', 'private',
              'protected', 'class', 'friend', 'template', 'operator', 'namespace', 'asm'}


def strip(l):
    l = re.sub(r'"(\\.|[^"\\])*"', '""', l)
    l = re.sub(r"'(\\.|[^'\\])*'", "''", l)
    l = re.sub(r'/\*.*?\*/', '', l)
    return l.split('//', 1)[0]


SHOW_ALL = '--every' in sys.argv


def depth_now(kinds):
    return len(kinds)


def used_after_block(lines, start, name, _d):
    # end of the block that holds line `start`, then: is `name` read before the enclosing block ends?
    depth = 0
    i = start - 1
    # walk to the end of the current block
    while i < len(lines):
        for ch in strip(lines[i] if i != start - 1 else lines[i]):
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth < 0:
                    break
        if depth < 0:
            break
        i += 1
    # loop condition right after a do-block: `} while (name ...)`
    rest = strip(lines[i]) if i < len(lines) else ''
    if re.search(r'\b%s\b' % re.escape(name), rest.split('}', 1)[-1]):
        return i + 1
    depth = 0
    j = i + 1
    while j < len(lines):
        l = strip(lines[j])
        if re.match(r'^\s*' + TYPE + r'\**%s\s*(=|;|\[)' % re.escape(name), l):
            return 0   # redeclared in a sibling block before any read: harmless
        if re.search(r'\b%s\b' % re.escape(name), l):
            return j + 1
        for ch in l:
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
        if depth < 0:
            return 0
        j += 1
    return 0


hits = 0
for f in files:
    if not os.path.exists(f):
        continue
    lines = open(f, encoding='latin-1').read().split('\n')
    depth = 0          # brace depth (extern "C" blocks count)
    ext_depth = 0      # depth contributed by extern "C"/namespace/class blocks
    block_kinds = []   # per open brace: 'ext' | 'func' | 'block' | 'type'
    file_vars = set()
    scopes = []        # per open function-level brace: set of names
    for no, raw in enumerate(lines, 1):
        l = strip(raw)
        s = l.strip()
        in_func = 'func' in block_kinds
        in_type = block_kinds and block_kinds[-1] == 'type'
        m = DECL.match(l)
        if m and s.split()[0] not in SKIP_FIRST and '(' not in l[:m.start(2)]:
            name = m.group(2)
            if not in_func and not in_type:
                file_vars.add(name)
            elif in_func:
                if name in file_vars:
                    print('%s:%d  shadows-file-var     %s  | %s' % (f, no, name, raw.strip()))
                    hits += 1
                else:
                    for names in scopes[:-1]:
                        if name in names:
                            live = used_after_block(lines, no, name, depth_now(block_kinds))
                            if live or SHOW_ALL:
                                print('%s:%d  shadows-outer-local%s  %s  | %s' % (f, no, ' LIVE@%d' % live if live else '',
                                                                              name, raw.strip()))
                                hits += 1
                            break
                if scopes:
                    scopes[-1].add(name)
        for i, ch in enumerate(l):
            if ch == '{':
                head = l[:i]
                if in_func or 'func' in block_kinds:
                    block_kinds.append('block')
                    scopes.append(set())
                elif re.search(r'extern\s*"C(\+\+)?"\s*$|namespace\b', head):
                    block_kinds.append('ext')
                elif re.search(r'\b(struct|class|union|enum)\b', head) or s.endswith('= {') or '=' in head:
                    block_kinds.append('type')
                elif re.search(r'\)\s*(const)?\s*$', head) or (not head.strip() and no > 1 and
                                                               strip(lines[no - 2]).rstrip().endswith(')')):
                    block_kinds.append('func')
                    scopes = [set()]
                    # parameters belong to the function scope
                    sig = head if head.strip() else strip(lines[no - 2])
                    for p in re.findall(r'([A-Za-z_]\w*)\s*(?:\[[^\]]*\])?\s*(?:,|\))', sig.split('(', 1)[-1]):
                        scopes[0].add(p)
                else:
                    block_kinds.append('type')
                in_func = 'func' in block_kinds
            elif ch == '}':
                if block_kinds:
                    k = block_kinds.pop()
                    if k in ('block', 'func') and scopes:
                        scopes.pop()
print('%d hits' % hits)
