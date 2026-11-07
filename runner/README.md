# Long-run runner

A configurable runner for unattended search sessions ("overnight runs") on a **dtk-template project**: it picks
near-miss functions from `build/<VER>/report.json`, runs a list of external search tools on them (one *stage* per tool),
verifies what they report, remembers what it tried, and leaves a **package folder** with a morning report. It never
edits your source files; applying a result is a separate, re-verifying step (`apply.py`).

The tools it calls are anything that follows a one-line contract (below); the toolkit's own are
`tools/permuter/*` (permutation search) and, if you have them, `tools/search/regsolve.py` / `tricksweep.py`.

## Setup

- Project as for the other tools (`DECOMP_REPO`, `DECOMP_VERSION`, `python configure.py` done, a recent
  `build/<VER>/report.json`). Python 3.11+ (TOML config; JSON configs work on any 3.9+).
- For permuter stages: a decomp-permuter checkout (`paths.permuter` in the config or `DECOMP_PERMUTER`), see
  `tools/permuter/README.md`.
- Copy `runner/default.toml`, edit stages, selection and time budget.

## Run it

```
python runner/runner.py dry-run --config my.toml          # what would run, the exact command lines, a time estimate
python runner/runner.py run     --config my.toml --hours 8
python runner/runner.py status  --config my.toml          # counts + the last log lines (from another window)
python runner/runner.py stop    --config my.toml          # clean stop: running tasks end, package is written
python runner/runner.py report  --config my.toml          # rebuild the package from the logs at any time
```
Stop also works with Ctrl+C. **Resume** = run the same command again: `state.json` holds, per function, the
fingerprint of its source text and which stages were tried with which time budget; finished work is skipped, a function
whose source changed is retried from scratch. If the machine or Python dies, `runner.log` (flushed and fsynced per
line) and `results.jsonl` (one line per finished task) are intact; just start it again, then `report` if you want the
package of the dead run.

## Config (TOML or JSON)

```toml
output = "build/scratch/runner"   # everything the runner writes
hours = 8
scratch_prefix = "runner_"        # scratch names under build/scratch (verification copies, fcheck tags)

[paths]   permuter = "/path/to/decomp-permuter"   runs = "build/scratch/perm_runs"
[run]     lanes = 2   park_after = 2   pass_growth = 1.5
[boost]   lanes = 2   cpu_below = 60   interval = 30
[select]  min = 80 max = 99.99 max_size = 1200 order = "closest"  # units = [regex], skip = [...], skip_file, top

[[stage]]
name = "regsolve"
kind = "unit-source"
command = "{py} {tools}/search/regsolve.py {unit} {func} --src {src} --timeout {t}"
seconds = 120
```
- **Selection**: functions of units that are not `complete` in `report.json`, with fuzzy percent in `[min, max]`, code
  size `<= max_size`; skip by unit prefix (`game/old/`), symbol, or `unit:symbol`.
- **Stages** run in order, each over all selected functions (a *pass*), `lanes` tasks at a time. A task is killed
  after `seconds * grace(1.5) + 90` s. `enabled = false` switches a stage off; `threads` is `{j}`; `boost = false`
  keeps a stage out of boost lanes.
- **Passes and parking**: after a pass, functions that nobody improved get a strike; after `park_after` strikes they
  are *parked* (skipped) until their source text changes. Further passes multiply each stage's seconds by `pass_growth`.
- **Boost lanes**: if the last two CPU samples are below `cpu_below` percent, up to `boost.lanes` extra workers take
  tasks from the same queue; they retire when the CPU is busy again.
- **Hot reload**: the config file is re-read between tasks. A broken edit is rejected with one log line and the
  previous config stays in force. (Stages added or removed take effect at the next pass.)
- Command placeholders: `{py} {repo} {ver} {tools} {unit} {func} {src} {t} {j} {tag} {permuter} {runs}`.

## The tool contract

A stage command ends its stdout with
```
RESULT <unit> <func> <base> <best> <path-or-NONE>
```
`base`/`best` = number of differing lines (or permuter penalty), 0 = exact, `NONE` = unknown. `path` is
- stage `kind = "unit-source"`: a complete candidate source file for the unit. The runner compiles it with
  `tools/fcheck.py --src` and accepts it only if the function got better (exact for `best == 0`) and no other function of
  the unit got worse; otherwise it is logged as *rejected*.
- stage `kind = "permuter"`: a permuter output; at the end the runner runs `tools/permuter/permcollect.py` on the
  stage's `run_folder` (default `{func}`, tandem: `{func}__tandem`), which maps the edit onto your source, applies the
  behaviour guard and verifies it.
`examples/stub_stage.py` is the smallest conforming tool, handy to test a config.

## Output (`<output>/`)

`runner.log`, `results.jsonl`, `state.json`, `logs/<stage>/<func>.log`, and `package_<time>/`:
`REPORT.md` (ready, improved, rejected, per-stage time and win counts, parked functions, tool problems, average CPU),
`ready/` and `improved/` (`new_source.*`, `old_source.*`, `diff.txt`, `info.json` per result), `permuter_review/`
(permcollect's ready/improved/hints). **Review a diff, then**
```
python runner/apply.py <package>/ready/<unit>__<func>          # replaces the source, runs fcheck, restores on failure
python runner/apply.py <package>/permuter_review/ready/<func>
```
`apply.py` refuses if the source changed since the result was made, keeps `<src>.before_apply`, and finishes by printing
fcheck's checks. Build with your normal `ninja` and commit yourself.

## Safety

The runner compares each unit's source file before and after every task and restores it if a tool edited it (logged as
WARNING). It writes only below `<output>` and `build/scratch/<scratch_prefix>*`, plus the tools' own scratch folders
(fcheck: `build/scratch/fcheck/<prefix>*`). It does not run ninja.

## Limitations

- Developed and tested on Windows; the CPU sampler also has a Linux branch (`/proc/stat`) that was not exercised.
- The per-function fingerprint is a hash of the function's text found by name and braces; a function that cannot be
  located (macros, unusual formatting) falls back to a hash of the whole file, so any edit of the file reopens it.
- A running tool is killed by timeout, not by `stop`: a stop waits for the current tasks (at most their time budget).
- Verification of `unit-source` results checks the function percent and other functions of the unit only; run the full
  `fcheck.py` checks (layout, constants, data) and your normal build before committing.
- Requires decomp-permuter for the permuter stages; `regsolve.py`/`tricksweep.py` are separate tools following the
  contract above, not part of this folder.
