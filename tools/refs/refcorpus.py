"""refcorpus.py - compile other decompilation projects' source into a corpus of reference objects for refmatch.py.

usage (project root): python tools/refs/refcorpus.py [--repos a,b] [--all] [--fetch] [--no-own-compiler] [--list] [-j 8]

Why: shared code (SDK, C library, runtime, middleware) is usually already decompiled and matching somewhere. Comparing
COMPILED code finds it whatever the names or the file split.

Setup: clone bare mirrors of matching projects built with the same compiler family:
    git clone --bare https://github.com/<owner>/<repo>.git $DECOMP_REFS/history/<owner>_<repo>.git
For each mirror ($DECOMP_REFS/history/*.git, default build/scratch/refs/history):
  1. export HEAD to $DECOMP_REFS/src/<name> (only when HEAD moved)
  2. list its source files with their compiler + flags. dtk projects: configure.py is read STATICALLY (ast, data
     only); downloaded code is never executed. Other projects: describe them in $DECOMP_REFS/makefile_projects.json:
         {"<name>": {"versions": ["GC/1.2.5n"], "flags": ["-O4,p -proc gekko ... -I- -i include"]}}
     (every .c below src/ is compiled with every version + flag line)
  3. compile with the compilers of THIS project (build/compilers) -> $DECOMP_REFS/objs/<name>/<variant>/<src>.o +
     manifest.json. Variants: own = the project's compiler; game = this project's main compiler version ($DECOMP_MWCC,
     else the most used one in build.ninja), because a game may build the same library with another version.
Default: library code only (LIB below / library progress categories); --all compiles game code too.
Failures are normal (generated headers, precompiled headers, missing compilers); they are counted, never fatal.
"""
import argparse
import ast
import glob
import io
import os
import re
import subprocess
import sys
import tarfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from refcommon import HIST, OBJ, REFS, SRC, U, compiler_cmd, split_flags   # noqa: E402

STAMP = 'v2'
LIB = re.compile(r'(^|/)(dolphin|sdk|os|gx|dvd|vi|pad|card|ai|ar|arq|dsp|exi|si|mtx|thp|gd|db|base|dbgcomm|'
                 r'OdemuExi2|odenotstub|amcstubs|amcnotstub|amcExi2|MSL|MSL_C|Runtime|Runtime\.PPCEABI\.H|'
                 r'PowerPC_EABI_Support|TRK|MetroTRK|TRK_MINNOW_DOLPHIN|musyx|sysdolphin|baselib|hsd|'
                 r'libc|debugger|axfx|ax|mix|sp|syn|seq|synth|libs|lib|JSystem|Dolphin_SDK|dolsdk|NdevExi2A|'
                 r'odemustubs|perf|G2D|support|hw)(/|$)', re.I)
LIB_CATS = {'sdk', 'lib', 'libs', 'library', 'dolphin', 'runtime', 'msl', 'trk', 'musyx', 'sysdolphin', 'hsd',
            'third_party', 'thirdparty', 'jsystem'}


def own_version():
    """the compiler version this project uses most"""
    v = os.environ.get('DECOMP_MWCC')
    if not v and os.path.exists('build.ninja'):
        with open('build.ninja', encoding='utf-8') as f:
            c = Counter(re.findall(r'^\s+mw_version = (\S+)', f.read(), re.M))
        v = c.most_common(1)[0][0].replace(chr(92), '/') if c else None
    return v


# ---- static reading of dtk configure.py (the downloaded repos' code is never executed) -----------------------------------

UNK = object()


class _Obj:
    def __init__(self, status, name, kw):
        self.status, self.name, self.kw = status, name, kw


class StaticEval:
    """Evaluates only data: literals, lists/dicts/tuples, *spread, +, names bound by earlier module-level assignments,
    list.append/extend, config.<attr> = ..., Object(...), and calls to module-level functions whose body is
    assignments + `return <expr>` (DolphinLib, Rel, ...). Anything else is UNK and dropped from lists."""

    def __init__(self, tree):
        self.env = {'Matching': True, 'NonMatching': False, 'Equivalent': False, 'True': True, 'False': False,
                    'None': None}
        self.funcs = {}
        self.config = {}
        self.tree = tree

    def run(self):
        for st in self.tree.body:
            self.stmt(st, self.env, top=True)

    def stmt(self, st, env, top=False):
        if isinstance(st, ast.FunctionDef):
            if top:
                self.funcs[st.name] = st
        elif isinstance(st, (ast.Assign, ast.AnnAssign)):
            targets = st.targets if isinstance(st, ast.Assign) else [st.target]
            if st.value is None:
                return
            v = self.ev(st.value, env)
            for t in targets:
                if isinstance(t, ast.Name):
                    env[t.id] = v
                elif isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id == 'config':
                    self.config[t.attr] = v
        elif isinstance(st, ast.AugAssign) and isinstance(st.target, ast.Name) and isinstance(st.op, ast.Add):
            cur, v = env.get(st.target.id, UNK), self.ev(st.value, env)
            if isinstance(cur, list) and isinstance(v, list):
                env[st.target.id] = cur + v
        elif isinstance(st, ast.Expr) and isinstance(st.value, ast.Call):
            f = st.value.func
            if isinstance(f, ast.Attribute) and f.attr in ('append', 'extend') and isinstance(f.value, ast.Name):
                cur = env.get(f.value.id)
                if isinstance(cur, list) and st.value.args:
                    v = self.ev(st.value.args[0], env)
                    if v is not UNK:
                        if f.attr == 'append':
                            cur.append(v)
                        elif isinstance(v, list):
                            cur.extend(v)

    def ev(self, n, env):
        if isinstance(n, ast.Constant):
            return n.value
        if isinstance(n, ast.Name):
            return env.get(n.id, self.env.get(n.id, UNK))
        if isinstance(n, (ast.List, ast.Tuple)):
            out = []
            for e in n.elts:
                if isinstance(e, ast.Starred):
                    v = self.ev(e.value, env)
                    if isinstance(v, list):
                        out.extend(v)
                    continue
                v = self.ev(e, env)
                if v is not UNK:
                    out.append(v)
            return out
        if isinstance(n, ast.Dict):
            d = {}
            for k, v in zip(n.keys, n.values):
                if k is None:
                    sub = self.ev(v, env)
                    if isinstance(sub, dict):
                        d.update(sub)
                    continue
                kk = self.ev(k, env)
                if isinstance(kk, str):
                    d[kk] = self.ev(v, env)
            return d
        if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Add):
            a, b = self.ev(n.left, env), self.ev(n.right, env)
            if isinstance(a, list) and isinstance(b, list):
                return a + b
            if isinstance(a, str) and isinstance(b, str):
                return a + b
            return UNK
        if isinstance(n, ast.JoinedStr):
            parts = []
            for v in n.values:
                x = self.ev(v.value, env) if isinstance(v, ast.FormattedValue) else v.value
                if not isinstance(x, (str, int, float)):
                    return UNK
                parts.append(str(x))
            return ''.join(parts)
        if isinstance(n, (ast.ListComp, ast.GeneratorExp)) and len(n.generators) == 1 and not n.generators[0].ifs \
                and isinstance(n.generators[0].target, ast.Name):   # [f"-i {s}" for s in includes]
            xs = self.ev(n.generators[0].iter, env)
            if not isinstance(xs, list):
                return UNK
            vs = [self.ev(n.elt, dict(env, **{n.generators[0].target.id: x})) for x in xs]
            return [v for v in vs if v is not UNK]
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == 'map' and len(n.args) == 2 \
                and isinstance(n.args[0], ast.Lambda) and len(n.args[0].args.args) == 1:
            # *map(lambda s: f"-i {s}", includes)
            xs = self.ev(n.args[1], env)
            if not isinstance(xs, list):
                return UNK
            arg = n.args[0].args.args[0].arg
            vs = [self.ev(n.args[0].body, dict(env, **{arg: x})) for x in xs]
            return [v for v in vs if v is not UNK]
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
            name = n.func.id
            args = [self.ev(a, env) for a in n.args if not isinstance(a, ast.Starred)]
            kw = {k.arg: self.ev(k.value, env) for k in n.keywords if k.arg}
            if name == 'Object':
                return _Obj(args[0] if args else UNK, args[1] if len(args) > 1 else UNK, kw)
            if name in ('MatchingFor', 'EquivalentFor'):
                return name == 'MatchingFor'
            fd = self.funcs.get(name)
            if fd is not None:
                local = dict(env)
                params = [p.arg for p in fd.args.args]
                defaults = fd.args.defaults
                for p, d in zip(params[len(params) - len(defaults):], defaults):
                    local[p] = self.ev(d, env)
                for p, v in zip(params, args):
                    local[p] = v
                local.update({k: v for k, v in kw.items() if k in params})
                for st in fd.body:
                    if isinstance(st, ast.Return):
                        return self.ev(st.value, local) if st.value is not None else None
                    self.stmt(st, local)
            return UNK
        return UNK


def static_config(text):
    se = StaticEval(ast.parse(text))
    se.run()
    libs = se.config.get('libs')
    if not isinstance(libs, list):
        return []
    src_dir = se.config.get('src_dir') if isinstance(se.config.get('src_dir'), str) else 'src'
    lver = se.config.get('linker_version') if isinstance(se.config.get('linker_version'), str) else None
    out = []
    for lib in libs:
        if not isinstance(lib, dict) or not isinstance(lib.get('objects'), list):
            continue
        for o in lib['objects']:
            if not isinstance(o, _Obj) or not isinstance(o.name, str):
                continue
            kw = o.kw
            ver = kw.get('mw_version') if isinstance(kw.get('mw_version'), str) else lib.get('mw_version')
            if not isinstance(ver, str):
                ver = lver
            base = kw.get('cflags') if isinstance(kw.get('cflags'), list) else lib.get('cflags')
            cfl = [x for x in (base if isinstance(base, list) else []) if isinstance(x, str)]
            cfl += [x for x in (kw.get('extra_cflags') if isinstance(kw.get('extra_cflags'), list) else [])
                    if isinstance(x, str)]
            if not any(re.match(r'^-O', x) for x in cfl):   # added per object in a loop: assume the usual pair
                cfl += ['-O4,p', '-inline auto']
            src = kw.get('source') if isinstance(kw.get('source'), str) else o.name
            sp = lib.get('strip_prefix')
            if isinstance(sp, str) and src.startswith(sp):
                src = src[len(sp):]
            sd = kw.get('src_dir') if isinstance(kw.get('src_dir'), str) else lib.get('src_dir')
            sd = sd if isinstance(sd, str) else src_dir
            cat = kw.get('progress_category') if isinstance(kw.get('progress_category'), str) else lib.get('progress_category')
            out.append({'src': (sd.rstrip('/') + '/' + src).replace('\\', '/'), 'ver': ver, 'cflags': cfl,
                        'done': o.status is True, 'cat': cat if isinstance(cat, str) else '',
                        'lib': lib.get('lib') if isinstance(lib.get('lib'), str) else ''})
    return out


def git(gd, *args):
    return subprocess.run(['git', '--git-dir', gd] + list(args), capture_output=True).stdout


def export(name):
    gd = os.path.join(HIST, name + '.git')
    head = git(gd, 'rev-parse', 'HEAD').decode().strip()
    dst = os.path.join(SRC, name)
    stamp = os.path.join(dst, '.refhead')
    if os.path.exists(stamp):
        with open(stamp) as f:
            if f.read().strip() == head:
                return dst, head
    data = git(gd, 'archive', '--format=tar', 'HEAD')
    os.makedirs(dst, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data)) as t:
        t.extractall(dst, filter='data')
    with open(stamp, 'w') as f:
        f.write(head)
    return dst, head


def file_list(name, root, makefiles):
    out = []
    if name in makefiles:
        m = makefiles[name]
        for p in glob.glob(os.path.join(root, 'src', '**', '*.c'), recursive=True):
            rel = os.path.relpath(p, root).replace('\\', '/')
            for v in m['versions']:
                for k, fl in enumerate(m['flags']):
                    out.append({'src': rel, 'ver': v, 'cflags': [fl], 'done': True, 'cat': 'lib',
                                'variant': 'mk_%s_%d' % (v.replace('/', '_'), k)})
        return out
    cfg = os.path.join(root, 'configure.py')
    if not os.path.exists(cfg):
        return []
    try:
        with open(cfg, encoding='utf-8', errors='replace') as f:
            entries = static_config(f.read())
    except Exception as ex:   # never fatal: an unreadable config just skips the project
        print('  %s: configure.py not readable statically: %s' % (name, ex))
        return []
    return [dict(e, variant='own') for e in entries if e['ver'] and re.search(r'\.(c|cpp|cp)$', e['src'])]


def is_lib(e):
    return bool(LIB.search(e['src'])) or e.get('cat', '').lower() in LIB_CATS or bool(LIB.search(e.get('lib', '') or ''))


def compile_one(root, outdir, e):
    cmd = compiler_cmd(e['ver'])
    if not cmd:
        return dict(e, ok=False, err='compiler %s not in build/compilers' % e['ver'])
    obj = os.path.join(outdir, e['variant'], re.sub(r'\.(c|cpp|cp)$', '.o', e['src']))
    cmd = cmd + split_flags(e['cflags']) + ['-maxerrors', '1', '-c', e['src'], '-o', obj]
    stamp = obj + '.cmd'
    if os.path.exists(obj) and os.path.exists(stamp) and \
            os.path.getmtime(obj) >= os.path.getmtime(os.path.join(root, e['src'])):
        with open(stamp, encoding='utf-8') as f:
            if f.read() == ' '.join(cmd):
                return dict(e, ok=True, obj=obj, err='')
    os.makedirs(os.path.dirname(obj), exist_ok=True)
    if os.path.exists(obj):
        os.remove(obj)
    try:
        r = subprocess.run(cmd, cwd=root, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=300)
        ok = r.returncode == 0 and os.path.exists(obj)
        # MWCC prints the source line, `Error:` and then the message
        text = re.sub(r'^#[ \t]?', '', r.stdout + r.stderr, flags=re.M)
        m = re.search(r'Error:[ \t^]*\n\s*([^\n]+)', text)
        err = '' if ok else (m.group(1).strip() if m else next((l.strip() for l in text.splitlines() if l.strip()), '?'))[:200]
    except (subprocess.TimeoutExpired, OSError) as ex:
        ok, err = False, type(ex).__name__
    if ok:
        with open(stamp, 'w', encoding='utf-8') as f:
            f.write(' '.join(cmd))
    return dict(e, ok=ok, obj=obj if ok else None, err=err)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--repos', help='comma-separated parts of mirror names (default: all)')
    ap.add_argument('--all', action='store_true', help='game code too, not only library code')
    ap.add_argument('--fetch', action='store_true', help='git fetch every mirror first')
    ap.add_argument('--no-own-compiler', action='store_true', help="do not also compile with this project's compiler")
    ap.add_argument('-j', type=int, default=8)
    ap.add_argument('--list', action='store_true', help='only list what would be compiled')
    a = ap.parse_args()
    names = sorted(os.path.basename(p)[:-4] for p in glob.glob(os.path.join(HIST, '*.git')))
    if a.repos:
        names = [n for n in names if any(w in n for w in a.repos.split(','))]
    if not names:
        sys.exit('no mirrors in %s (see the docstring: git clone --bare ...)' % HIST)
    makefiles = U.load_json(os.path.join(REFS, 'makefile_projects.json'), {})
    over = own_version()
    grand = [0, 0]
    for name in names:
        if a.fetch:
            git(os.path.join(HIST, name + '.git'), 'fetch', '-q', 'origin', '+refs/heads/*:refs/heads/*')
        root, head = export(name)
        files = [e for e in file_list(name, root, makefiles) if a.all or is_lib(e)]
        jobs = []
        for e in files:
            if not os.path.exists(os.path.join(root, e['src'])):
                continue
            jobs.append(e)
            if over and not a.no_own_compiler and e['ver'] != over and e['variant'] == 'own':
                jobs.append(dict(e, ver=over, variant='game'))
        outdir = os.path.join(OBJ, name)
        if a.list:
            print('%-45s %5d compiles, versions %s, e.g. %s' % (
                name, len(jobs), sorted({str(e['ver']) for e in jobs}), jobs[0]['src'] if jobs else '-'), flush=True)
            continue
        with ThreadPoolExecutor(a.j) as ex:
            res = list(ex.map(lambda e: compile_one(root, outdir, e), jobs))
        U.save_json(os.path.join(outdir, 'manifest.json'), {'head': head, 'stamp': STAMP, 'files': res}, indent=0)
        ok = sum(1 for r in res if r['ok'])
        grand[0] += ok
        grand[1] += len(res)
        errs = Counter(r['err'][:60] for r in res if not r['ok'])
        print('%-45s %5d/%5d compiled  %s' % (name, ok, len(res), '; '.join('%dx %s' % (n, e) for e, n in errs.most_common(2))), flush=True)
    if not a.list:
        print('refcorpus: %d/%d objects compiled -> %s' % (grand[0], grand[1], OBJ))


if __name__ == '__main__':
    main()
