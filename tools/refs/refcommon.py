"""refcommon.py - shared code of the reference tools (refcorpus, refmatch, refport).

Folders (environment or defaults):
  DECOMP_REFS     reference root (default: build/scratch/refs inside the project)
                    history/<name>.git   bare mirrors of other decompilation projects (you clone them)
                    src/<name>/          exported HEAD          (refcorpus)
                    objs/<name>/         compiled objects       (refcorpus)
  config/<VER>/units_overlay.json        imported units (refport; read by unit_overlay.py from configure.py)
"""
import os
import re
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import unitlib as U              # noqa: E402,F401
from unitlib import REPO, VER    # noqa: E402,F401

REFS = os.path.abspath(os.environ.get('DECOMP_REFS') or os.path.join(REPO, 'build', 'scratch', 'refs')).replace('\\', '/')
HIST, SRC, OBJ = REFS + '/history', REFS + '/src', REFS + '/objs'
COMPILERS = os.path.abspath('build/compilers').replace('\\', '/')
OUT = 'build/scratch/refmatch'
EXE = '.exe' if os.name == 'nt' else ''


def compiler_cmd(ver):
    """argv prefix that runs MWCC `ver` (through sjiswrap, and wibo outside Windows), or None if it is not downloaded"""
    mwcc = '%s/%s/mwcceppc.exe' % (COMPILERS, ver)
    if not os.path.exists(mwcc):
        return None
    sjis = os.path.abspath('build/tools/sjiswrap.exe')
    cmd = ([sjis] if os.path.exists(sjis) else []) + [mwcc]
    if os.name != 'nt':
        wibo = os.path.abspath('build/tools/wibo')
        cmd = [wibo if os.path.exists(wibo) else 'wine'] + cmd
    return cmd


def split_flags(cflags):
    """dtk flag lists hold whole options like '-pragma "cats off"': one argv list, quoted parts kept as one argument"""
    out = []
    for f in (cflags if isinstance(cflags, list) else [cflags]):
        out += [x[1:-1] if x[:1] in '"\'' else x for x in re.findall(r'"[^"]*"|\'[^\']*\'|\S+', f)]
    res, i = [], 0
    while i < len(out):
        if out[i] in ('-maxerrors', '-msgstyle') and i + 1 < len(out):
            i += 2
        elif out[i] in ('-MMD', '-MD'):
            i += 1
        else:
            res.append(out[i])
            i += 1
    return res


# ---- ELF objects -------------------------------------------------------------------------------------------------------
def read_obj(path):
    """functions of a relocatable ELF32-BE object: [{name, code, off, relocs: {offset: (type, symbol)}}]"""
    with open(path, 'rb') as f:
        d = f.read()
    shoff, = struct.unpack_from('>I', d, 0x20)
    shentsize, shnum, shstrndx = struct.unpack_from('>HHH', d, 0x2E)
    secs = []
    for i in range(shnum):
        name, typ, flags, addr, off, size, link, info, align, entsize = struct.unpack_from('>IIIIIIIIII', d, shoff + i * shentsize)
        secs.append(dict(name=name, type=typ, off=off, size=size, link=link, info=info))
    strtab = secs[shstrndx]

    def sname(o, s):
        return d[s['off'] + o:d.index(b'\0', s['off'] + o)].decode('utf-8', 'replace')
    for s in secs:
        s['name'] = sname(s['name'], strtab)
    symsec = next((s for s in secs if s['type'] == 2), None)
    if symsec is None:
        return []
    symstr = secs[symsec['link']]
    syms = []
    for i in range(symsec['size'] // 16):
        nm, val, size, info, other, shndx = struct.unpack_from('>IIIBBH', d, symsec['off'] + i * 16)
        syms.append(dict(name=sname(nm, symstr), value=val, size=size, type=info & 0xF, shndx=shndx))
    funcs = []
    for idx, s in enumerate(secs):
        if s['name'] not in ('.text', '.init') or s['type'] != 1:
            continue
        text = d[s['off']:s['off'] + s['size']]
        relocs = {}
        for r in secs:
            if r['type'] == 4 and r['info'] == idx:
                for j in range(r['size'] // 12):
                    off, info, add = struct.unpack_from('>IIi', d, r['off'] + j * 12)
                    relocs[off] = (info & 0xFF, syms[info >> 8]['name'])
        for sym in syms:
            if sym['shndx'] == idx and sym['type'] == 2 and sym['size']:
                a = sym['value']
                funcs.append(dict(name=sym['name'], code=text[a:a + sym['size']], off=a,
                                  relocs={o - a: v for o, v in relocs.items() if a <= o < a + sym['size']}))
    return funcs


# ---- the game -----------------------------------------------------------------------------------------------------------
def dol_path():
    """path of the original executable: $DECOMP_DOL, else config.yml's object, else orig/<VER>/sys/main.dol"""
    p = os.environ.get('DECOMP_DOL')
    if not p:
        try:
            with open('config/%s/config.yml' % VER, encoding='utf-8') as f:
                t = f.read()
            base = re.search(r'^object_base:\s*(\S+)', t, re.M)
            m = re.search(r'^object:\s*(\S+)', t, re.M)
            if m:
                cands = [m.group(1)] + ([os.path.join(base.group(1), m.group(1))] if base else [])
                p = next((c for c in cands if os.path.exists(c)), None)
        except OSError:
            pass
    p = p or 'orig/%s/sys/main.dol' % VER
    if not os.path.exists(p):
        sys.exit('original executable not found (%s); set DECOMP_DOL' % p)
    return p


def read_dol(path=None):
    """reader(address, size) -> bytes of the original executable, or None outside its sections"""
    with open(path or dol_path(), 'rb') as f:
        d = f.read()
    offs = struct.unpack_from('>18I', d, 0)
    addrs = struct.unpack_from('>18I', d, 0x48)
    sizes = struct.unpack_from('>18I', d, 0x90)
    segs = [(a, o, s) for a, o, s in zip(addrs, offs, sizes) if s]

    def read(addr, n):
        for a, o, s in segs:
            if a <= addr and addr + n <= a + s:
                return d[o + addr - a:o + addr - a + n]
        return None
    return read


SYMS = 'config/%s/symbols.txt' % VER
_FN = re.compile(r'^(\S+) = \.(?:text|init):0x([0-9A-Fa-f]+); // .*?type:function .*?size:0x([0-9A-Fa-f]+)')
_FN2 = re.compile(r'^(\S+) = \.(?:text|init):0x([0-9A-Fa-f]+); // type:function size:0x([0-9A-Fa-f]+)')


def text_functions():
    """[(address, size, name)] of symbols.txt, sorted"""
    out = []
    with open(SYMS, encoding='utf-8') as f:
        for l in f:
            m = _FN2.match(l) or _FN.match(l)
            if m:
                out.append((int(m.group(2), 16), int(m.group(3), 16), m.group(1)))
    return sorted(out)


def game_funcs():
    read = read_dol()
    out = []
    for addr, size, name in text_functions():
        code = read(addr, size)
        if code:
            out.append(dict(name=name, addr=addr, code=code))
    return out


# ---- comparison ----------------------------------------------------------------------------------------------------------
def words(b):
    return list(struct.unpack('>%dI' % (len(b) // 4), b[:len(b) // 4 * 4]))


def masked(ws, mask_at):
    """relocated words are reduced to their opcode (and registers for load/store/addi forms)"""
    out = []
    for i, w in enumerate(ws):
        if i * 4 in mask_at or i * 4 + 2 in mask_at:      # @ha/@l relocations may point at the half word
            out.append((w >> 26) << 26 if w >> 26 in (16, 18) else w & 0xFFFF0000)
        else:
            out.append(w)
    return out


def generic_mask(ws):
    """without relocation info: branch targets and the immediates of addi/lis/load/store are dropped (candidates only)"""
    out = []
    for w in ws:
        op = w >> 26
        if op in (16, 18):
            out.append(op << 26)
        elif op in (14, 15, 24, 32, 34, 36, 38, 40, 42, 44, 46, 47, 48, 50, 52, 54):
            out.append(w & 0xFFFF0000)
        else:
            out.append(w)
    return tuple(out)
