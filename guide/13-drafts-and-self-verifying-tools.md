# 13 - Better drafts first, and tools that verify themselves
Applies to: **universal** (the order of work, the acceptance rules); the draft repairs are **m2c / PowerPC / MWCC**.

## 1. Look at the drafts before you search

A permuter or source search moves a nearly right function to exact. It cannot repair a function with no body, a switch
that became a chain of ifs, or a body that lost a third of its statements because they did not compile. On one project a
24 hour search had added five exact functions, none of them in the last 14 hours, because so many functions still had an
empty or badly damaged body. Redoing the drafts, with prototypes for the whole program, then gave several hundred exact
functions in a few hours of machine time.

So, before a long run, tabulate the score bands of your unfinished functions. Many functions below 10% means "fix the
drafter"; a hump at 60-90% means "fix a systematic draft mistake"; a hump at 97-99.9% means "now search".

## 2. Why drafts are worse than they need to be

| Symptom in the draft | Cause | Cure (`tools/draft/redraft.py` does these) |
|---|---|---|
| empty body, or many statements commented out | the drafter gave up after N compile errors | read the compiler's message and TRY casts on the sub-expressions of that statement, keeping the one that moves the error on; comment out only as the last resort |
| `goto` soup or compare chains instead of `switch` | the tool never saw the jump table | hand it the table from the data section |
| `M2C_ERROR`, `==` where the original has `>=` / `<=` | the disassembler's spelling of `cror` is not the one the tool knows | rewrite the operand spelling before drafting |
| float constants reloaded after every call or store | the constant is declared `extern` | give the tool the constant pool, so it writes literals |
| stores to locals vanish | an array was split into separate locals, so the stores are dead | join an address-taken stack slot with the slots behind it |
| `t = f(); g(a); h(t)` where the original nests the calls | callee prototypes unknown: one register off | whole-program prototypes as context (section 3) |
| `void *` arithmetic refused, or off by a factor of 4 | pointer type | byte pointers for byte offsets |

Compile every combination of these and keep the best score per function: no single choice wins everywhere.
Accept a draft that still drops statements only if it is clearly better; a high score on incomplete code misleads later.

## 3. Whole-program prototypes (`tools/draft/protos.py`)

One function at a time, a decompiler cannot know whether the callee uses r3 or whether it returns something. The whole
program knows:

- **Parameters**: an argument register is a parameter when some path reads it before writing it. "Passed on unchanged to
  a callee that uses it" counts as a read, so iterate over the call graph until nothing changes.
- **Results**: a function returns a value when some caller reads r3 / f1 after the call. Drop a result that is live at
  the function's own entry only because it was assumed.
- **Types** are the weak part (m2c's own view is good enough); the *number* of parameters and the *existence* of a
  result are what fixes the code.

The same table finds wrong hand-written prototypes: compare it with your headers.

## 4. Acceptance rules for tools that edit the source

A tool whose findings wait for a person is a tool whose findings are lost. A tool that installs on "score went up" fills
the project with wrong code. The way out is to let tools install, with rules that make a wrong install unlikely:

1. **Identical beats everything.** If the function's instructions are identical (`unitlib.strict_exact`: registers,
   immediates, call and branch targets), its behaviour is identical. Install, unless the text itself is a hack.
2. **Better is not proof.** Below 100%, install only changes that are *provably* right for an independent reason: the
   constant the target loads; the parameter that arrived in that register, with no call before that could clobber it and
   a callee that really reads it. Everything else is a hint.
3. **Nothing else may move**: no other function of the unit lower, no new wrong constant, no new wrong call.
4. **A partial fix must reduce the thing it fixes.** Swapping two constants between two calls can gain 0.01 and fix
   nothing; count the flagged registers before and after.
5. **Natural text only**: no volatile, no goto, no comma tricks, no `if (1)`, no uninitialised reads (check the code,
   not the variable name: a renamed trick is still a trick), no assignments inside expressions.
6. **Remember what was tried**, keyed by the source it was tried on, so a rerun costs nothing; when the *tool* or its
   *rules* change, bump a version so everything is tried again.
7. **Reversible**: start from a clean tree, restore on failure, commit between stages.

Tools here built on these rules: `argrepair.py` (wrong literal arguments), `permuter/permfix.py` (permuter matches made
natural), `draft/redraft.py`, `refs/refport.py`, and `runner/autoinstall.py` for the overnight runner's package.

## 5. A session that uses them

```
python tools/refs/refcorpus.py                  # compile the reference projects (cached)
python tools/refs/refmatch.py                   # what of it is in your game?
python tools/refs/refport.py                    # import it, verified; then --close
python tools/draft/protos.py                    # whole-program prototypes
python tools/draft/redraft.py                   # redo weak drafts
python tools/argrepair.py                       # wrong literal arguments
python tools/learn/refmine.py && python tools/learn/fixrules.py build
python runner/runner.py run --config my.toml --hours 8
python runner/autoinstall.py --latest           # or --watch 30 in a second window while the run works
python tools/permuter/permfix.py
python tools/regclass.py --full100              # nothing that looks finished may be fake
ninja
```
Commit after each step that changed something. Library imports and redrafts change the split / whole bodies, so run them
while nothing else is building; the rest can run beside a search.

## 6. When the run goes flat

Plot exact functions per hour. Flat for several hours means the queue is parked or the stages are exhausted, and more
hours will not help. Then, in this order: new reference projects (or newer revisions), new draft repairs, new rewrite
rules (then re-open parked functions), and only then more permuter time.
