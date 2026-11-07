# 07 - decomp-permuter, source search, and the near-miss workflow
Applies to: **universal** strategy (permuter, scripted search, near-miss workflow, puzzles file); the specific edit families are tuned to **GameCube / MWCC**.

## What the permuter is good at

[decomp-permuter](https://github.com/simonlindholm/decomp-permuter) mutates a C function and keeps the variants that
score closer to the target object. It is strong on **register-only and scheduling-only** differences, weak on
structural ones (a missing local, a different loop shape - fix those by reading the diff). Typical results: a few exact
hits per hundred near misses, plus many "improved but not exact" candidates that serve as hints.

### Before you start

1. **Faithfulness check.** Compile and score the unmodified base (`--debug`, or just the first line of the run). A base
   score far above ~5 per differing register line means the setup is not faithful: stale `base.c`, the function was
   moved away from its original position (later functions then get inlined), wrong flags, or a deferred file. Fix the
   setup first.
2. **Isolate the function.** In deferred-inlining files extra helper functions are emitted; score only the target
   function (the template's `isolate_function.py` idea).
3. **Do the cheap manual forms first** (chapter 03 section C): declaration order, pointer-vs-index, typed temporaries,
   `int` vs `s32`, inline helper vs inline code.
4. C++ methods: the permuter parses C. Either temporarily add an `extern "C"` copy that takes `self` as the first
   parameter (same code, `self` is `r3`) and permute that, or hide unparsable statements behind `PERM_IGNORE(...)`.
5. Clean up the weights: passes that only create banned code (`(0, x)`, `^ 0`, dummy variables, empty blocks, `volatile`)
   or that change behaviour (statement reordering across calls, AST removal) should be off, or every score is
   uninstallable. See `../permuter/` for the weights and the behaviour-preserving passes we added.

### After a hit

- A result is a **hint**. Read the diff; rewrite it into code a human would write; recompile; re-run all checks.
- Comma expressions `(0, EXPR)` = an *inlined call*: write the value through a small `static inline` helper or a
  temporary local. A `long long tmp = x; f(tmp);` hit usually means a value wants a wider type or an extra copy.
- A score of 0 ignores branch **targets** and constant values: re-check with relocations/label offsets
  (`reldiff.py`) and the const check.
- Reject results that need a duplicated dead store, a second pointer equal to the first, a casted `this`, `volatile`
  or a changed constant. Behaviour changes sneak in: swapped arguments, a different variable, an overwritten
  accumulator.
- Keep spelling: `unsigned int` is not `u32`; types in a permuter result must be translated exactly.

### Making the search cheaper

- **Memory of tried candidates** (`../permuter/`): 20-60% of compiles repeat something already tried, within a run and
  across runs. A shared store of scores avoids them.
- **Focus regions.** Limit mutation to the source lines that create the misplaced values
  (`regwhere.py` tells which) plus the declaration block.
- **Stop when idle.** If there is no new best for a few minutes, move on; run the same function again later with a
  different seed or after a manual change.
- Run register-only near misses first; they are where the permuter pays.

## Scripted source search (no randomness)

For a stuck function, a *systematic* search of behaviour-preserving edits is faster than random mutation:
declaration moves, hoisting a sub-expression into a typed local, `int`/`s32`, call -> inline helper -> temporary,
pointer local -> its expression, `x++` split/merged, compound vs plain assignment, `for` -> `while`. Score each variant
with the real compiler and combine the ones that improve. In practice:

- `srcvariants.py`: you list variants (text replacements) in a small file; it compiles each and prints every function
  whose score changes.
- `labgrid.py`: a *miniature* of the stuck function with `<<<a|||b>>>` alternatives; compiles all combinations with
  the unit's flags and groups them by identical code (or by each variable's register with `--vars`). Answers "which of
  these forms even matter?" in seconds. Many hours of speculation end here.
- `regplan.py` / `regwhere.py`: which variable and which source line produce the wrong register (from compiling with
  `-sym on` and reading the debug info).
- `structdist.py`, `regonly.py`: classify near misses (register-only vs structural vs ordering) so you choose the
  right tool.

## Near-miss workflow

1. Classify every non-exact function: structural (instruction set differs) vs register-only vs order-only
   (`regonly.py`, `structdist.py`). Do **structural** ones by hand first: a real source difference hides there.
2. For structural ones check the mechanical suspects: frame size, switch cases, callee prototypes, shadowing,
   signedness, inverted conditions (tools list in `../tools/README.md`).
3. Register-only: declaration order -> pointer/index -> temporaries -> inline helpers -> types -> opt level -> permuter.
4. Park after two fruitless attempts. Add a note: function, score, what was tried, what the diff looks like.
5. Revisit parked functions when you learn a new trick; do not re-run the same searches.

## Keeping a "puzzles" file

Whenever a shape resists, write it down as *pattern + what was tried + where it occurs*, plus a regex over the asm.
`patternhunt.py` runs that regex over other projects' matching asm (same compiler) and prints the source of each hit:
solutions to several puzzles in this guide came from another game's source (the `continue`/explicit-`else` loop shape,
the `if (model) return;` void function, the sibling's `float` getter). Searching sibling decomps before experimenting
is the cheapest trick in this guide.

## Tuning the search with data

Applies to: universal strategy (the edit families are MWCC-flavoured).

- **Order of attack for a register-only near miss:** (1) check the *real* difference is zero (`shapedist.py`); (2) the
  scripted source-edit search (`tools/search/regsolve.py`: declaration moves, hoists, int/s32, call to inline helper,
  call to temp local, pointer local to its expression, then pairs and a greedy combination; and the "expr" family:
  split `x++`, `++x` before, compound versus plain assignment, reuse a loaded local, move a call-free assignment up,
  integer sum regrouping, load into a temporary, `for` to `while` with a temporary step); (3) the permuter (focused on
  the lines that define the misplaced values, then whole-function); (4) park.
- **Use an ordering score as the tie breaker** (`regsolve --order`): among edits with equal diff-line counts, prefer
  the one whose colouring order is closer to the target's (chapter 12, section 3). It turned several near misses into
  exact matches.
- **Start from the most faithful base.** If a hand-found variant has a more faithful shape (right width, right
  temporary) but a lower score, make that the base of the next search.
- **Know when the permuter is exhausted.** Statistics over tens of thousands of improvements showed no useful gain left
  on the functions that are still register-only: stop re-running the same pile; spend the CPU on new functions,
  new tricks, or a shared helper that explains a whole family.
- **Do not train a ranker on edit kind + function statistics.** Tried, cross-validated by function, no better than random
  (chapter 12, section 4).
- Skip functions that are already exact in the current source ("base score 0 = already exact"): a stale list otherwise
  burns minutes per fake zero.
