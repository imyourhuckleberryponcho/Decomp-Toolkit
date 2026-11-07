# 11. Customising the tools for your game

The tools in this repository are generic on purpose: they know C, C++ and the compiler, but not your game. That is
enough to start, but **most of the wins come after you teach them your project**. In the project these tools come
from, the generic search found a fair share of fixes. Even more came from tricks written for that game's own engine,
SDK and draft habits, and from rules mined from the project's own fix history. The near misses of one game cluster
around the same few idioms, so every idiom you teach the tools pays off many times.

This chapter is the "how". Do it gradually: whenever you fix something by hand twice, it belongs in a tool.

## 1. Know what your near misses have in common

Before writing anything, look at the leftovers as a group:

```
python tools/regonly.py --min 90        # register-only / order / immediate / structural, per function
python tools/structdist.py UNIT FUNC    # 0 = only registers differ
```

Then read ten diffs of the same kind side by side. Typical findings, each one a candidate trick:

- the target reads a field through an **engine accessor** (a tiny inline function or macro) where the draft writes
  `obj->field`;
- the target uses an **SDK conversion helper** (for example a fast float-to-u16 inline) where the draft casts;
- the draft is a decompiler output with **casts and temporaries** the original never had (`(s32)`, `var_r3`, ...);
- the target calls a **shared inline helper** (the same instruction run appears in many functions);
- a whole group of files was built with **other flags** (debug menus without the peephole optimiser, a library
  at `-O4,s`, `-inline deferred` files written bottom-to-top).

Write each finding down (a puzzle log is worth it: symptom, what fixed it, what did not). Most entries become one of
the customisations below.

## 2. Add your engine's spellings as trick plug-ins

`tools/search/tricksweep.py` loads extra tricks from plug-in files, so you never have to edit the library:

```
python tools/search/tricksweep.py UNIT FUNC --plugins my_game_tricks.py
SEARCH_TRICK_PLUGINS=my_game_tricks.py   # for every run, including inside the runner
```

`examples/my_game_tricks.py` is a template. A trick is a function `t_name(body) -> yields (description, new_body)`
over the text of ONE function, one variant per place it applies. The most valuable kinds, in our experience:

1. **Same value, other spelling.** Field read through the engine accessor and back
   (`obj->model` <-> `GetModel(obj)`); a cast <-> the SDK helper that computes the same value; an index <-> pointer
   arithmetic written the way the engine's macros write it. The compiler treats an inline's parameter as a new value,
   which moves register allocation; that is why these matter even though the value is identical.
   **Check the inline's body first**: only pairs that are truly identical may be listed.
2. **Undo your draft tool's habits.** Remove decompiler casts that change nothing, fold its temporaries back into the
   expression, turn its `goto` chains into the loops they came from. These are project-specific because every
   draft pipeline leaves its own fingerprints.
3. **Inline helpers.** When the same instruction run appears in many functions, the original had a small
   `static inline` helper. A trick that wraps the repeated statement in a call to *your* helper
   (`helper(a, b)` instead of the open-coded body) can fix a whole family at once; fix the helper, not each caller.

Rules for every trick: it must keep every value the same, it must never add `volatile`, comma tricks, empty `if`s
or padding variables, and you must read the diff of each win before installing it.

## 3. Mine your own fix history

Your version control is a list of solved problems. For every commit that made a function match, the before/after
diff of that function is a rule ("this shape became that shape"). A small script can:

1. walk the history, extract (old body, new body) pairs of functions that went from near miss to exact;
2. keep the small ones (1-3 changed lines) that are behaviour-preserving (same constants, same calls, same
   variables; only order, spelling, declarations, types of the same width);
3. turn each into a trick (a regex replacement or a token-level rewrite) in a plug-in file.

Then **replay** the history: run the trick set on the pre-fix source of every old fix and count how many it
re-finds. That number is your regression test, and the fixes it does NOT re-find, grouped by kind, are the next
tricks to write. The generic regsolve in this repository re-finds a real one-line fix of that kind in seconds
(`window->selection = (pos + 1) + top` -> `1 + (top + pos)`), so the machinery works; what it lacks is your
project's vocabulary.

## 4. Tell regsolve about your project

`tools/search/regsolve.py` searches combinations of behaviour-preserving edits. Two project-specific inputs help it a
lot:

- **types of the same width**: if your headers spell 32-bit integers several ways (`s32`, `int`, `long`, a
  `BOOL` typedef), make sure they are in the swap tables; a wrong-width swap is a behaviour change, so never add a
  pair you have not checked;
- **accessors**: its `access` edit reads `base->field` through an inline accessor; point it at your engine's
  accessor names (same rule: only real ones).

Use `--ops` to restrict it to the edit families that ever worked in your project; it then explores them deeper in
the same time.

## 5. Per-unit compiler settings

Before searching inside a function, rule out the file-level causes, which no source search can fix:

- optimisation level and size/speed mode per file (`stmw/lmw` in the target suggests `-O4,s`);
- `-inline deferred` (function and constant order reversed: `tools/orderscan.py`, `tools/deferscan.py`);
- `#pragma peephole off` / `scheduling off` blocks (often whole subsystems such as debug menus);
- `#pragma force_active on` for functions only reached through data tables (otherwise the linker strips them);
- the original compiler version of a library (SDKs were often built with another one).

Write the settings you find into your build configuration once; then the whole unit's search starts from the right
place.

## 6. Configure the long-run runner for your project

`runner/runner.py` reads a config file (see `runner/default.toml`). Tune it with what you learned:

- **selection**: score band and size limit where the stages actually win in your project; skip lists for units
  that need a human (wrong structure, missing functions);
- **stage order and budgets**: cheap source search first (tricksweep with your plug-ins, regsolve), the permuter
  last and longest; the morning report shows time per stage vs wins, so move time to the stages that win;
- **parking**: functions that keep failing are parked until their source changes, so the run spends its time
  on new chances instead of repeating old failures;
- **guard**: the behaviour guard rejects permuter outputs that change constants, arguments or variables. If your
  project uses macros the guard misreads, teach it those names rather than switching it off.

A good routine: the run works overnight, and in the morning you read the report and install the verified results
by hand. Then fix the near-zero leftovers in person, because they rarely fall by themselves, and turn what you
learned into a new trick before the next run.

## 7. Use other decompilations of the same engine or SDK

If other games used the same engine, SDK or compiler, their matched code is a dictionary of shapes. Search their
assembly for the instruction pattern you cannot reproduce (`tools/patternhunt.py --corpus DIR`), then read the
source that produced it. Many "impossible" shapes were solved there already.

## 8. When to stop customising

Customise when a pattern repeats. A one-off puzzle is cheaper to solve by hand and log. Signs that a customisation
is worth it: the same diff shape in five or more functions, or a fix you just made by hand for the second time.
