"""refport.py - import code other projects already matched (found by refmatch.py) into your project, verified.

usage (project root, nothing else building):
  python tools/refs/refport.py [--max N] [--minutes M] [--dry] [--only ADDR] [--retry] [--any-license]
  python tools/refs/refport.py --close     also runs of CLOSE matches whose symbol already has the reference's name
  python tools/refs/refport.py --refresh   re-check the calls of every imported unit against the game

Setup: install unit_overlay.py (see its docstring), start from a clean git tree, commit between sessions.

A RUN is a stretch of consecutive functions of the game that all match functions of ONE compiled reference object. Best
first (bytes that become exact):
  1. pre-check, touching nothing: the reference file is trimmed to the run's functions (falls back to the whole file),
     reference names are #defined to your symbols, and it is compiled ALONE with the reference's compiler + flags. Every
     function refmatch called exact must still be identical to the executable (relocated words masked).
  2. call check: the game's own `bl` targets say which of YOUR functions each reference callee is. Two callees with the
     same name must be the same function; afterwards every call of every exact function must name exactly the
     function the game calls. (A diff tool that ignores call targets scores a wrong callee 100%.)
  3. import: the run's .text range becomes its own unit ref/<repo>/<path>_<ADDR>.<ext> (splits.txt +
     units_overlay.json); the unit it was cut from keeps the functions before the run, the functions after it move to
     <unit>_<ADDR>.c.
  4. gate (report.json before / after, by address): kept only if the exact count rises (--close: enough matched bytes
     gained), no exact function is lost and everything else does not drop. Otherwise every file is restored and rebuilt.
Never cut: units marked as matching, units that own data sections, C++ units.

LICENSES: only projects with a recognised license file (CC0, MIT, BSD, Apache, Unlicense, zlib, ISC) are imported, and the
license is named in the file header; --any-license imports the others too - do not publish those. No license file means
no permission to redistribute. Keep the project's license text and credit it when you publish.
Imported units include headers from your local export ($DECOMP_REFS/src). Copy what they need into your tree (and adapt
the paths in units_overlay.json) before anyone else can build them.
Log: build/scratch/refmatch/refport.log. Failed runs are retried only when their reference project or your code changed.
"""
import argparse
import difflib
import hashlib
import json
import os
import re
import shlex
import struct
import subprocess
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from refcommon import (OBJ, OUT, SRC as REFSRC, SYMS, U, VER, compiler_cmd, masked, read_dol, read_obj, split_flags,   # noqa: E402
                       text_functions, words)

SPLITS = 'config/%s/splits.txt' % VER
OVERLAY = 'config/%s/units_overlay.json' % VER
REPORT = 'build/%s/report.json' % VER
MANGLED = re.compile(r'[A-Za-z0-9]__[A-Za-z0-9]|^__(ct|dt)__|@')
HDR = '/* Imported by refport.py'
DEF_END = '/* refport: end of name map */\n'
REL24 = 10
CLOSE = False
ALIASES = {}   # address -> every .text label there (a label may share its function's address)
log_ = U.Log(OUT + '/refport.log')


def log(msg):
    log_('%s %s' % (time.strftime('%H:%M:%S'), msg))


def sh(argv, timeout=3600):
    return subprocess.run(argv, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout)


def read(path):
    with open(path, encoding='utf-8', errors='replace') as f:
        return f.read()


# ---- licenses --------------------------------------------------------------------------------------------------------
_LIC = {}


def license_of(repo):
    """short license name of an exported project, '' when there is no file or it is not recognised"""
    if repo not in _LIC:
        _LIC[repo] = ''
        d = os.path.join(REFSRC, repo)
        for n in sorted(os.listdir(d)) if os.path.isdir(d) else []:
            if n.upper().split('.')[0] not in ('LICENSE', 'LICENCE', 'COPYING', 'UNLICENSE'):
                continue
            p = os.path.join(d, n)
            if not os.path.isfile(p):
                continue
            t = re.sub(r'\s+', ' ', read(p))
            for name, pat in (('CC0-1.0', 'CC0 1.0|Creative Commons Zero'), ('MIT', 'MIT License|Permission is hereby granted, free of charge'),
                              ('Apache-2.0', 'Apache License'), ('Unlicense', 'released into the public domain'),
                              ('BSD', 'Redistribution and use in source and binary forms'),
                              ('ISC', 'Permission to use, copy, modify, and(?:/or)? distribute'),
                              ('zlib', 'This software is provided .as-is.')):
                if re.search(pat, t):
                    _LIC[repo] = name
                    break
    return _LIC[repo]


# ---- project state -----------------------------------------------------------------------------------------------------
ENTRY = re.compile(r'^(\S+):([^\n]*)\n((?:[ \t]+\S+[^\n]*\n?)+)', re.M)


def read_splits():
    """(text, {unit: (start, end)} of the .text ranges, {units that own data sections})"""
    text = read(SPLITS)
    units, data = {}, set()
    for m in ENTRY.finditer(text):
        if m.group(1) == 'Sections':
            continue
        rng = re.findall(r'^\s+\.text\s+start:0x([0-9A-Fa-f]+) end:0x([0-9A-Fa-f]+)', m.group(3), re.M)
        if len(rng) == 1:
            units[m.group(1)] = (int(rng[0][0], 16), int(rng[0][1], 16))
        elif rng:
            data.add(m.group(1))     # several .text ranges: left alone
            units[m.group(1)] = (min(int(a, 16) for a, _ in rng), max(int(b, 16) for _, b in rng))
        if any(s not in ('.text', 'extab', 'extabindex') for s in re.findall(r'^\s+(\S+)\s+start:', m.group(3), re.M)):
            data.add(m.group(1))
    return text, units, data


def drop_entry(text, unit):
    return re.sub(r'^%s:[^\n]*\n(?:[ \t]+\S+[^\n]*\n?)+\n?' % re.escape(unit), '', text, flags=re.M)


def report_funcs():
    out, complete = {}, set()
    for u in U.load_json(REPORT, {'units': []})['units']:
        if (u.get('metadata') or {}).get('complete'):
            complete.add(U.unit_name(u['name']))
        for f in u.get('functions', []):
            a = (f.get('metadata') or {}).get('virtual_address')
            if a:
                out[int(a)] = (float(f.get('fuzzy_match_percent') or 0), int(f['size']), f['name'], u['name'])
    return out, complete


def overlay():
    d = U.load_json(OVERLAY, {})
    d.setdefault('units', {})
    d.setdefault('removed', [])
    return d


# ---- runs -----------------------------------------------------------------------------------------------------------------
def close_filter(port, funcs):
    """--close: an exact source stays; a close one only if your symbol already carries that very name, so code that is
    merely similar is never imported"""
    name = {f[0]: re.sub(r'_[0-9A-F]{8}$', '', f[2]) for f in funcs}
    out = []
    for p in port:
        a = int(p['addr'], 16)
        ss = [s for s in p['sources'] if s['kind'] == 'exact' or s['func'] == name.get(a) and s.get('score', 0) >= 0.80]
        if ss:
            out.append(dict(p, sources=ss))
    return out


def find_runs(port, funcs, rep):
    """[(gain in bytes, start, end, [(gain, members, source)])] best first; member = (addr, size, name, ref name, kind)"""
    by_obj = defaultdict(dict)
    for p in port:
        for s in p['sources']:
            by_obj[s['obj']].setdefault(int(p['addr'], 16), s)
    order = [f[0] for f in funcs]
    pos = {a: i for i, a in enumerate(order)}
    fmap = {f[0]: f for f in funcs}
    runs = {}
    for amap in by_obj.values():
        idx = sorted(pos[a] for a in amap if a in pos)
        i = 0
        while i < len(idx):
            j = i
            while j + 1 < len(idx) and idx[j + 1] == idx[j] + 1:
                j += 1
            members = [order[k] for k in idx[i:j + 1]]
            i = j + 1
            refnames = [amap[a]['func'] for a in members]
            if len(set(refnames)) != len(refnames):
                continue
            if CLOSE:
                gain = int(sum(max(0.0, 95.0 * amap[a].get('score', 1.0) - rep.get(a, (0,))[0]) * fmap[a][1] / 100.0 for a in members))
                if gain < 40:
                    continue
            else:
                gain = sum(fmap[a][1] for a in members if amap[a]['kind'] == 'exact' and rep.get(a, (0,))[0] < 100)
            if not gain:
                continue
            key = (members[0], members[-1] + fmap[members[-1]][1])
            runs.setdefault(key, []).append((gain, [(a, fmap[a][1], fmap[a][2], amap[a]['func'], amap[a]['kind'])
                                                    for a in members], amap[members[0]]))
    out = []
    for (start, end), items in runs.items():
        items.sort(key=lambda it: (not license_of(it[2]['repo']), -it[0], not it[2].get('done'), it[2]['variant'] != 'own'))
        out.append((items[0][0], start, end, items))
    return sorted(out, key=lambda r: (-r[0], r[1]))


# ---- reference source preparation -----------------------------------------------------------------------------------------
def abs_flags(cflags, root, srcdir):
    flags, out, i = split_flags(cflags), [], 0

    def absp(p):
        return p if os.path.isabs(p) else os.path.join(root, p).replace('\\', '/')
    while i < len(flags):
        f = flags[i]
        if f in ('-i', '-I', '-ir') and i + 1 < len(flags):
            out += [f, absp(flags[i + 1])]
            i += 2
            continue
        m = re.match(r'^-I(?!-)(.+)$', f)      # joined form only for -I (-i... is -inline, -ipa, ...)
        out.append('-I' + absp(m.group(1)) if m else f)
        i += 1
    return out + ['-i', srcdir.replace('\\', '/'), '-i', root]


def name_defines(members, callees):
    d = {m[3]: m[2] for m in members if m[3] != m[2]}
    for ref, mine in sorted(callees.items()):
        if ref != mine and ref not in d:
            d[ref] = mine
    return ''.join('#define %s %s\n' % kv for kv in sorted(d.items())) + DEF_END


TOP = re.compile(r'^(?!\s)(?!(?:if|for|while|switch|return|else|do)\b)\w[\w\s\*&:<>,~]*?\b([\w:~]+)\s*\(([^;{}]*)\)\s*(?:const\s*)?\{?\s*$', re.M)


def top_functions(text):
    """{name: definition text}"""
    out = {}
    for m in TOP.finditer(text):
        brace = text.find('{', m.start())
        if brace < 0 or text.find(';', m.start(), brace) >= 0:
            continue
        depth = 0
        for i in range(brace, len(text)):
            if text[i] == '{':
                depth += 1
            elif text[i] == '}':
                depth -= 1
                if depth == 0:
                    out.setdefault(m.group(1), text[m.start():i + 1])
                    break
    return out


def make_source(root, s, members, trim, callees=None):
    text = read(os.path.join(root, s['src']))
    keep = {m[3] for m in members}
    if trim:
        for name, body in top_functions(text).items():
            if name not in keep and not re.search(r'\binline\b', body.split('{', 1)[0]):
                text = text.replace(body, '', 1)
    hdr = '%s from %s/%s (license: %s). Reference names are mapped to this project\'s symbols. */\n' % (
        HDR, s['repo'], s['src'], license_of(s['repo']) or 'NONE FOUND, do not publish')
    return hdr + name_defines(members, callees or {}) + text


# ---- checks against the executable ----------------------------------------------------------------------------------------
def dol_calls(dol, addr, size):
    """offset -> called address, for every `bl` in the game's copy of the function"""
    out, code = {}, dol(addr, size)
    for off in range(0, len(code) - 3, 4):
        w, = struct.unpack_from('>I', code, off)
        if w >> 26 == 18 and w & 1 and not w & 2:
            disp = w & 0x03FFFFFC
            out[off] = addr + off + (disp - 0x04000000 if disp & 0x02000000 else disp)
    return out


def call_map(obj, members, dol, sym_at):
    """({ref callee name: your symbol}, problem or ''), learned from the calls of the exact members"""
    fs = {f['name']: f for f in read_obj(obj)}
    own = {m[2] for m in members}
    cmap = {}
    for a, size, cname, rname, kind in members:
        f = fs.get(cname)
        if kind != 'exact' or f is None:
            continue
        calls = dol_calls(dol, a, size)
        for off, (typ, target) in f['relocs'].items():
            if typ != REL24 or target in ALIASES.get(calls.get(off), ()):
                continue
            mine = sym_at.get(calls.get(off))
            if mine is None:
                return {}, '%s: call at +0x%X does not land on a known function' % (cname, off)
            if target == mine:
                continue
            if target in own or mine in own:
                return {}, '%s calls %s here, the game calls %s: wrong pairing' % (cname, target, mine)
            if cmap.get(target, mine) != mine:
                return {}, '%s: %s would have to be both %s and %s' % (cname, target, cmap[target], mine)
            cmap[target] = mine
    return cmap, ''


def strict_calls(obj, members, dol, sym_at):
    fs = {f['name']: f for f in read_obj(obj)}
    for a, size, cname, rname, kind in members:
        f = fs.get(cname)
        if kind != 'exact' or f is None:
            continue
        calls = dol_calls(dol, a, size)
        for off, (typ, target) in f['relocs'].items():
            if typ == REL24 and sym_at.get(calls.get(off)) != target and target not in ALIASES.get(calls.get(off), ()):
                return False, '%s calls %s at +0x%X, the game calls %s' % (cname, target, off, sym_at.get(calls.get(off)))
    return True, ''


def compile_alone(path, ver, flags, out):
    os.makedirs(os.path.dirname(out), exist_ok=True)
    if os.path.exists(out):
        os.remove(out)
    cmd = compiler_cmd(ver)
    if not cmd:
        return False, '# compiler %s missing' % ver
    r = sh(cmd + flags + ['-c', path, '-o', out], timeout=300)
    return r.returncode == 0 and os.path.exists(out), (r.stdout + r.stderr).strip()


def precheck(obj, members, dol):
    fs = {f['name']: f for f in read_obj(obj)}
    exact = 0
    for a, size, cname, rname, kind in members:
        f = fs.get(cname)
        if f is None:
            return False, 'function %s missing from the compiled file' % cname
        if kind == 'exact':
            code = dol(a, size)
            if len(f['code']) != size or masked(words(f['code']), f['relocs']) != masked(words(code), f['relocs']):
                return False, '%s (%s) is not exact when compiled here' % (cname, rname)
            exact += 1
    if CLOSE and not exact:
        tot = same = 0.0
        for a, size, cname, rname, kind in members:
            f = fs[cname]
            r = difflib.SequenceMatcher(None, masked(words(f['code']), f['relocs']), words(dol(a, size)), False).ratio()
            tot += size
            same += r * size
        return same >= 0.45 * tot, 'close, %.0f%% of the words' % (100 * same / tot)
    return exact > 0, '%d exact' % exact


# ---- applying a run ------------------------------------------------------------------------------------------------------
class Backup:
    def __init__(self):
        self.saved = {}

    def save(self, p):
        if p not in self.saved:
            self.saved[p] = None
            if os.path.exists(p):
                with open(p, 'rb') as f:
                    self.saved[p] = f.read()

    def write(self, p, text):
        self.save(p)
        os.makedirs(os.path.dirname(p) or '.', exist_ok=True)
        with open(p, 'w', encoding='utf-8', errors='surrogateescape', newline='\n') as f:
            f.write(text)

    def remove(self, p):
        self.save(p)
        if os.path.exists(p):
            os.remove(p)

    def restore(self):
        for p, data in self.saved.items():
            if data is None:
                if os.path.exists(p):
                    os.remove(p)
            else:
                with open(p, 'wb') as f:
                    f.write(data)


def drop_functions(text, names):
    for n in names:
        sp = U.definition(text, n)
        if sp:
            end = sp[1] + (text[sp[1]:sp[1] + 1] == '\n')
            text = text[:sp[0]] + text[end:]
    return text


def blocked(start, end, complete):
    """why this range cannot be carved out, or '' """
    _, units, data = read_splits()
    for u, (us, ue) in units.items():
        if ue <= start or us >= end:
            continue
        stem = os.path.splitext(u)[0]
        if u.startswith('ref/'):
            return 'already imported'
        if stem in complete:
            return u + ' is marked as matching'
        if u in data:
            return u + ' owns data sections'
        if not u.endswith('.c') and (us < start or ue > end):
            return u + ' is not a C file'
    return ''


def apply_run(start, end, s, src_text, flags, funcs, bk):
    text, units, _ = read_splits()
    ov = overlay()
    stem, ext = os.path.splitext(s['src'])
    stem = re.sub(r'^(src|libs)/', '', stem)
    unit = 'ref/%s/%s_%08X%s' % (s['repo'].split('_', 1)[-1], stem, start, ext if ext in ('.c', '.cpp', '.cp') else '.c')
    pieces = [(unit, start, end, src_text)]
    removed = []
    for u, (us, ue) in sorted(units.items(), key=lambda kv: kv[1]):
        if ue <= start or us >= end:
            continue
        src = 'src/' + u
        draft = U.read_source(src) if os.path.exists(src) else ''
        inside = [f for f in funcs if us <= f[0] < ue]
        removed.append(u)
        if us < start:
            pieces.append((u, us, start, drop_functions(draft, [f[2] for f in inside if f[0] >= start])))
        if ue > end:
            rest = '%s_%08X.c' % (os.path.splitext(u)[0], end)
            pieces.append((rest, end, ue, drop_functions(draft, [f[2] for f in inside if f[0] < end])))
            ov['units'][rest] = {'like': u}
    names = {p[0] for p in pieces}
    for u in removed:
        text = drop_entry(text, u)
        if u not in names:
            bk.remove('src/' + u)
            if u in ov['units'] and not any(o.get('like') == u for o in ov['units'].values()):
                del ov['units'][u]
            elif u not in ov['removed']:
                ov['removed'].append(u)
    for nm, a, b, _ in sorted(pieces, key=lambda p: p[1]):
        text = text.rstrip('\n') + '\n\n%s:\n\t.text       start:0x%08X end:0x%08X\n' % (nm, a, b)
    # an argument with a space ('cats off') must carry its quotes: ninja joins cflags with spaces
    ov['units'][unit] = {'mw_version': s['ver'], 'cflags': ['"%s"' % f if ' ' in f else f for f in flags], 'shift_jis': True}
    bk.write(SPLITS, text)
    bk.write(OVERLAY, json.dumps(ov, indent=1) + '\n')
    for nm, a, b, t in pieces:
        bk.write('src/' + nm, t)
    return unit


def rebuild():
    args = []
    if os.path.exists('build.ninja'):     # configure again with the arguments of last time
        m = re.search(r'^configure_args = ((?:.*\$\n)*.*)$', read('build.ninja'), re.M)
        args = shlex.split(m.group(1).replace('$\n', ' ')) if m else []
    r = sh([sys.executable, 'configure.py'] + args, timeout=900)
    if r.returncode != 0:
        return False, 'configure failed: ' + (r.stdout + r.stderr)[-400:]
    r = sh(['ninja', REPORT], timeout=7200)
    if r.returncode != 0:
        out = r.stdout + r.stderr
        k = out.find('FAILED:')
        detail = (out[k:k + 2500] if k >= 0 else out[-2500:]).splitlines()
        err = [l.strip() for l in detail[1:] if l.startswith('#') and l.strip('# -')][:8]
        return False, 'ninja failed: %s | %s' % (detail[0][:160] if detail else '?', ' / '.join(err)[:600])
    return True, ''


def gate(before, after, members):
    lost = [a for a, v in before.items() if v[0] >= 100 and after.get(a, (0,))[0] < 100]
    ex_b, ex_a = sum(v[0] >= 100 for v in before.values()), sum(v[0] >= 100 for v in after.values())
    mem = {m[0] for m in members}

    def weight(d, test):
        return sum(d.get(a, (0, v[1]))[0] * v[1] for a, v in before.items() if test(a)) / 100.0
    if lost:
        return False, '%d exact functions lost (e.g. %s)' % (len(lost), before[lost[0]][2])
    if CLOSE:
        gained = sum(v[0] * v[1] for a, v in after.items() if a in mem) / 100.0 - weight(before, lambda a: a in mem)
        if gained < 24 and ex_a <= ex_b:
            return False, 'members gain only %.0f matched bytes' % gained
    elif ex_a <= ex_b:
        return False, 'no new exact function (%d -> %d)' % (ex_b, ex_a)
    other = weight(before, lambda a: a not in mem) - weight(after, lambda a: a not in mem)
    if other > 0.01:
        return False, 'other functions got worse (%.1f matched bytes lost)' % other
    return True, 'exact %d -> %d, matched bytes %.0f -> %.0f' % (ex_b, ex_a, sum(v[0] * v[1] for v in before.values()) / 100.0,
                                                                sum(v[0] * v[1] for v in after.values()) / 100.0)


# ---- imported units: calls re-checked for every function that scores 100% now ------------------------------------------------
def refresh_unit(unit, o, rng, funcs, rep, dol, sym_at):
    """('ok' | 'changed' | 'fake', why). A 'close' function that turned 100% later was never call-checked before."""
    path = 'src/' + unit
    text = read(path)
    if not text.startswith(HDR):
        return 'ok', 'not imported by refport'
    lines = text.split('\n')
    i, defs = 1, {}
    while i < len(lines) and lines[i].startswith('#define '):
        parts = lines[i].split(None, 2)
        if len(parts) == 3:
            defs[parts[1]] = parts[2].strip()
        i += 1
    if i < len(lines) and lines[i] + '\n' == DEF_END:
        i += 1
    body = '\n'.join(lines[i:])
    rev = {v: k for k, v in defs.items()}
    members = [(a, s, n, rev.get(n, n), 'exact' if rep.get(a, (0,))[0] >= 100 else 'close')
               for a, s, n in funcs if rng[0] <= a < rng[1]]
    own = {m[2] for m in members}
    flags = [f[1:-1] if f.startswith('"') and f.endswith('"') else f for f in o.get('cflags', [])]
    obj = OUT + '/work/refresh.o'
    ok, _ = compile_alone(path, o['mw_version'], flags, obj)
    if not ok:
        return 'fake', 'does not compile alone'
    cmap, why = call_map(obj, members, dol, sym_at)
    if why:
        return 'fake', why
    callees = {k: v for k, v in defs.items() if v not in own}
    callees.update(cmap)
    new = lines[0] + '\n' + name_defines([(0, 0, v, k, '') for k, v in defs.items() if v in own], callees) + body
    status = 'ok'
    if new != text:
        Backup().write(path, new)
        ok, _ = compile_alone(path, o['mw_version'], flags, obj)
        if not ok:      # the name map collides with something in the file: keep the old text
            Backup().write(path, text)
            return 'fake', 'name map does not compile (kept the old names)'
        status = 'changed'
    good, why = strict_calls(obj, members, dol, sym_at)
    return (status, '') if good else ('fake', why)


def refresh(dol, sym_at):
    _, su, _ = read_splits()
    funcs, (rep, _) = text_functions(), report_funcs()
    changed, fakes, okn = 0, [], 0
    for unit, o in sorted(overlay()['units'].items()):
        if not unit.startswith('ref/') or unit not in su or not os.path.exists('src/' + unit):
            continue
        st, why = refresh_unit(unit, o, su[unit], funcs, rep, dol, sym_at)
        if st == 'fake':
            fakes.append((unit, why))
        else:
            okn += 1
            changed += st == 'changed'
    Backup().write(OUT + '/refport_fakes.tsv', ''.join('%s\t%s\n' % x for x in fakes))
    built, msg = rebuild()
    log('refport --refresh: %d units rewritten, %d verified, %d with real call differences (%s/refport_fakes.tsv)%s' % (
        changed, okn, len(fakes), OUT, '' if built else '; BUILD FAILED: ' + msg))


_HEADS = {}


def run_sig(start, end, items):
    """what a failed attempt depended on: the reference files (their commit) and your source of every overlapped unit"""
    d = hashlib.sha1()
    for _, members, s in items:
        if s['repo'] not in _HEADS:
            _HEADS[s['repo']] = U.load_json('%s/%s/manifest.json' % (OBJ, s['repo']), {}).get('head', '')
        d.update(('%s|%s|%s|%s|%d\n' % (s['repo'], _HEADS[s['repo']], s['src'], s['ver'], len(members))).encode())
    _, units, _ = read_splits()
    for u, (us, ue) in sorted(units.items(), key=lambda kv: kv[1]):
        if us < end and ue > start and os.path.exists('src/' + u):
            with open('src/' + u, 'rb') as f:
                d.update(f.read())
    return d.hexdigest()[:16]


def prepare(members, s, dol, sym_at):
    """(source text, flags, why); text None when the run does not pass the checks"""
    if any(MANGLED.search(m[3]) and m[3] != m[2] for m in members):
        return None, None, 'C++ names'
    root = (REFSRC + '/' + s['repo']).replace('\\', '/')
    flags = abs_flags(s['cflags'], root, os.path.dirname(os.path.join(root, s['src'])))
    tmp = OUT + '/work/port' + os.path.splitext(s['src'])[1]
    why = ''
    for trim in (True, False):
        Backup().write(tmp, make_source(root, s, members, trim))
        ok, msg = compile_alone(tmp, s['ver'], flags, tmp + '.o')
        if not ok:
            why = 'compile: ' + ' | '.join(l for l in msg.splitlines() if re.search(r'^#\s+(\d+:|[a-z])', l))[:400]
            continue
        ok, why = precheck(tmp + '.o', members, dol)
        if not ok:
            continue
        cmap, problem = call_map(tmp + '.o', members, dol, sym_at)
        if problem:
            why = problem
            continue
        text = make_source(root, s, members, trim, cmap)
        Backup().write(tmp, text)
        ok, _ = compile_alone(tmp, s['ver'], flags, tmp + '.o')
        ok, why = precheck(tmp + '.o', members, dol) if ok else (False, 'compile with the name map failed')
        if ok:
            ok, problem = strict_calls(tmp + '.o', members, dol, sym_at)
            if ok:
                return text, flags, why
            why = problem
    return None, None, why


def main():
    global CLOSE
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--max', type=int, default=1000, help='imports to attempt')
    ap.add_argument('--minutes', type=float, default=0, help='stop starting new imports after this many minutes')
    ap.add_argument('--dry', action='store_true', help='pre-checks only, change nothing')
    ap.add_argument('--only', help='run start address (hex)')
    ap.add_argument('--alts', type=int, default=3, help='reference sources tried per run')
    ap.add_argument('--retry', action='store_true', help='also runs that failed before')
    ap.add_argument('--close', action='store_true')
    ap.add_argument('--refresh', action='store_true')
    ap.add_argument('--any-license', action='store_true')
    a = ap.parse_args()
    CLOSE = a.close
    os.makedirs(OUT + '/work', exist_ok=True)
    funcs, dol = text_functions(), read_dol()
    sym_at = {f[0]: f[2] for f in funcs}
    with open(SYMS, encoding='utf-8') as f:      # labels inside functions are call targets too (register save helpers)
        for l in f:
            m = re.match(r'^(\S+) = \.text:0x([0-9A-Fa-f]+); // type:label', l)
            if m:
                sym_at.setdefault(int(m.group(2), 16), m.group(1))
                ALIASES.setdefault(int(m.group(2), 16), set()).add(m.group(1))
    if 'unit_overlay' not in read('configure.py') and not a.dry:
        sys.exit('configure.py does not load unit_overlay yet: see the docstring of unit_overlay.py')
    if a.refresh:
        return refresh(dol, sym_at)
    port = U.load_json(OUT + '/refmatch.json', None)
    if port is None:
        sys.exit('run refcorpus.py and refmatch.py first')
    if CLOSE:
        port = close_filter(port, funcs)
    tried_f = OUT + ('/refport_tried_close.json' if CLOSE else '/refport_tried.json')
    tried = {} if a.retry else U.load_json(tried_f, {})
    t0 = time.time()
    if not a.dry:
        built, msg = rebuild()      # the baseline must be what is in src/ now
        if not built:
            sys.exit('the project does not build: ' + msg)
    before, complete = report_funcs()
    runs = find_runs(port, funcs, before)
    log('refport: %d candidate runs, %d bytes could become exact' % (len(runs), sum(r[0] for r in runs)))
    done_rng, n_ok, n_try, unlicensed = [], 0, 0, 0
    for gain, start, end, items in runs:
        if n_try >= a.max or a.minutes and time.time() - t0 > a.minutes * 60:
            break
        if a.only and int(a.only, 16) != start or any(start < e and end > s for s, e in done_rng):
            continue
        if not a.any_license:
            lic = [it for it in items if license_of(it[2]['repo'])]
            if not lic:
                unlicensed += 1
                continue
            items = lic
        key = '%08X-%08X' % (start, end)
        sig = run_sig(start, end, items[:a.alts])
        prev = tried.get(key)
        if prev and prev.get('status') == 'failed' and prev.get('sig') == sig and not a.only:
            continue
        why = blocked(start, end, complete)
        if why:
            if a.only:
                log('  %s: %s' % (key, why))
            continue
        n_try += 1
        ok_any = False
        for gain_i, members, s in items[:a.alts]:
            label = '%s %s (%s, %s) %d functions %d B' % (key, s['src'], s['repo'], s['ver'], len(members), gain_i)
            text, flags, why = prepare(members, s, dol, sym_at)
            if text is None:
                log('  pre-check failed %s: %s' % (label, why))
                continue
            if a.dry:
                log('  pre-check ok     %s: %s' % (label, why))
                ok_any = True
                break
            bk = Backup()
            try:
                unit = apply_run(start, end, s, text, flags, funcs, bk)
                built, msg = rebuild()
                after = report_funcs()[0] if built else {}
                good, gmsg = gate(before, after, members) if built else (False, msg)
                if good:
                    st, why = refresh_unit(unit, overlay()['units'][unit], (start, end), funcs, after, dol, sym_at)
                    if st == 'fake':
                        good, gmsg = False, 'call check: ' + why
                    elif st == 'changed':
                        built, msg = rebuild()
                        after = report_funcs()[0] if built else {}
                        good, gmsg = gate(before, after, members) if built else (False, msg)
            except KeyboardInterrupt:
                bk.restore()
                rebuild()
                raise
            except Exception as ex:
                good, gmsg = False, 'error: %r' % ex
            if good:
                log('  IMPORTED %s -> %s: %s' % (label, unit, gmsg))
                before = after
                done_rng.append((start, end))
                n_ok += 1
                ok_any = True
                break
            log('  rejected %s: %s (restored)' % (label, gmsg))
            bk.restore()
            rebuild()
            before = report_funcs()[0]   # judge the next attempt against what is really built now
        if not a.dry:
            tried[key] = {'status': 'ok' if ok_any else 'failed', 'sig': sig}
            U.save_json(tried_f, tried, indent=0)
    log('refport: %d imported of %d tried%s' % (n_ok, n_try, '; %d runs skipped because no source has a recognised '
                                                'license (--any-license)' % unlicensed if unlicensed else ''))


if __name__ == '__main__':
    main()
