# decomp-permuter improvements

`our-changes.diff` is a patch against **simonlindholm/decomp-permuter** at commit `8556c81`
("Add a pass to reverse if/else blocks (#215)"). decomp-permuter is MIT licensed (copyright Simon Lindholm and
contributors); this patch is a derivative work and is offered under the same MIT terms (see `LICENSE-permuter-upstream`).

Apply to a clone of upstream:

```
git clone https://github.com/simonlindholm/decomp-permuter
cd decomp-permuter && git checkout 8556c81
git apply --ignore-whitespace /path/to/our-changes.diff
```

New file `src/seenstore.py` is included in the patch. Everything is opt-in or invisible unless noted.

## What changed

1. **Shared, persistent memory of candidate scores** (`src/seenstore.py`, hooks in `src/permuter.py`).
   Measured with 8 workers: 20-36% of compiles in a 60 s run repeated a candidate another worker had already compiled,
   and on a second run of the same function 35-52% repeated earlier work. All workers now append hashes+scores to one
   file per process, read each other's every few seconds, and keep the store between runs. The key covers target object,
   compile commands, scorer options and scorer code. Only scores *worse than the base* are served from the store, which
   are never output anyway, so results are unchanged - just 20-60% more new candidates per minute.
   `PERM_SEEN=0` disables it, `PERM_SEEN_DIR` moves it (default `<run dir>/../_seen`); stores unused for 14 days are
   deleted; stats are written per worker.
2. **`perm_reorder_stmts_safe`** (weight 10): swaps neighbouring statements only when they cannot see each other's
   effects (no shared reads/writes, no calls, no barriers). Upstream's reorder pass can move calls across branches;
   288 of a collection of past manual fixes were pure statement moves.
3. **`perm_swap_comma`** (weight 3): swaps two independent expressions of a comma list whose value is unused
   (`for (i = 0, p = a; ...)` -> `for (p = a, i = 0; ...)`).
4. **Editing inline helpers too** (`PERM_HELPERS=name,name`, `PERM_HELPER_SHARE`, default 0.5): the real cause of a
   register mismatch often sits in a small `static inline` helper that the target function only inlines. With the
   variable set, a share of the mutations is applied to the named helpers (each candidate gets its own copy).
5. **`volatile` is no longer generated** by `random_type` unless `PERM_VOLATILE=1`: a candidate that wins by adding
   `volatile` cannot be installed in a natural source.
6. **Loop-local declarations** (`for (int i = 0; ...)`) are visited correctly by the AST walker.
7. **Windows convenience**: `src/preprocess.py` falls back to a regex pass when no `cpp` is installed (when `base.c` was
   already preprocessed by the compiler), and `src/compiler.py` can run a Python driver named by
   `compile_native.json` next to the compile script instead of `bash` (set up `compile_native.json` with a `"driver"`
   path, or ignore it and use the normal `compile.sh`).

## Related advice

See `guide/07-permuter-and-search.md` for how to set up a faithful run, which passes to turn off for natural-looking
results, and how to treat outputs as hints.
