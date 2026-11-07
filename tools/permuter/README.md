# Permuter drivers

Generic drivers for [decomp-permuter](https://github.com/simonlindholm/decomp-permuter) on a **dtk-template project**
(MWCC GameCube/Wii). They set up a run folder for one function from the unit's own compiler command line, check that
the setup is faithful, run single functions or lists of them in parallel, and turn the results into a **review folder**
(verified / hints) instead of touching your source.

## Setup

1. A decomp-permuter checkout, ideally patched with `../../permuter/our-changes.diff` (shared memory of tried
   candidates, safe reorder passes, `PERM_HELPERS`, no generated `volatile`). Upstream works too; the drivers need the
   patch only for speed. Point to it with `--permuter DIR` or `DECOMP_PERMUTER`.
2. The project as for the other tools: run from the project root or set `DECOMP_REPO` (and `DECOMP_VERSION`);
   `python configure.py` once, so `ninja -t commands` knows the unit's flags; `build/<VER>/{asm,obj}` and
   `build/binutils`, `build/tools/{dtk,objdiff-cli}` present.
3. Run folders go to `build/scratch/perm_runs` (`--runs DIR` / `DECOMP_PERM_RUNS`). Python 3.9+, standard library only.
   Windows needs no bash: the patched permuter calls `compile_driver.py` directly (an unpatched permuter falls back to
   the generated `compile.sh`, which needs bash).

## The tools

| Tool | What it does |
|---|---|
| `permsetup.py UNIT SYMBOL` | creates `<runs>/<SYMBOL>/`: `target.o` (only that function), `base.c` (preprocessed unit, the function in place, everything else hidden behind `PERM_IGNORE`), `compile.sh` + `compile_native.json` (real flags, then keep only that function), `settings.toml` with CLEAN weights. C functions and C++ free functions directly; C++ **methods** through a surrogate `permX(...)` using `self->member`, with hidden `#define`s that give the compiler the real method back. |
| `permcheck.py RUN` | scores the unmodified `base.c`. Roughly 5 per differing register line is faithful; huge insertion/deletion counts mean a stale or wrong setup (exit code 2); 0 = already exact. |
| `permrun.py UNIT SYMBOL` / `RUN` | set up + run for `--seconds`, `--idle` early stop, `--stopfile`; kills the permuter and its workers; last line `RESULT <unit> <symbol> <base> <best|NONE> <path|NONE>`. |
| `permbatch.py --list FILE` | many functions, `--lanes N` at a time, claim files in `<runs>/_claims` so parallel batches or runner lanes never take the same function, `--tandem`, `--skip-done`, results appended to `batch_results.tsv`. |
| `permtandem.py UNIT SYMBOL` | two lanes on one function: **focus** (random edits only around the declaration block and the statements just changed, register-biased weights) and **whole** (the entire body). Every `--check` seconds the best new, guard-passing find re-seeds both lanes. Stops on score 0, after `--idle` seconds without progress, or at `--seconds`. |
| `permcollect.py RUN... / --all` | turns results into `ready/` (the edit is mapped back onto your source text, compiled with the real flags with `tools/fcheck.py --src`, function exact, nothing else worse), `improved/` (natural, better, not exact) and `hints/` (needs a hand rewrite: behaviour guard fired, macros in the function, does not compile, not better). Never edits `src/`. |
| `guard.py`, `semguard.py` | the behaviour guard used by tandem and collect: changed constants, swapped call arguments, another existing variable, statements moved across a dependency, changed number of returns/branches, narrowing casts, added `volatile`, `goto`, `(int)this`, `(0, x)`, empty `if` blocks, padding variables. |
| `parsefix.py` | hides C++ statements the permuter's C parser cannot read behind `PERM_IGNORE` (same code, same score). |

`pcommon.py` (shared helpers) and `compile_driver.py` (the permuter's compile step) are internal.

## Worked example

```
export DECOMP_REPO=/path/to/project DECOMP_PERMUTER=/path/to/decomp-permuter
python tools/permuter/permsetup.py game/menu fooBar__FP3Bar          # run folder
python tools/permuter/permcheck.py fooBar__FP3Bar                    # base score = 30 -> fine
python tools/permuter/permrun.py   fooBar__FP3Bar --seconds 300 -j 8 # or: game/menu fooBar__FP3Bar (sets up first)
python tools/permuter/permcollect.py fooBar__FP3Bar --out /tmp/review
```
Then read `/tmp/review/REVIEW.md`. For a list: `permbatch.py --list funcs.txt --lanes 2 -j 4 --seconds 300 --tandem`
(`funcs.txt`: lines `game/menu fooBar__FP3Bar`).

## Reading the results

- A permuter score is a **hint**. A zero means a source variant compiles to the original; rewrite it naturally before
  keeping it. `permcollect.py` maps only the *token edits* onto your text, so macros and layout survive, but check each
  `diff.txt` yourself - the guard is conservative, not a proof.
- Stop a run: `Ctrl+C` or `--stopfile`. Stale claim files of a crashed process expire after 4 hours (delete
  `<runs>/_claims/*` to free them now).

## Limitations

- Developed and tested on Windows (Python 3.12, MWCC 2.x through the dtk template). Linux/macOS paths are
  untested here (you also need a way to run the compiler).
- The permuter's C parser (pycparser) sees typedef names as plain `int` and class members through the surrogate, so
  struct-heavy functions produce many "permuter failures"; the base score is still right, the search is slower.
- C++ methods: constructors/destructors, templates, reference parameters and `new`/`delete`/`operator` in the body are
  not supported (the setup says so). Results are edits of the surrogate; `permcollect.py` translates them back when
  the source tokens line up, otherwise it writes a hint.
- The focus lane only knows "declaration block + recently edited statements"; it does not use the debug-info based
  register analysis of `regplan.py`/`regwhere.py`.
- A new top-level helper function added by the permuter's `inline_fn` pass is not carried into the next tandem
  base; such a candidate is skipped or lands in `hints/`.
- `permcollect.py`'s `ready/` means: compiles with the unit's flags, the function is 100% by objdiff, no other function
  of the unit got worse. Run the unit's full checks (`fcheck.py`, then your normal build) before committing.
