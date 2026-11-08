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

### Focused and tandem permuter runs

Applies to: universal (the measurements are from a GameCube/MWCC project; the method works with any compiler)

A plain permuter run mutates the whole function at random. On a long function most mutations land far away from the
problem, so the run spends hours on edits that cannot matter. Two refinements fix that:

- **Focused run.** Mutate only a few marked regions: the lines that create the misplaced values (a line table from a
  debug build, or `regwhere`-style output, tells you which source line defines each wrong register) plus the
  declaration block. Every candidate is now "near" the problem, so a lot more of them change the score.
- **Tandem run** (`tools/permuter/permtandem.py`). Two lanes on the SAME function at the same time, sharing CPU:
  a **focus** lane (only the marked regions, register-biased mutation weights) and a **whole** lane (the entire body).
  Every N seconds the best new find of either lane - after a behaviour guard rejects junk (volatile, empty ifs,
  repeated conditions, read-before-set, changed constants or call arguments) - is spliced into BOTH lanes as their new
  starting point, and the focus regions are re-marked around the statements that just changed. The log says which lane
  found what. Stop on score 0, after a few idle minutes, or at a time limit.

**What a fair test showed.** We ran all three modes at the same time, with equal CPU, on a fixed list of register-only
near misses (two rounds, 20-24 functions, 10-20 minutes each), then judged every result by hand:

| mode | valid wins | note |
|---|---|---|
| tandem | 3-4 | including one score 280 -> 0 (exact) and one 155 -> 55 that needed a find from EACH lane combined |
| whole function only | 4 | |
| focus only | 0 | never won on its own |

So: **narrowing alone is not enough** - the fix is often a small edit somewhere you did not mark (a declaration far
away, a helper). Narrowing is valuable as a *partner* that refines what the whole-function lane finds. In a long
unattended run the tandem pass reached score 0 on 8 functions in one night where the previous night's plain pass
reached 0.

**Practical lessons**
- Check the starting point first: if the permuter's base does not score close to your real diff (a few points per
  differing line), its "improvements" are noise. Re-create the setup from the current source.
- When a function's tandem turn fails, restart it from a few different *source variants* (seed hopping, e.g. the
  top variants of a scripted search) instead of the same start with a new random seed.
- Read results by hand. Automatic validity checks on reprinted permuter code have false alarms (an `else if` split
  looks like a structure change) and miss things (an overwritten accumulator). Keep junk passes (volatile, `(0, x)`)
  switched off.
- Record what each run tried (a shared "seen" store): 20-60 % of candidates repeat earlier ones.
- Know when to stop: once the leftovers are pure register colouring, the permuter's yield drops to almost nothing.
  Mining the history of its results (which edit kinds ever helped which shape of problem) tells you when you are there.

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
