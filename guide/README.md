# Matching decompilation guide (GameCube / Wii, Metrowerks CodeWarrior)

A handbook for **byte-matching decompilation**: writing C/C++ that the original compiler turns into the exact same
bytes as the retail game. It is written for people using the usual toolchain (decomp-toolkit "dtk", objdiff, ninja,
decomp-permuter, m2c), mostly with MWCC 2.x ("GC/2.6" style compilers), but the verification and workflow chapters
apply to any decompilation project. Every file starts with an `Applies to:` line: **universal** or **GameCube / MWCC**; the top-level README has the full scope table.

Everything here was learned the hard way on a large project. The examples are short and use neutral names.
Compiler behaviour is described as *observed* with specific compiler builds; always confirm with your own lab
(see `tools/labgrid.py`) before relying on a rule.

## Chapters

| File | Covers |
|---|---|
| [01-setup-and-workflow.md](01-setup-and-workflow.md) | Toolchain, layout, the core loop, what to attack in which order |
| [02-compiler-model.md](02-compiler-model.md) | How MWCC allocates registers, orders evaluation, lays out stack and sections |
| [03-codegen-cookbook.md](03-codegen-cookbook.md) | Symptom -> source fix recipes with before/after code |
| [04-flags-pragmas-and-files.md](04-flags-pragmas-and-files.md) | Whole-file causes: flags, pragmas, deferred inlining, function order |
| [05-data-and-linking.md](05-data-and-linking.md) | Splits, padding, dead-stripping, constant pools, tables, vtables |
| [06-verification.md](06-verification.md) | Why a fuzzy 100% is not proof, and the checks that are |
| [07-permuter-and-search.md](07-permuter-and-search.md) | Using decomp-permuter and scripted source search well; the near-miss workflow |
| [08-reference-decomps.md](08-reference-decomps.md) | Porting from sibling projects and the SDK libraries |
| [09-portability.md](09-portability.md) | If you later compile the decompiled source for PC: what breaks and how to avoid it |
| [10-automation-tips.md](10-automation-tips.md) | Scripting and batch-work tips |
| [11-customizing-for-your-game.md](11-customizing-for-your-game.md) | Teaching the tools your engine, SDK and fix history: most wins come from this |
| [12-triage-and-learning.md](12-triage-and-learning.md) | Real-difference triage, the lab method, ordering scores, learning from history (including a negative result), layout sweeps, byte-identity proofs |

## Five rules that matter most

1. **Only the final binary is the truth.** Per-function scores lie in specific ways. Finish every unit with the
   full-executable hash check.
2. **Cheapest first.** Twins and reference ports cost minutes; fresh functions cost hours; register puzzles can cost
   days. Park puzzles.
3. **Flags before code.** A file where every function is "almost right" usually lacks a flag or pragma.
4. **Write natural code.** No inline asm (unless the original was asm), no byte blobs, no dummy variables to trick
   the allocator. Tool "wins" are hints: rewrite them into code a human would have written.
5. **Commit only verified states.**
