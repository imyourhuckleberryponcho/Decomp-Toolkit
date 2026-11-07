# tools/search - source-variant search for near-miss functions

Two tools that take ONE function whose structure is already right but whose registers or instruction order differ from
the target, compile many small source rewrites with the unit's real compile command, and report the best one.
They never edit your sources: variants are written to `build/scratch/search/` and compiled with `../fcheck.py --src`.

| Tool | What it tries | Typical win |
|---|---|---|
| `regsolve.py UNIT FUNC` | Edits aimed at register colouring: move/hoist a declaration, `int` <-> `s32`/`long`, other local widths, route a call through a tiny inline helper, hold a call result or sub-expression in a named local (or pass a local straight on), replace a pointer local by its expression, if/else <-> ternary, operand flips on the lines that define the misplaced registers, and an expression family (`x++` split out, `++x` before, compound <-> plain assignment, reuse of a just-loaded local, moving a call-free assignment up, three-term integer sum regrouping, load into a temp, `for` -> `while`). Round 1 = every single edit, round 2 = pairs of the best, round 3 = greedy growth, then restart from the best text. | register-only misses (same instructions, other registers) |
| `tricksweep.py UNIT FUNC` | The trick library in `tricks.py` (54 pure source -> variants functions: statement/declaration order, operand order, compound and inc/dec forms, loop forms, early return / nested if, `&&` splits, chained inits, ...), hill-climbing and stacking non-overlapping wins. `--list` prints the tricks. | order / shape differences, small structural misses |

Use them AFTER the function body is structurally right (same calls, same control flow). If whole blocks are missing,
fix the code first; these tools only reshuffle what is there. Run `regsolve.py` first on register-only misses (it needs
`regwhere.py` and `dtk` for the culprit-line edits, and works without them for the plain edits); run `tricksweep.py` when
the instruction ORDER differs or regsolve found nothing.

## Usage

Run from the project root (or set `DECOMP_REPO`; `DECOMP_VERSION` if `build/` has several version folders):

    python tools/search/regsolve.py game/player update__6PlayerFv
    python tools/search/regsolve.py game/player update__6PlayerFv --src build/scratch/attempt.cpp -j 8 --timeout 900
    python tools/search/tricksweep.py game/player update__6PlayerFv --depth 3 --timeout 600
    python tools/search/tricksweep.py game/player update__6PlayerFv --tricks t_stmt_swap,t_commute
    python tools/search/tricksweep.py --list

`UNIT` is the path under `src/` without extension, `FUNC` the symbol as it appears in the asm / object (mangled for
C++ methods, constructors and destructors included). `--src FILE` starts from another file than `src/UNIT.c(pp)`, e.g. the
best text of an earlier search. `-j N` parallel compiles (default 6), `--timeout S` stops the search after S seconds and
still reports what it has. Needs `python configure.py` to have been run (the compile command comes from
`ninja -t commands`) and the target object in `build/<VER>/obj/`.

Environment: `SEARCH_SCRATCH` (output folder, default `build/scratch/search`), `SEARCH_TAG` (prefix for the
`build/scratch/fcheck/<tag>` folders, useful to tell several runs apart).

## Your own tricks (plug-ins)

`tricksweep.py --plugins my_tricks.py[,more.py]` or `SEARCH_TRICK_PLUGINS=my_tricks.py` adds project tricks without
editing `tricks.py` (a plug-in with the same name replaces a built-in one). Template: `examples/my_game_tricks.py`;
why and what to write: `guide/11-customizing-for-your-game.md`. In the runner, set the variable in the environment
the runner starts in, or put `--plugins ...` in the stage's command line.

## The RESULT contract

The last line of output is always

    RESULT <unit> <func> <base_score> <best_score> <path-to-best-variant-or-NONE>

`score` = number of differing instruction lines of the function against the target (registers and relocations
included, `fcheck.py --diff` normalisation), lower is better, 0 = exact. The path is the full source file of the best
variant (written only when `best < base`), otherwise `NONE`. If the base does not compile or the function cannot be
found the line is `RESULT <unit> <func> -1 -1 NONE` and the exit code is 2. Everything else printed (rounds, the 5-8
best candidates per round, a unified diff of the winner) is for humans.

Inside `regsolve.py` the search ranks by a slightly stricter internal score (+10 per instruction only one side has, +100
per other function of the unit that stops matching, because helper edits touch every inliner); RESULT reports the plain
differing-line count. The objdiff percentage before/after is printed when something improved.

## Files

- `regsolve.py`, `regsolve_expr.py` - the register search; `regsolve_expr.py` is its expression-edit module (imported).
- `tricks.py` - the trick library. Plug-in list `TRICKS` of `Trick(name, description, fn)`; `fn(body_text)` yields
  `(description, new_body_text)`. Add a trick by writing a function and adding a line to `_DESC` at the bottom.
  `variants(file_text, 'Class::method')` yields whole-file variants round-robin over the tricks.
- `tricksweep.py` - the sweep, a thin driver over `tricks.py`.

## Limitations (please read)

- Variants are TEXT rewrites guarded by regexes: they keep values the same in the cases the guards recognise
  (side effects, narrowing, precedence, operator families) but they are not a proof. Pointer aliasing between different
  pointers is not analysed when statements are reordered. Always read the diff of a winner and re-run your normal
  checks (`fcheck.py`, build, hash) before installing it. No trick uses `volatile`, comma-operator hacks or empty ifs;
  `tricksweep.py` also refuses winners that introduce them.
- Only the one function is scored; edits to helper functions or file-scope declarations change other functions, and
  `tricksweep.py` does not check them (`regsolve.py` penalises breaking exact functions of the same unit).
- The variant file is compiled from `build/scratch/search/...` under the original file name, so `#include "x.h"`
  relative to the source folder will not resolve; include through `-i` paths as dtk templates normally do.
- Type respellings assume the usual `u8/s32/f32` typedefs and a 32-bit `long` (true for the GameCube/PowerPC
  compilers this was written for). On other targets the `int`/`long` swaps are not value-preserving.
- Heuristic source location: the function body is found by name (demangled through `dtk demangle` when available,
  else by the Metrowerks mangling rules). Exotic definitions (macros generating functions, definitions split by
  `#if`) may not be found; the tools then print `RESULT ... -1 -1 NONE`.
- Compile time dominates: roughly 0.5-3 s per variant per core. A function with hundreds of candidate edits and a
  large unit can take minutes; use `--timeout`.
- A zero score means the function's code is identical to the target, not that the unit links: check layout, constants
  and data with `fcheck.py` before marking the object as matching.
