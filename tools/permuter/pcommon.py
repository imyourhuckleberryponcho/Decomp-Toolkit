"""pcommon.py - shared helpers of the permuter drivers (project discovery, run folders, claims, process control).

Project discovery comes from ../dtkproj.py (DECOMP_REPO / DECOMP_VERSION). Two more settings:
  DECOMP_PERMUTER   folder of a decomp-permuter checkout (patched with ../../permuter/our-changes.diff is best,
                    upstream works too); every driver also takes --permuter DIR
  DECOMP_PERM_RUNS  where run folders are created (default: build/scratch/perm_runs inside the project);
                    every driver also takes --runs DIR
"""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from dtkproj import REPO, VER, EXE, tool   # noqa: E402  (also chdirs to the project root)
except SystemExit:
    if not any(a in ('-h', '--help') for a in sys.argv):
        raise
    REPO, VER, EXE = '.', 'VER', ''     # --help without a project

    def tool(name):
        return name

HERE = os.path.dirname(os.path.abspath(__file__)).replace('\\', '/')
C_WORDS = {"long", "int", "short", "char", "float", "double", "unsigned", "signed", "void", "const", "volatile",
           "static", "struct", "union", "enum", "return", "if", "else", "for", "while", "do", "switch", "case",
           "default", "break", "continue", "goto", "sizeof", "extern", "register", "auto", "inline", "typedef"}

# Pass weights written to settings.toml. CLEAN (default) switches off passes whose best results are code nobody would
# install (padding variables, `(0, x)` commas, empty `if (x && x) {}`, behaviour-changing statement moves/removals).
CLEAN_OFF = [
    'perm_dummy_comma_expr', 'perm_add_mask', 'perm_xor_zero', 'perm_mult_zero', 'perm_add_self_assignment',
    'perm_empty_stmt', 'perm_ins_block', 'perm_pad_var_decl', 'perm_cast_simple', 'perm_float_literal',
    'perm_alias_array', 'perm_add_sub', 'perm_long_chain_assignment',
    'perm_reorder_stmts', 'perm_remove_ast', 'perm_duplicate_assignment', 'perm_var_cond_block', 'perm_refer_to_var',
]
REGS = {'perm_reorder_decls': 50.0, 'perm_temp_for_expr': 30.0, 'perm_expand_expr': 20.0}


def weights_section(regs=False, dirty=False):
    w = dict(REGS) if regs else {}
    if not dirty:
        for p in CLEAN_OFF:
            w[p] = 0.0
    return ('\n[weight_overrides]\n' + ''.join('%s = %s\n' % kv for kv in w.items())) if w else ''


def safe(name):
    """folder-safe version of a function name (templates contain characters Windows forbids)"""
    return re.sub(r'[<>:"/\\|?*]', '_', name)


def add_common_args(ap):
    ap.add_argument('--permuter', default=os.environ.get('DECOMP_PERMUTER'),
                    help='decomp-permuter checkout (default: $DECOMP_PERMUTER)')
    ap.add_argument('--runs', default=os.environ.get('DECOMP_PERM_RUNS'),
                    help='folder for run folders (default: build/scratch/perm_runs)')


def permuter_dir(arg=None):
    p = arg or os.environ.get('DECOMP_PERMUTER')
    if not p or not os.path.exists(os.path.join(p, 'permuter.py')):
        sys.exit('decomp-permuter not found: pass --permuter DIR or set DECOMP_PERMUTER (a checkout containing permuter.py)')
    return os.path.abspath(p)


def runs_dir(arg=None):
    p = os.path.abspath(arg or os.environ.get('DECOMP_PERM_RUNS') or os.path.join(REPO, 'build', 'scratch', 'perm_runs'))
    os.makedirs(p, exist_ok=True)
    return p


def decode(raw):
    for enc in ('utf-8', 'shift_jis'):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    return raw.decode('utf-8', errors='replace')


def compiler_split(unit):
    """(prefix argv up to and including the compiler, flags argv, source path as in the build command, output dir)
    for a unit, from `ninja -t commands` (via dtkproj.unit_command)."""
    from dtkproj import unit_command
    argv = unit_command(unit)
    k_c = argv.index('-c')
    ci = max(i for i in range(k_c) if os.path.basename(argv[i]).lower().startswith('mwcc'))
    return argv[:ci + 1], argv[ci + 1:k_c], argv[k_c + 1], argv[argv.index('-o') + 1]


def unit_asm_function(unit, func):
    """the `.fn func ... .endfn func` text of one function from build/<VER>/asm/<unit>.s"""
    asm = open('build/%s/asm/%s.s' % (VER, unit), encoding='utf-8', errors='replace').read()
    m = re.search(r'^\.fn "?%s"?,.*?^\.endfn "?%s"?$' % (re.escape(func), re.escape(func)), asm, re.S | re.M)
    if not m:
        sys.exit('function %s not found in build/%s/asm/%s.s' % (func, VER, unit))
    return m.group(0)


def assemble_target(unit, func, out_dir):
    """target.s / target.o of one function (what the permuter scores against)"""
    text = unit_asm_function(unit, func)
    ts = os.path.join(out_dir, 'target.s')
    with open(ts, 'w', encoding='utf-8') as f:
        f.write('.include "macros.inc"\n.section .text, "ax"\n.balign 4\n' + text + '\n')
    r = subprocess.run([tool('as'), '-mgekko', '-I', 'include', '-I', 'build/%s/include' % VER, '--defsym', 'BUILD_VERSION=0',
                        '-o', os.path.join(out_dir, 'target.o'), ts], capture_output=True, text=True)
    if r.returncode:
        sys.exit('assembling the target failed:\n' + r.stderr[-1500:])


def run_env(run_dir):
    env = dict(os.environ)
    extra = [os.path.join(REPO, 'build', 'binutils')]
    for g in (r'C:\Program Files\Git\usr\bin', '/usr/bin'):
        if os.path.isdir(g):
            extra.append(g)
    env['PATH'] = os.pathsep.join(extra + [env.get('PATH', '')])
    tmp = os.path.join(run_dir, 'tmp')
    os.makedirs(tmp, exist_ok=True)
    env['TMP'] = env['TEMP'] = env['TMPDIR'] = tmp     # compiler temporaries writable from every child
    env['PYTHONUTF8'] = '1'
    env['PYTHONUNBUFFERED'] = '1'
    env.setdefault('PERM_SEEN_DIR', os.path.join(os.path.dirname(os.path.abspath(run_dir)), '_seen'))
    return env


def kill_tree(proc):
    if proc.poll() is not None:
        return
    if os.name == 'nt':
        subprocess.call(['taskkill', '/PID', str(proc.pid), '/T', '/F'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        import signal
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except OSError:
            proc.kill()
    try:
        proc.wait(10)
    except Exception:
        pass


def start_permuter(run_dir, permuter, jobs=4, extra=None, log_path=None):
    """Popen of permuter.py on a run folder (stack differences scored, stops on a zero by default)."""
    args = [sys.executable, 'permuter.py', run_dir, '-j%d' % jobs, '--stack-diffs'] + (extra if extra is not None else ['--stop-on-zero'])
    log = open(log_path or os.path.join(run_dir, 'permuter.log'), 'w', encoding='utf-8', errors='replace')
    kw = {} if os.name == 'nt' else {'start_new_session': True}
    return subprocess.Popen(args, cwd=permuter, env=run_env(run_dir), stdout=log, stderr=log, **kw)


def outputs(run_dir):
    """[(score, folder)] of the permuter's output-<score>-<n>/ folders, best first"""
    res = []
    for d in glob.glob(os.path.join(run_dir, 'output-*')):
        m = re.match(r'output-(\d+)-', os.path.basename(d))
        if m and os.path.exists(os.path.join(d, 'source.c')):
            res.append((int(m.group(1)), d))
    return sorted(res)


def base_score(run_dir, permuter, branch=False, timeout=900):
    """score of the unmodified base.c (permuter --debug); None when it does not compile/parse. Second value = text"""
    args = [sys.executable, 'permuter.py', run_dir, '--debug', '--stack-diffs'] + (['--no-ignore-branch-targets'] if branch else [])
    try:
        r = subprocess.run(args, cwd=permuter, env=run_env(run_dir), capture_output=True, text=True, timeout=timeout,
                           encoding='utf-8', errors='replace')
    except subprocess.TimeoutExpired:
        return None, 'timeout'
    out = re.sub(r'\x1b\[[0-9;]*m', '', r.stdout + r.stderr)
    m = re.search(r'base score = (\d+)', out)
    return (int(m.group(1)) if m else None), out


def run_dir_function(run_dir):
    m = re.search(r'func_name\s*=\s*"([^"]+)"', open(os.path.join(run_dir, 'settings.toml'), encoding='utf-8').read())
    return m.group(1)


# ---- claims: a claim file keeps parallel lanes (and other processes) off one function ----------------------------------
def claim(claim_dir, key, ttl=4 * 3600):
    """create the claim file of `key`; None when somebody holds a fresh claim. Pass the path to release()/touch()."""
    os.makedirs(claim_dir, exist_ok=True)
    p = os.path.join(claim_dir, safe(key))
    try:
        fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, ('%d\n' % os.getpid()).encode())
        os.close(fd)
        return p
    except FileExistsError:
        try:
            if time.time() - os.path.getmtime(p) < ttl:
                return None
            os.utime(p, None)       # stale claim of a dead process: take it over
            return p
        except OSError:
            return None


def touch(p):
    try:
        os.utime(p, None)
    except OSError:
        pass


def release(p):
    try:
        os.remove(p)
    except OSError:
        pass


def stop_requested(stopfile):
    return bool(stopfile) and os.path.exists(stopfile)


def read(path):
    return open(path, encoding='utf-8', errors='replace').read()


def write_json(path, obj):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, path)
