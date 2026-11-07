# 06 - Verification: a fuzzy 100% is not proof

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
