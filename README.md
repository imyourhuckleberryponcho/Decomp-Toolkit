# Matching-decompilation toolkit

Know-how and helper scripts for people doing **byte-matching decompilation** of GameCube / Wii games compiled with
Metrowerks CodeWarrior (MWCC), using decomp-toolkit (dtk), objdiff, ninja and decomp-permuter. Much of the guide
(verification, workflow, portability) applies to other decompilation projects too.

It is a collection of things that were learned while matching a large game: why a register ends up where it does,
which source shape produces which instruction pattern, what makes a unit link or not, and small scripts that
automated the boring parts. It is not a framework; take what helps.

## Who it is for

- You have a dtk-template project and are stuck at 95-99% on functions, or on files that "almost" link.
- You want a checklist of things to try before spending hours, and a way to confirm that a "100%" is really right.
- You plan to compile the decompiled source for PC later and want to avoid the traps early.

## Quick start

1. Read `guide/01-setup-and-workflow.md`, then keep `guide/03-codegen-cookbook.md` open while you work.
2. Copy `tools/` somewhere (or add it to `PATH`), and run from your project root:
   ```
   python tools/fcheck.py game/menu          # per-function scores + layout / constants / calls / data checks
   python tools/regonly.py --min 90          # which near misses are register-only?
   python tools/framescan.py                 # frame size differences = real source differences
   python tools/labgrid.py lab.cpp --unit game/menu   # which of these source forms change the machine code?
   ```
   See `tools/README.md` for the full list and for how project paths are found (`DECOMP_REPO`, `DECOMP_VERSION`).
3. If you use decomp-permuter, look at `permuter/` for a patch with a shared memory of tried candidates and safer passes.

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

## Caveats

- Compiler behaviour is described as *measured* on MWCC GC 2.x builds; other versions may differ. Run a quick lab
  (`tools/labgrid.py`) before relying on a rule.
- The scripts were developed on Windows against one project and then made generic. Read a script before you run it on
  your tree; they only write below `build/scratch/` and (for source-variant search) restore the source file afterwards.
- No game data, assets or disassembly are included.

Contributions and corrections are welcome.
