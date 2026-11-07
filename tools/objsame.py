"""objsame.py: prove that an edit left the compiled object byte-identical.

Use it for edits that must not change the original-platform build: porting fixes (PC-only code behind #ifdef),
refactors, header clean-ups, renames.  It compiles the file twice with the exact ninja compile command (once from a
git revision, once from the working tree) and compares the disassembly with relocations (code) and the raw section
bytes (data).  No full build and no link needed, so it also works while a long automated run is using the build
folder.  Exit code 0 = all identical.

usage: python objsame.py SRC [SRC...] [--rev HEAD]
         file edits: working tree versus the same file at git revision REV (default HEAD)
       python objsame.py --save NAME SRC...      (with the OLD header in place)
       ... edit the header ...
       python objsame.py --compare NAME SRC...   (the current tree's objects versus the saved ones)
         header edits: the default mode only swaps the .cpp back, so both builds would see the new header.

Anonymous-namespace names and generated names embed the source file name; they are normalised for the old copy.
Output goes to build/scratch/objsame/.
"""
from dtkproj import *   # REPO, VER, EXE, HERE; chdir to the project root
import json, os, re, subprocess, sys, difflib

OBJDUMP = tool('objdump')
OUT = 'build/scratch/objsame'
os.makedirs(OUT, exist_ok=True)

args = sys.argv[1:]
rev = 'HEAD'
mode = None
if '--save' in args or '--compare' in args:
    k = '--save' if '--save' in args else '--compare'
    i = args.index(k)
    mode, tag = k[2:], args[i + 1]
    del args[i:i + 2]
if '--rev' in args:
    i = args.index('--rev')
    rev = args[i + 1]
    del args[i:i + 2]


def unit_of(src):
    rel = src.replace('\\', '/')
    if rel.startswith('src/'):
        rel = rel[4:]
    return os.path.splitext(rel)[0]


def compile_cmd(src):
    argv = unit_command(unit_of(src))        # ends with: -c <src> -o <obj>
    k = argv.index('-c')
    return argv[:k]


def build(argv, src_path, o):
    r = subprocess.run(argv + ['-c', src_path, '-o', o], capture_output=True, text=True, errors='replace')
    if r.returncode:
        print(r.stdout[-2000:], r.stderr[-2000:])
        return None
    d = subprocess.run([OBJDUMP, '-d', '-r', o], capture_output=True, text=True).stdout
    s = subprocess.run([OBJDUMP, '-s', o], capture_output=True, text=True).stdout
    strip = lambda t: '\n'.join(l for l in t.splitlines() if not re.search(r'file format|^\S+\.o:', l))
    return strip(d), strip(s)


bad = 0
if mode:
    store = '%s/saved_%s.json' % (OUT, tag)
    saved = json.load(open(store)) if mode == 'compare' else {}
    for src in args:
        r = build(compile_cmd(src), src, '%s/%s_%s.o' % (OUT, tag, os.path.basename(src)))
        if r is None:
            print('%s: COMPILE FAILED' % src)
            bad += 1
            continue
        if mode == 'save':
            saved[src] = r
        else:
            same = saved.get(src) == list(r)
            print('%s: %s' % (src, 'SAME' if same else ('NOT SAVED' if src not in saved else 'DIFFERENT')))
            bad += 0 if same else 1
    if mode == 'save':
        json.dump(saved, open(store, 'w'))
        print('saved %d objects as %s' % (len(saved), tag))
    sys.exit(1 if bad else 0)

for src in args:
    argv = compile_cmd(src)
    base = os.path.basename(src)
    # compile the old copy from the same directory so relative includes resolve the same way
    old_in_place = os.path.join(os.path.dirname(src), '.objsame_old_' + base)
    with open(old_in_place, 'wb') as f:
        f.write(subprocess.check_output(['git', 'show', '%s:%s' % (rev, src.replace('\\', '/'))]))
    try:
        a = build(argv, old_in_place, '%s/old_%s.o' % (OUT, base))
    finally:
        os.remove(old_in_place)
    b = build(argv, src, '%s/new_%s.o' % (OUT, base))
    if a is None or b is None:
        print('%s: COMPILE FAILED' % src)
        bad += 1
        continue
    stem, ext = os.path.splitext(base)[0], os.path.splitext(base)[1][1:]
    anon = lambda t: re.sub(r'\d+@unnamed@', '@unnamed@', t)
    norm = lambda t: anon(t.replace('.objsame_old_' + base, base)
                          .replace('_objsame_old_%s_%s' % (stem, ext), '%s_%s' % (stem, ext)))
    b = (anon(b[0]), anon(b[1]))
    same_code, same_data = norm(a[0]) == b[0], norm(a[1]) == b[1]
    print('%s: code %s, sections %s' % (src, 'SAME' if same_code else 'DIFFERENT', 'SAME' if same_data else 'DIFFERENT'))
    if not (same_code and same_data):
        bad += 1
        x = norm(a[0] if not same_code else a[1]).splitlines()
        y = (b[0] if not same_code else b[1]).splitlines()
        for l in list(difflib.unified_diff(x, y, lineterm='', n=1))[:30]:
            print('   ', l)
sys.exit(1 if bad else 0)
