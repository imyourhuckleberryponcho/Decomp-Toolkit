# 06 - Verification: a fuzzy 100% is not proof
Applies to: **universal**. The examples are PowerPC; the pitfalls (normalised labels, interchangeable constants, jump tables, twin helpers) exist on every platform.

Every project eventually ships a "100% matched" function that is wrong. Know the ways scores lie.

| Pitfall | What happens | Check |
|---|---|---|
| Anonymous constants `@N` are interchangeable in diff tools | `0.5f` loaded instead of `1.0f` still scores 100% | compare constant **values** per function (`fcheck.py` CONST) |
| Local branch targets are normalised | `break` vs `continue` vs `return`, or a statement inside vs outside a loop shows as 99.9% with an empty diff | `reldiff.py` (branch targets as offsets from the function start) |
| A twin helper | a function copied from a twin scores 100% while calling the twin's helper | `fcheck.py` CALLS, `callorder.py` |
| Jump tables are data | a wrong `case` label scores 100% on code | `jtcheck.py` (each entry reduced to the *rank* of its target block) |
| Data-section scores | table bytes with relocations are masked | DATA check; the executable hash |
| Dead stripping and link order | units "match" individually, the hash fails | executable hash |
| Label-only diffs "don't matter" | two constants with identical values referenced in a different order break the hash | normalise labels, never delete diff lines containing `@` |
| Function statics / string pools | numbered differently or placed after a file static | LAYOUT + the hash |
| Per-function scores of a *different compile* | an old object compared | always compile fresh in a scratch folder |

Rules that follow:

1. Normalise label numbers (`@9` vs `@11`) when diffing; never hide lines containing `@` - a swapped constant load once
   hid there.
2. Gate commits on the **exit code** of the full build (`ninja`), which includes the executable hash check.
3. Compare `report.json` against a baseline taken at session start: no previously exact function may drop.
4. For headers/refactors that should not change codegen: compile the touched files before and after and compare the
   **objects** byte for byte (`objsame`-style check), not just the final hash.
5. Treat any permuter or search result as a *hint*; read the diff and rewrite it naturally.
6. After a prototype or type fix, re-check the other (already exact) callers in the file - exact functions are often
   tuned around a wrong declaration.

## Register-only is not always register-only

A diff that shows only different register numbers can hide a wrong value: the same opcode list where an operand is a
*different value* (another variable, another field, `x` vs `&x`). Categories worth scanning for: call arguments that come
from somewhere else than in the target (`argsrc`-style), signed/unsigned pairs, truncation (`extsh`/`clrlwi`) on one side
only, shadowing declarations, inverted conditions. See the draft-bug list in chapter 03 (section H).

## A "frame first" rule

Compare the `stwu r1,-N(r1)` frame size before any other analysis; a different size is a structural source
difference (`framescan.py`).

## Measure the real difference, not the line count

Applies to: universal. `tools/shapedist.py` ignores everything that follows from register allocation (register
names, stack-slot offsets, `mr` copies, saved-register spills, frame size, anonymous-versus-named symbols at the same
bytes) and counts what is left. Zero = a colouring puzzle; non-zero = a real source difference. A section anchor
(`...data.0 + off`) against the target's named symbol at the same address is *not* a difference: the object links
identically. Details and the other value checkers: chapter 12.

## Prove an edit is neutral

Applies to: universal. `tools/objsame.py FILE...` compiles the file from git HEAD and from the working tree with the
real compile command and compares code, relocations and all section bytes; no full build needed. Use it for portability
edits, header clean-ups and refactors. Generated names that embed the source file name (anonymous namespaces,
`__sinit_<file>`) are normalised.

## Value checkers (the idea)

Applies to: universal in concept. Scores see code *shape*; bugs are *values*. Each of these found real draft bugs behind
98-99% scores: call arguments whose value comes from elsewhere than in the target; how call results are used (stored,
compared, truncated); memory stores to another offset or width; a source register traced back to its producing
instruction and compared position by position when the opcode lists are equal; sign/width pairs; shadowing
declarations; inverted conditions; bool locals used as numbers; a loop counter narrower than its bound; a pointer
result cast to a narrow integer. Check each hit in the source before changing anything: some are legitimate
scheduling differences.

## Scoring dips are not always regressions

Applies to: universal. When a draft is made more faithful (the real width, the real operand order) its fuzzy score can
fall by 0.1-2%, because the registers then spill differently. Accept it when the real shape distance shrinks and the
value behaviour equals the target's; reject a "do-nothing" helper that merely lifts the score.
