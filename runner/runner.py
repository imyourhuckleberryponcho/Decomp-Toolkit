"""runner.py - a configurable long-run ("overnight") runner for matching-decompilation search tools.

usage (project root, or set DECOMP_REPO):
  python runner/runner.py run     --config runner/default.toml [--hours 8] [--lanes N] [--only SUBSTR] [--limit N]
  python runner/runner.py dry-run --config runner/default.toml          list what would run, run nothing
  python runner/runner.py status  --config runner/default.toml          progress of the current/last run
  python runner/runner.py stop    --config runner/default.toml          ask a running runner to stop cleanly
  python runner/runner.py report  --config runner/default.toml          (re)write the morning package from the logs

What it does: picks near-miss functions from build/<VER>/report.json, runs every configured STAGE (an external tool,
given as a command template) on each of them, several in parallel, verifies what the tools report, and writes a
package folder for a person to review. It NEVER edits the project's source files: results are candidate files in
<output>/package_<time>/ (apply one with runner/apply.py, which re-verifies). If a tool edits the source anyway, the
file is restored and a warning logged.

Tool contract: a stage command prints, as its LAST stdout line,
    RESULT <unit> <func> <base> <best> <path-or-NONE>
score = number of differing lines (or permuter penalty), 0 = exact; lower is better. The path is a candidate source file
(stage kind "unit-source": a complete replacement for the unit's source file, verified here with fcheck) or a permuter
output (kind "permuter": collected at the end with tools/permuter/permcollect.py into package/permuter_review/).

Files in <output> (default build/scratch/runner):
  runner.log      every event, flushed per line (survives crashes)       results.jsonl  one JSON line per finished task
  state.json      fingerprints / parking / what was tried (resume)       STOP           created by `stop`
  logs/<stage>/   stdout+stderr of every task                            package_<time>/ REPORT.md, ready/, improved/, ...
Resume = run the same command again: finished (stage, function, source fingerprint, budget) work is skipped.
Config: TOML or JSON; see runner/default.toml. It is re-read between tasks (hot reload): a broken edit is logged once
and ignored. Placeholders in commands: {py} {repo} {ver} {tools} {unit} {func} {src} {t} {j} {tag} {permuter} {runs}.
"""
import argparse
import ctypes
import datetime
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__)).replace('\\', '/')
TOOLS = os.path.join(os.path.dirname(HERE), 'tools').replace('\\', '/')
sys.path.insert(0, TOOLS)
try:
    from dtkproj import REPO, VER, EXE, tool   # noqa: E402  (chdirs to the project root)
except SystemExit:
    if not any(a in ('-h', '--help') for a in sys.argv):
        raise
    REPO, VER, EXE = '.', 'VER', ''

    def tool(name):
        return name

PY = sys.executable.replace('\\', '/')
LOCK = threading.Lock()


class Config:
    """the parsed config file, re-read when its mtime changes"""

    def __init__(self, path):
        self.path = os.path.abspath(path)
        self.mtime = 0
        self.data = self._load()
        self.mtime = os.path.getmtime(self.path)
        self.bad_mtime = None

    def _load(self):
        text = open(self.path, 'rb').read()
        if self.path.lower().endswith('.json'):
            d = json.loads(text.decode('utf-8'))
        else:
            import tomllib
            d = tomllib.loads(text.decode('utf-8'))
        if not isinstance(d.get('stage'), list) or not d['stage']:
            raise ValueError('config needs at least one [[stage]]')
        for s in d['stage']:
            for k in ('name', 'command', 'seconds'):
                if k not in s:
                    raise ValueError('stage %r lacks %r' % (s.get('name'), k))
        return d

    def reload_if_changed(self, log):
        try:
            m = os.path.getmtime(self.path)
        except OSError:
            return False
        if m == self.mtime or m == self.bad_mtime:
            return False
        try:
            self.data = self._load()
            self.mtime = m
            log('config reloaded')
            return True
        except Exception as e:     # keep the working config
            self.bad_mtime = m
            log('config change REJECTED (%s) - keeping the previous one' % e)
            return False

    def get(self, section, key, default=None):
        return self.data.get(section, {}).get(key, default)


class Logger:
    def __init__(self, path):
        self.f = open(path, 'a', encoding='utf-8', buffering=1)

    def __call__(self, msg):
        line = time.strftime('%Y-%m-%d %H:%M:%S ') + msg
        with LOCK:
            self.f.write(line + '\n')
            self.f.flush()
            try:
                os.fsync(self.f.fileno())
            except OSError:
                pass
            print(line, flush=True)


def write_json(path, obj):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, path)


def sha(text):
    return hashlib.sha1(text.encode('utf-8', 'replace')).hexdigest()[:12]


# ---------------------------------------------------------------- selection
def src_path_of(unit):
    for ext in ('.cpp', '.c', '.cp'):
        p = 'src/%s%s' % (unit, ext)
        if os.path.exists(p):
            return p
    return 'src/%s.cpp' % unit


def demangle_name(sym):
    if '__' not in sym[1:]:
        return sym
    r = subprocess.run([tool('dtk'), 'demangle', sym], capture_output=True, text=True)
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip().split('(')[0]      # `name` or `Class::method`
    return sym


def function_text(src_text, sym):
    """source text of a function (for the fingerprint); None when it cannot be found"""
    name = demangle_name(sym)
    pat = r'^[^\n;{}#]*\b%s\s*\([^;{]*\)\s*(?:const\s*)?\{' % re.escape(name)
    for m in re.finditer(pat, src_text, re.M):
        if re.match(r'\s*(if|while|for|switch|return|else)\b', m.group(0)):
            continue
        depth = 0
        for k in range(m.end() - 1, len(src_text)):
            if src_text[k] == '{':
                depth += 1
            elif src_text[k] == '}':
                depth -= 1
                if depth == 0:
                    return src_text[m.start():k + 1]
    return None


def select(cfg, only=None, limit=None):
    """[(unit, func, percent, size)] from report.json"""
    sel = cfg.data.get('select', {})
    lo, hi = float(sel.get('min', 80)), float(sel.get('max', 99.99))
    max_size = int(sel.get('max_size', 1200))
    skip = set(sel.get('skip', []))
    skip_file = sel.get('skip_file')
    if skip_file and os.path.exists(skip_file):
        skip |= {l.split('#')[0].strip() for l in open(skip_file, encoding='utf-8') if l.split('#')[0].strip()}
    units_only = sel.get('units')
    rep = json.load(open('build/%s/report.json' % VER, encoding='utf-8'))
    out = []
    for u in rep['units']:
        md = u.get('metadata', {})
        sp = md.get('source_path')
        if md.get('complete') or md.get('auto_generated') or not sp:
            continue
        unit = re.sub(r'\.(cpp|c|cp)$', '', sp.replace('\\', '/').removeprefix('src/'))
        if units_only and not any(re.search(x, unit) for x in units_only):
            continue
        if unit + '/' in skip or unit in skip:
            continue
        for f in u.get('functions', []):
            pct = float(f.get('fuzzy_match_percent', 0.0))
            size = int(f.get('size', 0))
            if not (lo <= pct <= hi) or size > max_size or f['name'] in skip or '%s:%s' % (unit, f['name']) in skip:
                continue
            if only and only not in f['name'] and only not in unit:
                continue
            out.append((unit, f['name'], pct, size))
    if sel.get('order', 'closest') == 'size_desc':
        out.sort(key=lambda r: -r[3])
    else:
        out.sort(key=lambda r: -r[2])
    top = limit or sel.get('top')
    return out[:int(top)] if top else out


# ---------------------------------------------------------------- cpu
_last = [None]


def cpu_percent():
    """whole-machine CPU use in percent since the previous call (None if unknown)"""
    try:
        if os.name == 'nt':
            class FT(ctypes.Structure):
                _fields_ = [('lo', ctypes.c_uint32), ('hi', ctypes.c_uint32)]
            i, k, u = FT(), FT(), FT()
            ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(i), ctypes.byref(k), ctypes.byref(u))
            v = [(x.hi << 32) | x.lo for x in (i, k, u)]
            idle, total = v[0], v[1] + v[2]
        else:
            a = [int(x) for x in open('/proc/stat').readline().split()[1:]]
            idle, total = a[3] + a[4], sum(a)
    except Exception:
        return None
    prev, _last[0] = _last[0], (idle, total)
    if not prev or total == prev[1]:
        return None
    return 100.0 * (1 - (idle - prev[0]) / (total - prev[1]))


# ---------------------------------------------------------------- the run
class Run:
    def __init__(self, cfg, args):
        self.cfg, self.args = cfg, args
        self.out = os.path.abspath(cfg.data.get('output', 'build/scratch/runner'))
        for sub in ('logs', ):
            os.makedirs(os.path.join(self.out, sub), exist_ok=True)
        self.log = Logger(os.path.join(self.out, 'runner.log'))
        self.state_path = os.path.join(self.out, 'state.json')
        try:
            self.state = json.load(open(self.state_path, encoding='utf-8'))
        except (OSError, ValueError):
            self.state = {}
        self.state.setdefault('funcs', {})
        self.stop_file = os.path.join(self.out, 'STOP')
        self.results_path = os.path.join(self.out, 'results.jsonl')
        self.t0 = time.time()
        self.deadline = self.t0 + float(args.hours or cfg.data.get('hours', 8)) * 3600
        self.counter = 0
        self.cpu = []
        self.prefix = cfg.data.get('scratch_prefix', 'runner_')
        self.stats = {}

    def stopping(self):
        return os.path.exists(self.stop_file) or time.time() >= self.deadline

    def save_state(self):
        with LOCK:
            write_json(self.state_path, self.state)

    def fp(self, unit, sym):
        p = src_path_of(unit)
        try:
            text = open(p, encoding='utf-8', errors='replace').read()
        except OSError:
            return 'nosrc'
        t = function_text(text, sym)
        return sha(t) if t else 'file-' + sha(text)

    def command(self, stage, unit, sym, seconds, jobs, tag):
        values = dict(py=PY, repo=REPO, ver=VER, tools=TOOLS, unit=unit, func=sym, src=src_path_of(unit), t=int(seconds),
                      j=jobs, tag=tag, permuter=self.cfg.get('paths', 'permuter', os.environ.get('DECOMP_PERMUTER', '')),
                      runs=self.cfg.get('paths', 'runs', os.path.join(self.out, 'perm_runs').replace('\\', '/')))
        values = {k: ('"%s"' % v if isinstance(v, str) and ' ' in v else v) for k, v in values.items()}
        return shlex.split(stage['command'].format(**values), posix=True)

    # ---- one task
    def run_task(self, stage, unit, sym, pct, pass_no):
        seconds = float(stage['seconds']) * float(self.cfg.get('run', 'pass_growth', 1.0)) ** (pass_no - 1)
        jobs = int(stage.get('threads', 4))
        with LOCK:
            self.counter += 1
            tag = '%s%s_%d' % (self.prefix, re.sub(r'\W', '', stage['name'])[:12], self.counter)
        argv = self.command(stage, unit, sym, seconds, jobs, tag)
        src = src_path_of(unit)
        before = open(src, 'rb').read() if os.path.exists(src) else None
        logp = os.path.join(self.out, 'logs', stage['name'])
        os.makedirs(logp, exist_ok=True)
        logf = os.path.join(logp, re.sub(r'[<>:"/\\|?*]', '_', sym) + '.log')
        t0 = time.time()
        res = dict(stage=stage['name'], unit=unit, func=sym, base=None, best=None, path=None, status='error', pass_no=pass_no)
        env = dict(os.environ, PYTHONUTF8='1', PYTHONUNBUFFERED='1')
        try:
            p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, cwd=REPO)
            hard = seconds * float(stage.get('grace', 1.5)) + 90
            try:
                out, _ = p.communicate(timeout=hard)
            except subprocess.TimeoutExpired:
                subprocess.call(['taskkill', '/PID', str(p.pid), '/T', '/F'], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL) if os.name == 'nt' else p.kill()
                out, _ = p.communicate()
                res['status'] = 'timeout'
            text = out.decode('utf-8', 'replace')
            open(logf, 'w', encoding='utf-8').write(text[-200000:])
            lines = [l for l in text.splitlines() if l.startswith('RESULT ')]
            if lines:
                parts = lines[-1].split()
                if len(parts) >= 6:
                    res.update(base=None if parts[3] == 'NONE' else int(float(parts[3])),
                               best=None if parts[4] == 'NONE' else int(float(parts[4])),
                               path=None if parts[5] == 'NONE' else ' '.join(parts[5:]))
                    res['status'] = 'ok'
            elif res['status'] != 'timeout':
                res['status'] = 'no-result'
                res['note'] = text.strip()[-300:]
        except Exception as e:
            res['note'] = 'launch failed: %s' % e
        res['seconds'] = round(time.time() - t0, 1)
        after = open(src, 'rb').read() if os.path.exists(src) else None
        if before is not None and after != before:
            open(src, 'wb').write(before)
            self.log('WARNING: stage %s changed %s - restored it. Tools must write candidates elsewhere.' % (stage['name'], src))
            res['note'] = (res.get('note', '') + ' tool edited the source (restored)').strip()
        if res['status'] == 'ok':
            res = self.assess(stage, res)
        return res

    def assess(self, stage, res):
        """improved? exact? verify unit-source candidates with fcheck (copy kept under package later)"""
        base, best = res['base'], res['best']
        if best is None or (base is not None and best >= base and best != 0):
            res['outcome'] = 'none'
            return res
        res['outcome'] = 'exact' if best == 0 else 'improved'
        if stage.get('kind', 'unit-source') == 'unit-source' and res['path']:
            ok, info = self.verify(res)
            res['verified'] = ok
            res['verify_info'] = info
            if not ok:
                res['outcome'] = 'rejected'
        return res

    def verify(self, res):
        """compile the candidate with the unit's real flags (tools/fcheck.py --src); the function must be exact for
        'exact', better for 'improved', and no other function of the unit may get worse"""
        cand = res['path']
        if not os.path.exists(cand):
            return False, 'candidate file missing'
        unit, sym = res['unit'], res['func']
        sdir = os.path.join(REPO, 'build', 'scratch', self.prefix + 'verify_' + re.sub(r'\W', '', res['stage']))
        os.makedirs(sdir, exist_ok=True)
        tmp = os.path.join(sdir, os.path.basename(src_path_of(unit)))
        shutil.copyfile(cand, tmp)
        try:
            def rows(src, tag):
                argv = [PY, TOOLS + '/fcheck.py', unit, '--tag', self.prefix + tag]
                if src:
                    argv += ['--src', src, '--extra', '-i ' + os.path.dirname(os.path.abspath(src_path_of(unit))).replace('\\', '/')]
                r = subprocess.run(argv, capture_output=True, text=True, encoding='utf-8', errors='replace', cwd=REPO)
                text = r.stdout + r.stderr
                if 'COMPILE FAILED' in text or r.returncode:
                    return None, text
                return {m.group(3): float(m.group(1)) for l in text.splitlines()
                        for m in [re.match(r'^\s*(\d+\.\d+)%\s+(\d+)\s+(\S+)', l)] if m}, text
            base_rows, _ = rows(None, 'vbase')
            new_rows, text = rows(tmp, 'vnew')
        finally:
            shutil.rmtree(sdir, ignore_errors=True)
        if new_rows is None or base_rows is None:
            return False, 'does not compile'
        before, after = base_rows.get(sym, 100.0), new_rows.get(sym, 100.0)
        worse = [n for n, p in new_rows.items() if n != sym and p < base_rows.get(n, 100.0)]
        if worse:
            return False, 'other functions got worse: ' + ', '.join(worse[:4])
        if res['outcome'] == 'exact' and after < 100.0:
            return False, 'not exact (%.2f%%)' % after
        if after <= before:
            return False, 'not better (%.2f%% -> %.2f%%)' % (before, after)
        layout = [l for l in text.splitlines() if l.startswith(('LAYOUT DIFFERS', 'CONST DIFF', 'CALLS DIFF', 'DATA DIFF'))]
        return True, '%.2f%% -> %.2f%%%s' % (before, after, ('; ' + '; '.join(layout)) if layout else '')

    # ---- scheduling
    def pass_over(self, items, pass_no):
        """stage-major pass: every stage over every function; returns True if anything was started"""
        started = False
        for stage_i in range(len(self.cfg.data['stage'])):
            self.cfg.reload_if_changed(self.log)
            stages = self.cfg.data['stage']
            if stage_i >= len(stages) or self.stopping():
                break
            stage = stages[stage_i]
            if not stage.get('enabled', True):
                continue
            seconds = float(stage['seconds']) * float(self.cfg.get('run', 'pass_growth', 1.0)) ** (pass_no - 1)
            queue = []
            for unit, sym, pct, size in items:
                st = self.state['funcs'].setdefault('%s:%s' % (unit, sym), {})
                fp = self.fp(unit, sym)
                if st.get('fp') != fp:             # the function changed: forget parking and history
                    st.clear()
                    st['fp'] = fp
                if st.get('parked'):
                    continue
                done = st.setdefault('stages', {}).get(stage['name'])
                if done and done.get('seconds', 0) >= seconds - 1:
                    continue
                queue.append((unit, sym, pct))
            if not queue:
                continue
            self.log('stage %s (pass %d): %d functions, %d s each' % (stage['name'], pass_no, len(queue), seconds))
            started = True
            self.run_queue(stage, queue, pass_no, seconds)
        return started

    def run_queue(self, stage, queue, pass_no, seconds):
        lanes = int(self.args.lanes or self.cfg.get('run', 'lanes', 2))
        boost = self.cfg.data.get('boost', {})
        qlock = threading.Lock()
        active = [0]
        stage_t0 = time.time()

        def worker(is_boost):
            while True:
                if self.stopping():
                    return
                if is_boost and not self.boost_wanted():
                    return
                with qlock:
                    if not queue:
                        return
                    unit, sym, pct = queue.pop(0)
                self.cfg.reload_if_changed(self.log)
                st = self.state['funcs']['%s:%s' % (unit, sym)]
                self.log('start %s %s:%s (%.2f%%)%s' % (stage['name'], unit, sym, pct, ' [boost]' if is_boost else ''))
                try:
                    res = self.run_task(stage, unit, sym, pct, pass_no)
                except Exception:
                    res = dict(stage=stage['name'], unit=unit, func=sym, status='error', note=traceback.format_exc()[-300:],
                               seconds=0, outcome='none')
                with LOCK:
                    open(self.results_path, 'a', encoding='utf-8').write(json.dumps(res) + '\n')
                    s = self.stats.setdefault(stage['name'], dict(tasks=0, seconds=0.0, improved=0, exact=0, errors=0))
                    s['tasks'] += 1
                    s['seconds'] += res.get('seconds', 0)
                    s['exact'] += res.get('outcome') == 'exact'
                    s['improved'] += res.get('outcome') == 'improved'
                    s['errors'] += res.get('status') not in ('ok',)
                    st['stages'][stage['name']] = dict(seconds=seconds, status=res['status'], base=res.get('base'),
                                                       best=res.get('best'), outcome=res.get('outcome'))
                    if res.get('outcome') in ('exact', 'improved'):
                        st['improved_at'] = time.time()
                        st['fails'] = 0
                self.save_state()
                self.log('done  %s %s:%s -> %s base=%s best=%s (%s s)%s' % (
                    stage['name'], unit, sym, res.get('outcome', res['status']), res.get('base'), res.get('best'),
                    res.get('seconds'), ' VERIFIED ' + str(res.get('verify_info')) if res.get('verified') else
                    (' REJECTED: ' + str(res.get('verify_info')) if res.get('outcome') == 'rejected' else '')))

        threads = [threading.Thread(target=worker, args=(False,), daemon=True) for _ in range(lanes)]
        for t in threads:
            t.start()
        extra = []
        interval = float(boost.get('interval', 30))
        while any(t.is_alive() for t in threads + extra):
            time.sleep(2)
            if int(time.time() - stage_t0) % int(max(interval, 2)) < 2 and queue and boost.get('lanes', 0) \
                    and stage.get('boost', True) and len(extra) < int(boost.get('lanes', 0)) and self.boost_wanted() \
                    and not self.stopping():
                t = threading.Thread(target=worker, args=(True,), daemon=True)
                t.start()
                extra.append(t)
                self.log('boost lane started (cpu %s%%)' % (round(self.cpu[-1]) if self.cpu else '?'))
        for t in threads + extra:
            t.join()

    def boost_wanted(self):
        b = self.cfg.data.get('boost', {})
        if not b.get('lanes', 0):
            return False
        c = cpu_percent()
        if c is not None:
            self.cpu.append(c)
        recent = self.cpu[-2:]
        return len(recent) == 2 and all(x < float(b.get('cpu_below', 60)) for x in recent)

    def main_loop(self, items):
        pass_no = int(self.state.get('pass', 1))
        park_after = int(self.cfg.get('run', 'park_after', 2))
        while not self.stopping():
            before = {k: v.get('improved_at') for k, v in self.state['funcs'].items()}
            started = self.pass_over(items, pass_no)
            if self.stopping():
                break
            for k, v in (self.state["funcs"].items() if started else []):
                if v.get("improved_at") == before.get(k) and not v.get("parked"):
                    v['fails'] = v.get('fails', 0) + 1
                    if v['fails'] >= park_after:
                        v['parked'] = True
                        self.log('parked %s (no improvement in %d passes; retried when its source changes)' % (k, v['fails']))
            self.save_state()
            if not started:
                self.log('nothing left to do')
                break
            pass_no += 1
            self.state['pass'] = pass_no


# ---------------------------------------------------------------- package / report
def write_package(run, items):
    out = run.out
    pkg = os.path.join(out, 'package_' + time.strftime('%Y%m%d_%H%M%S'))
    os.makedirs(pkg, exist_ok=True)
    rows = []
    if os.path.exists(run.results_path):
        for l in open(run.results_path, encoding='utf-8'):
            try:
                rows.append(json.loads(l))
            except ValueError:
                pass
    stages_cfg = {s['name']: s for s in run.cfg.data['stage']}
    perm_runs = []
    lines = ['# Run report', '', 'written %s' % time.strftime('%Y-%m-%d %H:%M:%S'), '']
    ready, improved, other = [], [], []
    for r in rows:
        kind = stages_cfg.get(r['stage'], {}).get('kind', 'unit-source')
        if r.get('outcome') in ('exact', 'improved') and kind == 'permuter':
            folder = stages_cfg[r['stage']].get('run_folder', '{func}').format(func=re.sub(r'[<>:"/\\|?*]', '_', r['func']))
            perm_runs.append(folder)
            continue
        if r.get('outcome') in ('exact', 'improved') and r.get('path') and os.path.exists(r['path']):
            sub = 'ready' if r['outcome'] == 'exact' else 'improved'
            d = os.path.join(pkg, sub, re.sub(r'[<>:"/\\|?*]', '_', r['unit'] + '__' + r['func']))
            os.makedirs(d, exist_ok=True)
            ext = os.path.splitext(src_path_of(r['unit']))[1]
            shutil.copyfile(r['path'], os.path.join(d, 'new_source' + ext))
            cur = src_path_of(r['unit'])
            shutil.copyfile(cur, os.path.join(d, 'old_source' + ext))
            import difflib
            open(os.path.join(d, 'diff.txt'), 'w', encoding='utf-8').write('\n'.join(difflib.unified_diff(
                open(cur, encoding='utf-8', errors='replace').read().splitlines(),
                open(r['path'], encoding='utf-8', errors='replace').read().splitlines(), 'current', 'candidate', lineterm='')))
            info = dict(r, src=cur)
            write_json(os.path.join(d, 'info.json'), info)
            (ready if sub == 'ready' else improved).append((r, d))
        elif r.get('outcome') == 'rejected':
            other.append('%s:%s (%s) rejected: %s' % (r['unit'], r['func'], r['stage'], r.get('verify_info')))
    lines += ['## Ready (verified exact)', ''] + ['- %s %s (%s stage %s, %s s) -> `%s`' % (
        r['unit'], r['func'], r['base'], r['stage'], r['seconds'], os.path.relpath(d, out)) for r, d in ready] or ['(none)']
    lines += ['', '## Improved (verified better, not exact)', ''] + ['- %s %s: %s -> %s (stage %s) -> `%s`' % (
        r['unit'], r['func'], r['base'], r['best'], r['stage'], os.path.relpath(d, out)) for r, d in improved] or ['(none)']
    if perm_runs:
        pr = os.path.join(pkg, 'permuter_review')
        cfg_runs = run.cfg.get('paths', 'runs', os.path.join(out, 'perm_runs'))
        argv = [PY, TOOLS + '/permuter/permcollect.py', '--out', pr, '--runs', cfg_runs] + sorted(set(perm_runs))
        if run.cfg.get('paths', 'permuter'):
            argv += ['--permuter', run.cfg.get('paths', 'permuter')]
        r = subprocess.run(argv, capture_output=True, text=True, encoding='utf-8', errors='replace', cwd=REPO)
        open(os.path.join(pkg, 'permuter_collect.log'), 'w', encoding='utf-8').write(r.stdout + r.stderr)
        lines += ['', '## Permuter results', '', 'collected into `permuter_review/` (ready / improved / hints, see its REVIEW.md):', '']
        lines += ['    ' + l for l in r.stdout.strip().splitlines()[-30:]]
    if other:
        lines += ['', '## Rejected by verification', ''] + ['- ' + o for o in other]
    lines += ['', '## Time per stage', '', '| stage | tasks | task-seconds | exact | improved | errors |', '|---|---|---|---|---|---|']
    agg = {}
    for r in rows:
        s = agg.setdefault(r['stage'], dict(tasks=0, seconds=0.0, exact=0, improved=0, errors=0))
        s['tasks'] += 1
        s['seconds'] += r.get('seconds', 0)
        s['exact'] += r.get('outcome') == 'exact'
        s['improved'] += r.get('outcome') == 'improved'
        s['errors'] += r.get('status') != 'ok'
    for k, s in agg.items():
        lines.append('| %s | %d | %.0f | %d | %d | %d |' % (k, s['tasks'], s['seconds'], s['exact'], s['improved'], s['errors']))
    parked = [k for k, v in run.state['funcs'].items() if v.get('parked')]
    lines += ['', '## Parked (no improvement; retried automatically when the function source changes)', ''] + \
             ['- ' + k for k in parked[:60]] + (['- ... %d more' % (len(parked) - 60)] if len(parked) > 60 else []) or ['(none)']
    errs = [r for r in rows if r.get('status') != 'ok']
    if errs:
        lines += ['', '## Tool problems', ''] + ['- %s %s: %s %s' % (r['stage'], r['func'], r['status'], r.get('note', '')[:100].replace('\n', ' '))
                                                for r in errs[:30]]
    if run.cpu:
        lines += ['', 'average cpu: %.0f%% (%d samples)' % (sum(run.cpu) / len(run.cpu), len(run.cpu))]
    k = 0
    while k < len(lines):          # an empty section says so
        if lines[k].startswith('## ') and (k + 2 >= len(lines) or lines[k + 2] == '' or lines[k + 2].startswith('## ')):
            lines.insert(k + 2, '(none)')
        k += 1
    open(os.path.join(pkg, 'REPORT.md'), 'w', encoding='utf-8').write('\n'.join(lines) + '\n')
    shutil.copyfile(os.path.join(out, 'runner.log'), os.path.join(pkg, 'runner.log'))
    return pkg


def cmd_dry(cfg, args):
    items = select(cfg, args.only, args.limit)
    print('%d functions selected (report.json, unlinked units, %s..%s %%, size <= %s):' % (
        len(items), cfg.get('select', 'min', 80), cfg.get('select', 'max', 99.99), cfg.get('select', 'max_size', 1200)))
    for unit, sym, pct, size in items[:40]:
        print('  %6.2f%% %5d  %s  %s' % (pct, size, unit, sym))
    if len(items) > 40:
        print('  ... %d more' % (len(items) - 40))
    run = Run.__new__(Run)
    run.cfg, run.out, run.prefix = cfg, os.path.abspath(cfg.data.get('output', 'build/scratch/runner')), 'dry_'
    run.args = args
    print('\nstages:')
    total = 0
    for s in cfg.data['stage']:
        if not s.get('enabled', True):
            print('  %-12s disabled' % s['name'])
            continue
        u, f, p, _ = items[0] if items else ('unit', 'func', 0, 0)
        print('  %-12s %4ds each, kind %s' % (s['name'], s['seconds'], s.get('kind', 'unit-source')))
        print('     ' + ' '.join(run.command(s, u, f, s['seconds'], int(s.get('threads', 4)), 'dry_tag')))
        total += s['seconds'] * len(items)
    lanes = int(args.lanes or cfg.get('run', 'lanes', 2))
    print('\none pass = about %.1f hours with %d lanes (if every task uses its full time)' % (total / lanes / 3600, lanes))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('action', choices=['run', 'dry-run', 'status', 'stop', 'report'])
    ap.add_argument('--config', default=os.path.join(HERE, 'default.toml'))
    ap.add_argument('--hours', type=float)
    ap.add_argument('--lanes', type=int)
    ap.add_argument('--only', help='only functions/units containing this text')
    ap.add_argument('--limit', type=int, help='at most N functions')
    args = ap.parse_args()
    cfg = Config(args.config)
    if args.action == 'dry-run':
        return cmd_dry(cfg, args)
    out = os.path.abspath(cfg.data.get('output', 'build/scratch/runner'))
    os.makedirs(out, exist_ok=True)
    if args.action == 'stop':
        open(os.path.join(out, 'STOP'), 'w').write(time.strftime('%F %T'))
        print('STOP file written; the runner stops after the running tasks and writes the package (resume: run again)')
        return
    run = Run(cfg, args)
    if args.action == 'status':
        tail = open(os.path.join(out, 'runner.log'), encoding='utf-8').read().splitlines()[-12:] if os.path.exists(os.path.join(out, 'runner.log')) else []
        st = run.state['funcs']
        print('%d functions known, %d parked, %d with a win' % (len(st), sum(1 for v in st.values() if v.get('parked')),
                                                                 sum(1 for v in st.values() if v.get('improved_at'))))
        print('\n'.join(tail))
        return
    items = select(cfg, args.only, args.limit)
    if args.action == 'report':
        print('package:', write_package(run, items))
        return
    if os.path.exists(run.stop_file):
        os.remove(run.stop_file)            # a stale STOP from the previous run must not stop this one
    run.log('run started: %d functions, %.1f h, config %s' % (len(items), (run.deadline - run.t0) / 3600, cfg.path))
    try:
        run.main_loop(items)
    except KeyboardInterrupt:
        run.log('interrupted (Ctrl+C) - packaging')
        run.deadline = 0                   # running tasks are abandoned (their tool processes end by their own timeout)
    except Exception:
        run.log('CRASH: ' + traceback.format_exc())
    finally:
        run.save_state()
        pkg = write_package(run, items)
        run.log('package written: ' + pkg)


if __name__ == '__main__':
    main()
