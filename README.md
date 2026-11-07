# Matching-decompilation toolkit

Know-how and helper scripts for people doing **byte-matching decompilation** of GameCube / Wii games compiled with
Metrowerks CodeWarrior (MWCC), using decomp-toolkit (dtk), objdiff, ninja and decomp-permuter. Much of the guide
(workflow, verification, triage, search strategy, portability) applies to **any** decompilation project (N64, GBA, PS2,
x86 ...); each file says which parts are universal and which are GameCube-specific (see the scope table below).

It is a collection of things that were learned while matching a large game: why a register ends up where it does,
which source shape produces which instruction pattern, what makes a unit link or not, and small scripts that
automated the boring parts. It is not a framework; take what helps.

## Who it is for

- You have a dtk-template project and are stuck at 95-99% on functions, or on files that "almost" link.
- You want a checklist of things to try before spending hours, and a way to confirm that a "100%" is really right.
- You plan to compile the decompiled source for PC later and want to avoid the traps early.

## Quick start

1. Read `guide/01-setup-and-workflow.md`, then `guide/12-triage-and-learning.md`; keep `guide/03-codegen-cookbook.md`
   open while you work.
2. Copy `tools/` somewhere (or add it to `PATH`), and run from your project root:
   ```
   python tools/fcheck.py game/menu          # per-function scores + layout / constants / calls / data checks
   python tools/regonly.py --min 90          # which near misses are register-only?
   python tools/framescan.py                 # frame size differences = real source differences
   python tools/shapedist.py game/menu myFunction --show   # the REAL difference, ignoring registers and stack slots
   python tools/objsame.py src/game/menu.cpp # did my edit leave the object byte-identical?
   python tools/labgrid.py lab.cpp --unit game/menu   # which of these source forms change the machine code?
   ```
   See `tools/README.md` for the full list and for how project paths are found (`DECOMP_REPO`, `DECOMP_VERSION`).
3. If you use decomp-permuter, look at `permuter/` for a patch with a shared memory of tried candidates and safer passes.

## What applies to you (scope table)

Every guide file and tool carries a scope. **Universal** = works for any decompilation project on any platform and
compiler. **GameCube / MWCC** = specific to CodeWarrior and PowerPC (register habits, small-data pools, deferred
inlining, `psq`, dtk/objdiff configuration). If you work on an N64, GBA, PS2 or x86 project, read the universal parts
as they are and use the GameCube parts as *examples of what to model*; `guide/09-portability.md` ends with a table of
what to replace.

| Guide file | Scope |
|---|---|
| 01 setup and workflow | universal workflow; toolchain table is GameCube / Wii (dtk) |
| 02 compiler model | GameCube / MWCC |
| 03 codegen cookbook | GameCube / MWCC (section H, draft bugs, is universal) |
| 04 flags, pragmas, files | GameCube / MWCC (the "whole-file cause" idea is universal) |
| 05 data and linking | universal (splits, padding, hash); GameCube parts: small-data pools, `@N` constants, vtable order |
| 06 verification | universal |
| 07 permuter and search | universal strategy; edit families tuned to MWCC |
| 08 reference decomps | universal |
| 09 portability | universal (porting to a modern 64-bit platform) + what to replace for other platforms |
| 10 automation tips | universal |
| 11 customizing for your game | universal |
| 12 triage and learning | universal (real-difference triage, lab method, history mining, honest negative results) |

| Tool | Scope |
|---|---|
| `fcheck`, `objsame`, `shapedist`, `labgrid`, `srcvariants`, `patternhunt`, `movefn`, `hoistdecl`, `shadowbug`, `fnptrscan`, `runner/` | universal in design (they use dtk-template paths; `shapedist` and a few scanners contain PowerPC register patterns you can swap) |
| `regonly`, `regplan`, `regwhere`, `structdist`, `reldiff`, `argextra`, `signscan`, `branchflip`, `callorder`, `switchcases`, `jtcheck`, `framescan` | PowerPC assembly syntax (easy to adapt: the register-name and mnemonic regular expressions are at the top) |
| `deferscan`, `poolscan`, `sdascan`, `orderscan`, `revfuncs`, `bindscan`, `search/regsolve`, `search/tricksweep` | GameCube / MWCC behaviour |
| `permuter/`, `tools/permuter/` | universal permuter drivers; the changes are tuned for MWCC |

## Make them fit your game

The tools are generic, and they work much better once they know your project: your engine's accessors and macros,
your SDK's helpers, your draft tool's habits, your files' compiler settings and your own fix history. Read
`guide/11-customizing-for-your-game.md`; `examples/my_game_tricks.py` is a plug-in template for the source search.

## Map

| Folder | Contents |
|---|---|
| `guide/` | the handbook: workflow, a model of MWCC register allocation / evaluation order / stack layout, symptom -> fix recipes, flags and deferred-inlining files, data and linking, verification pitfalls, permuter use, reference decomps, PC-port portability |
| `tools/` | stand-alone scripts (Python 3, standard library) plus `tools/README.md`; `tools/search/` (regsolve, tricksweep) and `tools/permuter/` (permuter drivers) |
| `runner/` | configurable long-run (overnight) runner: stages, lanes, parking, morning report; results go to a review folder, never into your source |
| `examples/` | a trick plug-in template (`my_game_tricks.py`) |
| `permuter/` | improvements to decomp-permuter as a patch against upstream commit `8556c81`, and a README explaining each |
| `LICENSE` | MIT (third-party notice for decomp-permuter included) |

## License

MIT (see `LICENSE`); the permuter patch keeps the upstream notice in `permuter/LICENSE-permuter-upstream`.

## Caveats

- Compiler behaviour is described as *measured* on MWCC GC 2.x builds; other versions may differ. Run a quick lab
  (`tools/labgrid.py`) before relying on a rule.
- The scripts were developed on Windows against one project and then made generic. Read a script before you run it on
  your tree; they only write below `build/scratch/` and (for source-variant search) restore the source file afterwards.
- No game data, assets or disassembly are included.

Contributions and corrections are welcome.
