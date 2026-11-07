#!/usr/bin/env python3
"""Per-function match check for one unit, safe to run in parallel.

usage: python fcheck.py <unit> [--src FILE] [--tag NAME] [--all] [--diff FUNC]
  unit  = path under src/ without extension, e.g. game/app/foo
  --src = compile this file instead of src/<unit>.cpp (scratch experiments)
  --tag = scratch name (default: unit basename); use a unique tag per job
  --all = also list functions that already match
  --diff FUNC = print objdiff's instruction diff for one function (substring ok)

Compiles with the unit's exact ninja flags into build/scratch/fcheck/<tag>/ (never touches
build/<VER>/src or ninja state), then scores every function with objdiff-cli, the same engine
that produces report.json. 100.0 = exact match including relocation targets.
"""
from dtkproj import *   # REPO, VER, EXE, HERE; chdir to the project root
import argparse, json, os, re, shlex, shutil, subprocess, sys

REPO = REPO


def prune_old(root, keep, max_age=2 * 3600, chance=0.01):
    """tools give every compile its own --tag folder (~0.6 MB) and never delete it; 213k of them
    (~130 GB) filled drive F:. About 1 call in 100 removes tag folders untouched for 2 hours."""
    import random, time
    if random.random() > chance:
        return
    now = time.time()
    try:
        for e in os.scandir(root):
            if e.is_dir() and e.path.replace('\\', '/') != keep and now - e.stat().st_mtime > max_age:
                shutil.rmtree(e.path, ignore_errors=True)
    except OSError:
        pass


def scoring_base(target, obj, out, min_size=16384):
    """objdiff's DATA diff is quadratic when a large target data section is compared with a much
    smaller one (a unit with 156 KB of texture tables vs our 14 bytes took 32 s per report while the compile took
    0.1 s, so batch searches on that unit ran at ~40% CPU). Function scores only
    depend on code + relocations, so the report gets a copy of our object whose large differing data sections
    hold the target's bytes (identical scores, verified on all 35 menu functions; 32 s -> 0.05 s).
    LAYOUT/CONST/DATA checks still read the real object. Returns the object path to give objdiff."""
    od = os.path.abspath('build/binutils/powerpc-eabi-objdump' + EXE)
    oc = os.path.abspath('build/binutils/powerpc-eabi-objcopy' + EXE)

    def sections(o):
        h = subprocess.run([od, '-h', o], capture_output=True, text=True).stdout
        return {n: (int(s, 16), fl) for n, s, fl in re.findall(
            r'^\s*\d+\s+(\S+)\s+([0-9a-f]+)\s+[0-9a-f]+\s+[0-9a-f]+\s+[0-9a-f]+\s+\S+\n\s+(.*)$', h, re.M)}
    try:
        ts, ms = sections(target), sections(obj)
        swap = [n for n, (size, fl) in ts.items() if size >= min_size and 'CONTENTS' in fl and 'CODE' not in fl
                and n in ms and ms[n][0] != size]
        if not swap:
            return obj
        cache = 'build/scratch/fcheck_tdata'
        os.makedirs(cache, exist_ok=True)
        args = []
        for n in swap:
            b = os.path.join(cache, '%s_%s_%d.bin' % (re.sub(r'\W', '_', target), n.strip('.'), int(os.path.getmtime(target))))
            if not os.path.exists(b):
                tmp = b + '.%d.tmp' % os.getpid()
                subprocess.run([oc, '-O', 'binary', '--only-section=' + n, target, tmp], capture_output=True)
                os.replace(tmp, b)
            args += ['--update-section', '%s=%s' % (n, b)]
        fast = os.path.join(out, 'scoring_' + os.path.basename(obj))
        r = subprocess.run([oc] + args + [obj, fast], capture_output=True, text=True)
        return fast if r.returncode == 0 and os.path.exists(fast) else obj
    except Exception:
        return obj


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('unit')
    ap.add_argument('--src')
    ap.add_argument('--tag')
    ap.add_argument('--all', action='store_true')
    ap.add_argument('--diff')
    ap.add_argument('--mw', help='compiler version override, e.g. GC/1.3.2 (report to parent: mw_version= on the Object)')
    ap.add_argument('--extra', default='', help='extra compiler flags, e.g. "-O4,s -lang=c" (report them to the parent for configure.py)')
    a = ap.parse_args()
    os.chdir(REPO)
    unit = a.unit.replace('\\', '/').removeprefix('src/')
    unit = re.sub(r'\.(cpp|c|cp)$', '', unit)
    tag = a.tag or os.path.basename(unit)
    out = f'build/scratch/fcheck/{tag}'
    os.makedirs(out, exist_ok=True)
    prune_old('build/scratch/fcheck', keep=out)

    cmd = subprocess.run(['ninja', '-t', 'commands', f'build/{VER}/src/{unit}.o'],
                         capture_output=True, text=True).stdout.strip().splitlines()
    if not cmd:
        sys.exit(f'no ninja command for {unit} (new file? run python configure.py)')
    cmd = cmd[-1]
    argv = shlex.split(cmd.replace('\\', '/'), posix=True)
    # replace -o dir, source file, drop -MMD
    i = argv.index('-o'); argv[i + 1] = out
    srcs = [k for k, x in enumerate(argv) if re.search(r'\.(cpp|c|cp)$', x) and argv[k - 1] == '-c']
    real_src = argv[srcs[0]]
    src = a.src or real_src
    base = os.path.splitext(os.path.basename(src))[0]
    argv[srcs[0]] = src
    argv = [x for x in argv if x != '-MMD']
    if a.mw:
        argv = [re.sub(r'compilers/GC/[^/]+/', f'compilers/{a.mw}/', x) for x in argv]
    argv[0] = os.path.abspath(argv[0])
    if a.extra:
        k = argv.index('-c'); argv[k:k] = shlex.split(a.extra)
    r = subprocess.run(argv, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stdout[-4000:], r.stderr[-4000:])
        sys.exit('COMPILE FAILED')
    obj = f'{out}/{base}.o'
    if not os.path.exists(obj):
        sys.exit(f'compiler produced no {obj}')

    target = f'build/{VER}/obj/{unit}.o'
    proj = f'{out}/proj'
    os.makedirs(proj, exist_ok=True)
    absp = lambda p: os.path.relpath(p, proj).replace('\\', '/')
    json.dump({'min_version': '2.0.0', 'units': [{'name': 'u', 'target_path': absp(target),
              'base_path': absp(scoring_base(target, obj, out))}]}, open(f'{proj}/objdiff.json', 'w'))
    exe = os.path.abspath('build/tools/objdiff-cli' + EXE)
    if a.diff:
        rep = subprocess.run([exe, 'report', 'generate', '-p', proj, '-o', f'{proj}/r.json', '-f', 'json'],
                             capture_output=True, text=True)
        names = [f['name'] for u in json.load(open(f'{proj}/r.json'))['units'] for f in u.get('functions', [])]
        sel = [n for n in names if n == a.diff] or [n for n in names if a.diff in n]
        if not sel:
            sys.exit('no such function')
        for name in sel[:1]:
            diff_one(exe, target, obj, name)
        return
    rep = subprocess.run([exe, 'report', 'generate', '-p', proj, '-o', f'{proj}/r.json', '-f', 'json'],
                         capture_output=True, text=True)
    if rep.returncode != 0:
        sys.exit(rep.stderr)
    data = json.load(open(f'{proj}/r.json'))
    fns = [f for u in data['units'] for f in u.get('functions', [])]
    good = bad = gb = bb = 0
    rows = []
    for f in fns:
        size = int(f.get('size', 0)); p = f.get('fuzzy_match_percent', 0.0)
        if p >= 100.0:
            good += 1; gb += size
        else:
            bad += 1; bb += size
        if p < 100.0 or a.all:
            rows.append((p, size, f['name']))
    for p, size, name in sorted(rows, key=lambda r: (-r[0], -r[1])):
        print(f'{p:6.2f}%  {size:6}  {name}')
    print(f'== {unit}: {good} exact ({gb} bytes), {bad} not ({bb} bytes)  obj={obj}')
    layout_check(target, obj)
    const_check(target, obj)
    calls_check(obj)
    data_check(target, obj)


def syms(o):
    exe = os.path.abspath('build/binutils/powerpc-eabi-objdump' + EXE)
    out = subprocess.run([exe, '-t', '-h', o], capture_output=True, text=True).stdout
    secs, res = {}, {}
    for ln in out.splitlines():
        m = re.match(r'\s*\d+ (\.\S+)\s+([0-9a-f]+)', ln)
        if m:
            secs[m[1]] = secs.get(m[1], 0) + int(m[2], 16)  # several same-named sections (weak inlines) add up
        m = re.match(r'([0-9a-f]{8}) (.{7}) (\.\S+)\s+([0-9a-f]{8}) (.*)$', ln)
        if m and m[3] in ('.text', '.rodata', '.data', '.bss', '.sbss', '.sdata', '.sdata2', '.ctors', '.dtors') and ' d ' not in ' ' + m[2] + ' ':
            name = m[5].split()[-1]
            if name.startswith('gap_') or m[2][6] == 'd':
                continue
            res.setdefault(m[3], []).append((int(m[1], 16), int(m[4], 16), name))
    return secs, res


def layout_check(target, obj):
    ts, tr = syms(target)
    ms, mr = syms(obj)
    ok = True
    for sec in sorted(set(tr) | set(mr) | {k for k in ts if k in ('.text', '.rodata', '.data', '.bss', '.sbss', '.sdata', '.sdata2')}):
        t = sorted(tr.get(sec, [])); m = sorted(mr.get(sec, []))
        if sec in ('.ctors', '.dtors'):
            continue  # split objects name no .ctors symbol; presence is checked below
        if sec == '.text':
            tn = [x[2] for x in t if x[1] > 0]; mn = [x[2] for x in m if x[1] > 0 and x[2] in set(tn)]
            if tn != mn:
                ok = False
                first = next((i for i, (a, b) in enumerate(zip(tn, mn)) if a != b), min(len(tn), len(mn)))
                print(f'LAYOUT .text: function order differs at #{first}: target {tn[first:first+3]} mine {mn[first:first+3]}')
            # functions only WE emit are only a NOTE: the linker strips unreferenced static functions
            # (placeholders for unused retail functions).
            extra = [x[2] for x in m if x[1] > 0 and x[2] not in set(tn)]
            if extra:
                print(f'NOTE .text: functions only in mine (fine if unreferenced statics - the linker strips them): {extra[:5]}')
            continue
        tl = sorted({a for a, sz, n in t if sz}); ml = sorted({a for a, sz, n in m if sz})
        # function statics (`name$1234`) are compared by `name$` - their numbers differ between builds;
        # skipping them hid a function static sitting after a file static (only the DOL caught it).
        named = lambda xs: {a: re.sub(r'\$\d+$', '$', n.strip('"')) for a, sz, n in xs
                            if sz and not n.strip('"').startswith(('@', 'gap_'))}
        tn_, mn_ = named(t), named(m)
        swapped = [(hex(a), tn_[a], mn_[a]) for a in tn_ if a in mn_ and tn_[a] != mn_[a] and mn_[a] in tn_.values()]
        if swapped:
            ok = False
            print(f'LAYOUT {sec}: named symbols at different offsets (offset, target, mine): {swapped[:4]}')
        if tl != ml:
            ok = False
            print(f'LAYOUT {sec}: symbol offsets target {[hex(x) for x in tl[:10]]}  mine {[hex(x) for x in ml[:10]]}  (target: {[n for a, sz, n in t][:6]})')
    for sec in ('.ctors', '.dtors'):
        # a hand-written __sinit without a .ctors entry passed LAYOUT and only failed the DOL
        if ts.get(sec, 0) and not ms.get(sec, 0):
            ok = False
            print(f'LAYOUT {sec}: target has {ts[sec]:#x} bytes, mine has none (__sinit not registered: let the compiler make it)')
    for sec in ('.rodata', '.data', '.bss', '.sbss', '.sdata', '.sdata2'):
        a, b = ts.get(sec, 0), ms.get(sec, 0)
        if (a + 7) // 8 != (b + 7) // 8:
            ok = False
            print(f'LAYOUT {sec}: size target {a:#x} mine {b:#x}')
    print('LAYOUT OK (function order + data symbol offsets + section sizes)' if ok else 'LAYOUT DIFFERS (unit will not link even if every function is exact)')


def diff_one(exe, target, obj, name):
    # one-shot text diff via dtk disassembly of both objects, function only
    dtk = os.path.abspath('build/tools/dtk' + EXE)
    outs = []
    for o, nm in ((target, 'T'), (obj, 'M')):
        s = f'{os.path.dirname(obj)}/{nm}.s'
        subprocess.run([dtk, 'elf', 'disasm', os.path.relpath(o), os.path.relpath(s)], capture_output=True)
        txt = open(s).read().splitlines()
        body, on = [], False
        for ln in txt:
            if ln.startswith('.fn '):
                on = ln.split()[1].rstrip(',').strip('"') == name
            if on:
                if ln.startswith('.endfn'):
                    body.append(ln); on = False; continue
                ln = re.sub(r'^/\* \S+ \S+\s+(\S\S ){4}\*/\s*', '    ', ln)
                ln = re.sub(r'\.L_[0-9A-F]+', '.L', ln)
                ln = re.sub(r'"?@[0-9]+"?', '@N', ln)
                ln = re.sub(r'\$[0-9]+', '$N', ln)
                body.append(ln)
        outs.append(body)
    import difflib
    print(f'--- target  +++ mine   ({name})')
    for ln in difflib.unified_diff(outs[0], outs[1], lineterm='', n=2):
        print(ln)



def sec_bytes(o):
    exe = os.path.abspath('build/binutils/powerpc-eabi-objdump' + EXE)
    out = subprocess.run([exe, '-s', o], capture_output=True, text=True).stdout
    data, cur = {}, None
    for ln in out.splitlines():
        m = re.match(r'Contents of section (\S+):', ln)
        if m:
            cur = m[1]; data[cur] = bytearray(); continue
        m = re.match(r' ([0-9a-f]+) ((?:[0-9a-f]+ ){1,4})', ln)
        if cur and m:
            data[cur] += bytes.fromhex(m[2].replace(' ', ''))
    return data


def anon_values(o):
    """name -> bytes for compiler-generated data symbols (@N) in data sections."""
    exe = os.path.abspath('build/binutils/powerpc-eabi-objdump' + EXE)
    out = subprocess.run([exe, '-t', o], capture_output=True, text=True).stdout
    data = sec_bytes(o)
    vals = {}
    for ln in out.splitlines():
        m = re.match(r'([0-9a-f]{8}) .{7} (\.\S+)\s+([0-9a-f]{8}) (.*)$', ln)
        if m and m[2] in data:
            name = m[4].split()[-1]
            if name.startswith('@') or '$' in name:
                off, sz = int(m[1], 16), int(m[3], 16)
                vals[name] = bytes(data[m[2]][off:off + sz])
    return vals


def fn_refs(o, tmp):
    dtk = os.path.abspath('build/tools/dtk' + EXE)
    subprocess.run([dtk, 'elf', 'disasm', os.path.relpath(o), os.path.relpath(tmp)], capture_output=True)
    refs, cur = {}, None
    for ln in open(tmp).read().splitlines():
        if ln.startswith('.fn '):
            cur = ln.split()[1].rstrip(','); refs[cur] = []
        elif ln.startswith('.endfn'):
            cur = None
        elif cur:
            for r in re.findall(r'"(@[0-9]+|[^"]*\$[0-9]+)"', ln):
                refs[cur].append(r)
    return refs


def const_check(target, obj):
    """objdiff treats @N constants as interchangeable; compare the referenced VALUES per function."""
    tv, mv = anon_values(target), anon_values(obj)
    tr = fn_refs(target, os.path.dirname(obj) + '/T_const.s')
    mr = fn_refs(obj, os.path.dirname(obj) + '/M_const.s')
    bad = []
    for fn, trefs in tr.items():
        mrefs = mr.get(fn)
        if mrefs is None or len(mrefs) != len(trefs):
            continue
        for a, b in zip(trefs, mrefs):
            va, vb = tv.get(a), mv.get(b)
            if va is not None and vb is not None and va.rstrip(bytes(1)) != vb.rstrip(bytes(1)):
                bad.append(f'{fn}: {a}={va[:16].hex()} vs mine {b}={vb[:16].hex()}')
                break
    if bad:
        print('CONST VALUES DIFFER (code may look exact but uses wrong literals/tables):')
        for b in bad[:15]:
            print('   ', b)
    else:
        print('CONST OK (referenced literal/table values match)')


def data_relocs(o):
    exe = os.path.abspath('build/binutils/powerpc-eabi-objdump' + EXE)
    out = subprocess.run([exe, '-r', o], capture_output=True, text=True).stdout
    rel, cur = {}, None
    for ln in out.splitlines():
        m = re.match(r'RELOCATION RECORDS FOR \[(\S+)\]', ln)
        if m:
            cur = m[1]; rel[cur] = set(); continue
        m = re.match(r'([0-9a-f]{8}) ', ln)
        if cur and m:
            rel[cur].add(int(m[1], 16))
    return rel


def data_check(target, obj):
    """Compare initialised data bytes (relocated words masked); LAYOUT only checks offsets/sizes."""
    td, md = sec_bytes(target), sec_bytes(obj)
    tr, mr = data_relocs(target), data_relocs(obj)
    _, tsyms = syms(target)
    bad = []
    for sec in ('.data', '.rodata', '.sdata', '.sdata2', '.ctors', '.dtors'):
        a, b = td.get(sec), md.get(sec)
        if a is None or b is None:
            continue
        mask = set()
        for r in tr.get(sec, set()) | mr.get(sec, set()):
            mask.update(range(r, r + 4))
        for i in range(min(len(a), len(b))):
            if a[i] != b[i] and i not in mask:
                owner = [n for off, sz, n in sorted(tsyms.get(sec, [])) if off <= i][-1:] or ['?']
                bad.append(f'{sec}+{i:#x} ({owner[0]}): target {a[i]:02x} mine {b[i]:02x}')
                break
    if bad:
        print('DATA DIFFERS (initialised values; the DOL check will fail):')
        for x in bad:
            print('   ', x)
        pool_order_hint(target, obj, td, md)
    else:
        print('DATA OK (initialised data bytes match)')


def pool_order_hint(target, obj, td, md):
    """same anonymous constants in another order = a source-order question, not wrong values.
    Reversed -> deferred file (-inline noauto,deferred + reversed functions, see guide/04); otherwise the
    constants are pooled in SOURCE TEXT order: rewrite the expression so they appear in the target's order
    (e.g. `150*(1+sin(2pi*t))` -> `a = sin(2pi*t); a = 1+a; a = 150*a`), or an earlier
    dead-stripped function used some of them first (placeholder function, see guide/04)."""
    _, ts = syms(target)
    _, ms = syms(obj)
    for sec in ('.sdata2', '.rodata'):
        def vals(syml, data):
            out = []
            for off, sz, n in sorted(syml.get(sec, [])):
                if sz and n.strip('"').startswith('@') and data:
                    out.append(bytes(data[off:off + sz]).hex())
            return out
        t, m = vals(ts, td.get(sec)), vals(ms, md.get(sec))
        if not t or t == m or sorted(t) != sorted(m):
            continue
        # which functions use the constants that moved? one function = expression order inside it
        names = [n.strip('"') for off, sz, n in sorted(ts.get(sec, [])) if sz and n.strip('"').startswith('@')]
        moved = {names[i] for i in range(min(len(t), len(m))) if t[i] != m[i]}
        users, cur = set(), None
        try:
            for ln in open(os.path.join(os.path.dirname(obj), 'T_const.s'), encoding='utf-8', errors='replace'):
                if ln.startswith('.fn '):
                    cur = ln.split()[1].rstrip(',')
                elif cur and any('"%s"' % n in ln for n in moved):
                    users.add(cur)
        except OSError:
            pass
        if len(users) == 1:
            how = 'all used by ONE function (%s): write its expressions so the constants appear in the target order ' \
                  '(source text order), e.g. split the expression into statements' % next(iter(users))
        elif t == m[::-1]:
            how = 'REVERSED across functions %s: deferred file? (-inline noauto,deferred + revfuncs.py)' % sorted(users)[:4]
        else:
            how = 'permuted across functions %s: function order / dead-stripped earlier user / expression order' % sorted(users)[:4]
        print(f'    {sec} constant POOL ORDER differs, same values; {how}:')
        print(f'      target {t[:8]}')
        print(f'      mine   {m[:8]}')


def fn_calls(tmp):
    calls, cur = {}, None
    for ln in open(tmp).read().splitlines():
        if ln.startswith('.fn '):
            cur = ln.split()[1].rstrip(','); calls[cur] = []
        elif ln.startswith('.endfn'):
            cur = None
        elif cur:
            m = re.search(r'	(bl|b)\s+([A-Za-z_][\w@$.]*)\s*$', ln)
            if m and not m.group(2).startswith('.L'):
                calls[cur].append(m.group(2))
    return calls


def calls_check(obj):
    """objdiff can score 100% while a bl goes to a different (sibling) function; compare call targets."""
    d = os.path.dirname(obj)
    tc, mc = fn_calls(d + '/T_const.s'), fn_calls(d + '/M_const.s')
    bad = [f'{fn}: target {[x for x in t if x not in mc[fn]][:3]} mine {[x for x in mc[fn] if x not in t][:3]}'
           for fn, t in tc.items() if fn in mc and t != mc[fn] and len(t) == len(mc[fn])]
    if bad:
        print('CALLS DIFFER (wrong callee; the DOL check will fail):')
        for b in bad[:15]:
            print('   ', b)
    else:
        print('CALLS OK (call targets match)')


if __name__ == '__main__':
    main()
