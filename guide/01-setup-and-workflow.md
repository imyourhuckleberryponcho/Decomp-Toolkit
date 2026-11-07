# 01 - Setup and workflow
Applies to: **universal** (workflow, order of attack). The toolchain table is **GameCube / Wii (dtk, MWCC)**; replace it with your own platform's assembler, linker and diff tool.

## Toolchain

| Piece | Role |
|---|---|
| decomp-toolkit (`dtk`) + the `dtk-template` layout | splits the retail DOL/RELs into per-file objects and asm, generates the ninja build, links, checks the hash |
| Metrowerks CodeWarrior compilers (`build/compilers/GC/<ver>/mwcceppc.exe`) | the original compiler; picking the version and flags is part of matching |
| objdiff (`objdiff-cli`) | per-function diff and score; produces `report.json` |
| m2c | asm -> draft C. A starting point, never the answer |
| decomp-permuter | random source mutation to find an exact register/scheduling result |
| binutils (powerpc-eabi) | objdump / nm / objcopy for inspecting objects |
| sjiswrap | lets sources contain UTF-8 text that compiles as Shift-JIS |

```
python configure.py
ninja
```
`build/<ID>/main.dol: OK` means everything marked matching still reproduces the retail executable. Gate on ninja's
**exit code**, not on grepping its output (a no-op ninja prints nothing; grepping for "OK|FAILED" also matches failures).

## Layout (dtk-template)

| Path | Contents |
|---|---|
| `config/<ID>/symbols.txt`, `splits.txt` | every symbol (address, size, type, scope) and which address ranges belong to which source file |
| `configure.py` | per-object `Object(NonMatching / Matching, path, extra_cflags=[...])`, libraries, compiler versions |
| `build/<ID>/asm/<unit>.s` | target disassembly of each unit: your spec |
| `build/<ID>/obj/<unit>.o` | target object |
| `build/<ID>/src/<unit>.o` | your compiled object |
| `build/<ID>/report.json` | progress: per-unit, per-function `fuzzy_match_percent` |

Small practical notes: new source files are only noticed after re-running `python configure.py`; ninja may not track
header edits in custom include trees (touch the users); run dtk with repo-relative paths (it can mangle absolute
paths on other drives on Windows); set `PYTHONUTF8=1` for scripts that read sources with non-ASCII text; save a copy
of `report.json` at the start of a session as a regression baseline.

## The core loop

1. Pick a function. 2. Read its target asm; draft C. 3. Compile just that unit and diff (`tools/fcheck.py`).
4. Fix until 100% **and** the layout/constant/call/data checks pass. 5. Mark the object matching, run the full
build, confirm the hash and that nothing regressed. 6. Commit.

Keep unfinished units `NonMatching` while you iterate; link a unit only when every function in it is exact.

## Order of attack (cost per matched byte)

| Step | Typical cost | Result |
|---|---|---|
| 1. Twins: unmatched functions that are copies of matched ones | seconds | 100% after renames |
| 2. Flag/pragma sweep over unfinished files | minutes, scripted | sometimes a whole file links |
| 3. Reference ports (sibling game / SDK sources) | minutes to hours per file | often 90-100% |
| 4. Generators for repetitive accessor / table files | 10-20 min per file | whole files at once |
| 5. New code from asm | hours | 60-95% first pass |
| 6. Register / scheduling near misses (95-99.9%) | unbounded | batch with the permuter, park the rest |

**Twins.** Compare *opcode sequences* (registers and immediates ignored) of every unmatched function with every
matched one; at >= 0.95 similarity copy the matched source and rename. After a twin copy, check the **call
targets**: a twin can score 100% while calling the twin's helper instead of yours.

**Flags first.** If all functions in a file are slightly off, sweep the usual variants automatically:
`-inline deferred`, `-inline noauto`, `-inline noauto,deferred`, `-O4,s`, `-O4,p`, `-inline off`,
`#pragma peephole off`, `-opt level=0..3`. Recognise them by symptom (chapter 04).

**New files.** Write every function in asm order first so the file compiles and layout/data can be checked early;
choose flags; small functions before large; skip a function after ~5 failed ideas and come back.

**Near misses.** First the cheap known fixes (declaration order, `int` vs `s32`, typed temporaries, inline helpers
with early returns - chapter 03), then a time-boxed permuter run (chapter 07). Still stuck: park it with a one-line
note at the top of the file.

## Session routine

Baseline report -> twin finder -> flag sweep -> ports / generators -> verify + commit after **each** file -> write
down anything new you learned (a note file of "symptom -> fix" is the most valuable artefact a project produces).
