"""After movefn --asm: make a reordered file compile again (iterative compile).
- a variable / type / prototype used before its declaration: the declaration moves up (keeps its extern "C" wrapper)
- a function DEFINITION used before it: a prototype is added instead (the definition keeps its place = .text order)
usage: python hoistdecl.py <unit> <src>"""
from dtkproj import *   # REPO, VER, EXE, HERE; chdir to the project root
import re, subprocess, sys
unit, src = sys.argv[1], sys.argv[2]
FCHECK = HERE + '/fcheck.py'
CR = chr(13)
EXT = re.compile(r'^(extern "C(\+\+)?"|namespace(\s+\w+)?)\s*\{\s*$')


def scan(lines):
    """per line: (depth before line, wrapper line or None)"""
    info = []
    depth = 0
    ext = []
    for l in lines:
        info.append((depth, ext[-1] if ext else None))
        s = re.sub(r'//.*', '', l)
        s = re.sub(r'"(\\.|[^"\\])*"', '""', s)
        s = re.sub(r"'(\\.|[^'\\])*'", "''", s)
        if depth == 0 and EXT.match(l):
            ext.append(l.strip())
            continue
        if depth == 0 and l.strip() == '}' and ext:
            ext.pop()
            continue
        depth += s.count('{') - s.count('}')
    return info


def compile_err():
    r = subprocess.run([sys.executable, FCHECK, unit, '--tag', 'hoist'], capture_output=True, text=True)
    o = r.stdout + r.stderr
    if 'COMPILE FAILED' not in o:
        return None
    m = re.search(r"^#\s+(\d+):.*\n(?:.*\n)?#\s+Error:.*\n#\s+(?:undefined identifier|identifier) '([^']+)'", o, re.M)
    if m:
        return int(m.group(1)), m.group(2)
    m = re.search(r'^#\s+(\d+):\s*(?:const\s+)?(?:struct\s+)?(\w+)[^\n]*\n#\s+Error:.*\n#\s+declaration syntax error', o, re.M)
    if m:
        return int(m.group(1)), m.group(2)
    m = re.search(r"^#\s+(\d+):.*\n(?:.*\n)?#\s+Error:.*\n#\s+function call '([\w:@]+)\(", o, re.M)
    if m:
        return int(m.group(1)), m.group(2), 'overload'
    print(o[-1500:])
    return ('?', None)


done = set()
for it in range(300):
    e = compile_err()
    if e is None:
        print('compiles after %d steps' % it)
        break
    if e[0] == '?':
        sys.exit('unhandled error')
    use, name = e[0], e[1]
    overload = len(e) > 2
    name = name.split('::')[-1]
    if (use, name) in done and not overload:
        sys.exit('no progress on %s at %d' % (name, use))
    done.add((use, name))
    lines = open(src, encoding='latin-1', newline='').read().split('\n')
    info = scan(lines)
    decl = None
    before = '\n'.join(lines[:use])
    for i in range(use, len(lines)):
        d, w = info[i]
        if d == 0 and re.search(r'\b%s\b' % re.escape(name), lines[i]) and not lines[i].startswith((' ', '\t', '}', '//', '#', 'extern "C', 'namespace')):
            if overload:
                sig = lines[i].rstrip(CR).split('{')[0].strip()
                if not re.search(r'\b%s\s*\(' % re.escape(name), sig) or sig.rstrip(';') + ';' in before:
                    continue
            decl = i
            break
    if decl is None:
        sys.exit('no later declaration of %s (used at %d)' % (name, use))
    # extent of the item
    end = decl
    j = decl
    depth = 0
    opened = False
    while True:
        s = re.sub(r'//.*', '', lines[j])
        if '{' in s:
            opened = True
        depth += s.count('{') - s.count('}')
        t = s.rstrip()
        if depth <= 0 and (t.endswith(';') or (opened and t.endswith('}'))):
            break
        j += 1
    end = j
    block = lines[decl:end + 1]
    w = info[decl][1]
    proto = None
    if opened and '(' in lines[decl] and not re.match(r'(struct|class|union|enum|typedef)\b', lines[decl]):
        sig = []
        for l in block:
            sig.append(l.rstrip(CR).strip())
            if '{' in l:
                break
        proto = ' '.join(sig).split('{')[0].rstrip() + ';' + CR
    k = use - 1
    while k > 0 and not (info[k][0] == 0 and lines[k][:1] not in ('', ' ', '\t', '}', '{', CR) and not lines[k].startswith('//')):
        k -= 1
    while k > 0 and (EXT.match(lines[k - 1]) or lines[k - 1].startswith('#pragma') or lines[k - 1].startswith('//')):
        k -= 1
    if proto:
        block = [proto]
    if w and w != info[k][1]:
        block = [w + CR] + block + ['}' + CR]
    print('%s %s (lines %d-%d%s) before %d' % ('proto' if proto else 'hoist', name, decl + 1, end + 1, ' in ' + w if w else '', k + 1))
    if not proto:
        del lines[decl:end + 1]
    lines[k:k] = block
    open(src, 'w', encoding='latin-1', newline='').write('\n'.join(lines))
else:
    print('gave up')
