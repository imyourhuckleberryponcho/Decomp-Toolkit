# 12 - Triage by real difference, the lab method, and learning from your own history

Applies to: **universal** (any decompilation project, any compiler or platform), except where an item says
"GameCube (MWCC / PowerPC)". The examples are PowerPC because that is where they were measured.

This chapter collects the habits that made the biggest difference once the easy functions were done and thousands of
near misses were left.

## 1. Triage by the real difference (universal)

A near miss is one of two very different things:

- a **source difference**: a missing statement, another constant, another branch, another operation, a value read
  from the wrong place;
- a **colouring puzzle**: the same instructions, but the compiler gave a value another register, a stack slot another
  place, or coalesced a copy differently.

The first kind is fixed by thinking. The second kind is fixed by (cheap) search, or parked. Mixing them up wastes days.
So measure the *real* difference before touching anything:

`tools/shapedist.py UNIT FUNC [--show]` compiles the unit, takes the diff, and throws away everything that follows
from register allocation: register names, stack-slot offsets, plain register copies, saved-register spills and
reloads, multi-register saves, the frame size, and the difference between an anonymous section symbol and a named
symbol at the same bytes (those link identically). What is left is the real shape distance.

- `0` -> a colouring puzzle. Run the register search (`tools/search/regsolve.py`), then the permuter, then park it.
- `> 0` -> read the remaining lines with `--show`: they are the statements you got wrong.

Lesson learned: simpler distances (register-independent line counts) still counted stack-slot shifts and `mr` copies,
so a large share of the "structural" near misses were puzzles in disguise, and some "puzzles" hid real value bugs.
Both are found faster with the right measurement.

Companion scans that ask "does it read a **wrong value**?" even when the opcode list is identical (universal in
concept; written for PowerPC asm in this repo):

- every source register is traced back to the instruction that produced it (reaching definitions, looking through
  register copies) and compared position by position with the target. A different producer means a different value:
  `x % mod` where the game computes `rem % mod`. (Idea only: "opsrc".)
- call arguments that come from somewhere else than in the target, call results used differently, memory stores to
  another offset. (`tools/argextra.py` covers part of it; "argsrc / retcheck / storecheck" are the fuller versions.)
  These checkers found real draft bugs behind 98-99% scores on their first runs. Build the ones your drafts need.

A commutative operation with its operands the other way round (`add r3,r3,r0` against `add r3,r0,r3`) is not a
register-allocation result either: it is the order the expression was *written* in or canonicalised to. Register
searches cannot fix it; look for the source spelling that puts the other operand first.

## 2. The lab method (universal)

When a rule is unknown ("when does the compiler drop this compare?", "which of these four spellings gives the extra
load?"), do not experiment inside the real function. Write the smallest file that shows the pattern and compile every
variant at once:

```
int f(int x) { switch (x) { case <<<0|||1>>>: <<<g();|||>>> break; default: break; } return x; }
```

`tools/labgrid.py template.cpp --unit game/menu` compiles every combination of the `<<<a|||b>>>` alternatives with the
unit's real flags and groups them by identical code. In seconds it tells you which forms matter and which are noise,
and `--vars` prints the register of each variable. Typical results (GameCube / MWCC, measured):

- a `switch` with exactly one do-nothing case (that case being the same as `default`) emits `cmpwi K; b default`
  and drops the `beq`; with two do-nothing cases the `beq` stays. An empty `if` around a debug macro vanishes
  completely.
- a top-tested loop `while (c) step;` is rotated (test at the bottom). The target's top-tested loop with a reload is
  `for (;;) { if (c) break; step; }`.
- `memcpy(&a[1 + n], ..)` folds the `+1` into the index, `&(&a[1])[n]` keeps the `+4` after the scaled index.
- helper boundaries (inline function versus statements) can be ruled out in the lab before rewriting a big function.

Write each finished lab result in your puzzles file with its template, so nobody re-runs it.

## 3. Ordering as a search signal; modelling the allocator (GameCube / MWCC, idea universal)

For register-only near misses, "number of differing lines" is a coarse score. A finer one: split the function into
*values* (def -> uses over the control-flow graph), build the interference graph, and ask, for each pair of
interfering values, which one the compiler coloured first (the lower scratch register wins; the saved registers are
claimed from the top downward). The set of such orderings is the "colouring order". Score = orderings the target has
and we lack + the reverse + values in another register. Zero means the colouring is the same.

Uses that paid off:
- a score of 0 with a remaining diff proves it is *not* a register problem (see the commutative-operand note above);
  stop wasting search time there;
- as a **tie breaker** in a source-edit search (`regsolve --order`): among candidates with equal diff counts, prefer
  the one closer in colouring order. This found exact matches the plain line count could not rank;
- jump tables must be handled (every labelled block after an indirect branch is a successor).

Honest limits: replaying the colouring algorithm from the code order predicted only ~71% of the values and ~54% of
the functions on 600 exact functions (calls-crossing values 52%). The scheduler moves definitions, the saved-register
ranking has no single rule, and a removed late use leaves invisible interference. It is a *score*, not a predictor.
(Not included as code here; the idea is in this chapter.)

## 4. Learn from your own history, and know when to stop (universal)

Keep every attempt: which edit, which function, before/after score. After a few thousand results, look at the data.

**Positive result.** Counting which kinds of edit led to exact matches (declaration moves, type/width changes,
constant forms, temporaries) showed that nearly all exacts came from *single* small edits of a few kinds. That is
what a scripted source search (`tools/search`) should try first, before any random permuting.

**The permuter is finite.** Classifying ~60,000 permuter improvements by edit kind and by the function's current
register-problem shape showed: on functions that are *still* register-only after earlier rounds, improvements were
tiny (0 to 0.5% mean gain) and only a handful ended in an exact match; the ~150 exacts all came from functions that
were not (or no longer) in that pile. Consequence: stop giving long unattended permuter hours to the same
register-only leftovers; spend the hours on new tricks, new functions or fixing the shared helper behind a family.

**A negative result worth recording.** A gradient-boosted ranker trained on 18,000 compiled candidate edits (features:
edit family, edit count, type words, and function statistics such as instruction count, calls, loops, saved
registers, the shape class) was evaluated with cross-validation *by function*. On held-out functions it ranked the
best real improvement no better than a random order (mean rank percentile 0.023 vs 0.015 for random; top-10 hits
27 vs 31). Edit *kind* plus function *statistics* does not predict which edit works; the information is in the
specific source text, which needs a different model. Do not repeat this experiment with the same features.

**Mining fix history into rules.** Convert past fixes (1-3 line, behaviour-preserving diffs) into replayable rules;
replaying all past fixes through the current trick list tells you which fixes are covered (regression test for the
tricks) and which kinds of fix are *not* covered (the tricks still to write).

## 5. Layout blockers: sweep them, then link (universal in concept)

A unit with all functions exact can still fail to link: function order, data symbol offsets, constant pool order,
bss sizes. Run the layout/data checker over *every* unlinked unit once (`fcheck` already prints LAYOUT / CONST / DATA)
and tabulate the kinds of blocker per unit. Units whose only blockers are layout are the cheapest wins; units with one
unfinished function plus a clean layout link the moment that function is exact.

Rules found this way (GameCube / MWCC):
- **vtable order**: vtables come out in the reverse order of each class's first non-inline virtual function. If the
  target's vtable order is the reverse of the destructors' order, the destructor was declared first among the
  virtuals.
- **deferred files** (`-inline deferred`): function *and* constant numbering follow the reverse order; an inline helper
  with a function-static table must be defined near its LAST use (the static's number comes from where it is
  generated); a map file's UNUSED entries tell you where placeholder statics go.
- a constant pool order can depend on assignment order alone; moving the declaration may cost score.
- a 0x10-byte tail gap before a 32-aligned object is linker alignment: shrink the split, do not add data.

## 6. Prove an edit changed nothing (universal)

`tools/objsame.py FILE...` compiles the file from git HEAD and from the working tree with the real compile command and
compares code + relocations + all section bytes. Use it for every edit that is *supposed* not to change the original
build (porting `#ifdef`s, header refactors, renames), also while a long automated run is using the build folder. For
header edits save the old objects first (`--save NAME`), then `--compare NAME` after the edit.

## 7. If you will run the decompiled code (universal)

Once the source is compiled for another platform and executed, **behaviour bugs in 98-99% drafts surface** that no
diff tool shows (a wrong argument, a truncated local, a missing statement that scores 99.9%). Treat it as a second
test oracle:

- functions that the port actually executes get priority over functions it never calls, because a draft that runs is a
  draft that is *tested*;
- every fix found by running goes back into the matching build as a value-faithful rewrite (check it with
  `objsame.py` and the diff tools: the score may dip a fraction of a percent while the shape gets closer, which is fine
  when the real distance (section 1) shrinks);
- keep a list of "faithfulness" bugs by kind (wrong width, wrong accumulator, shadowed variable, bool used as number,
  wrong store offset) and write a scanner for each kind that appeared twice.

## 8. Layouts for a port: many views of one class (GameCube-specific problem, idea universal)

Decompiled files often declare their own partial view of the same class at GameCube offsets. On a platform with
different pointer size or alignment those views disagree. Scan *every* class that appears in more than one file and
compare each view's field offsets and sizes on the new platform (a "gclayout" style table: field, GameCube offset, PC
offset, per file). Only offset mismatches are real bugs; a size-only mismatch of a short placeholder view is harmless
unless something embeds or copies it. See `09-portability.md`.
