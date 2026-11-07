# Tools

Small Python 3 scripts (standard library only) for projects that use the **dtk-template** layout
(`configure.py`, `build/<VERSION>/{asm,obj,src}/`, `report.json`, `build/binutils`, `build/tools`, ninja).

## Setup

Run every script **from the project root** (or set `DECOMP_REPO`). The shared module `dtkproj.py` finds:

- the project root (`DECOMP_REPO`, else the nearest folder with `configure.py`);
- the version folder under `build/` (`DECOMP_VERSION`, else the only folder with `report.json`/`asm`);
- the binutils / dtk / objdiff-cli programs in `build/binutils` and `build/tools`
  (`OBJDUMP`, `DTK`, `OBJDIFF` override).

Scripts write scratch output below `build/scratch/` only. Set `PYTHONUTF8=1` on Windows. A unit is the path under `src/`
without extension, e.g. `game/menu`. The compile step uses the exact flags from `ninja -t commands`, so run
`python configure.py` once first.

Status: written on Windows against a real project and cleaned up for release. Scripts that only read
`build/<VER>/{obj,src,asm}` and `report.json` were exercised again on a stand-in project; the ones that compile
(everything that calls `fcheck.py`) need your compiler and were not re-run after the clean-up. Report or fix breakage
in your own copy - they are short.

## Checking one unit

| Tool | Solves | Usage |
|---|---|---|
| `fcheck.py` | The per-unit checker: compiles a unit (or `--src` scratch file) with its real flags into a private folder, scores every function with objdiff, then checks LAYOUT (function order, symbol offsets, sizes), CONST (values of anonymous `@N` constants per function), CALLS and DATA | `python fcheck.py game/menu [--all] [--diff func] [--tag T] [--extra "-O4,s"] [--mw GC/1.3.2]` |
| `reldiff.py` | Diff with branch targets as offsets from the function start - finds `break`/`continue`/`return` and in-loop/out-of-loop differences hidden by a 99.9% score | `reldiff.py <unit> <func>` or `--scan [--lo 97]` |
| `jtcheck.py` | Jump-table entries (ranked by target block) vs the target: wrong/missing `case` labels that score 100% | `jtcheck.py [--only U] [--all]` |
| `callorder.py` | Call sequence per function, target vs ours | `callorder.py <unit> [func ...]` |
| `structdist.py` | Distance of one function after replacing register names: 0 = register-only | `structdist.py <unit> <func> [--show]` |
| `shapedist.py` | REAL code difference: also ignores stack-slot offsets, `mr` copies, saved-register spills, frame size and anonymous-versus-named symbols; 0 = colouring puzzle only | `shapedist.py <unit> <func> [--show]` or `--batch Q.tsv` |
| `objsame.py` | Proves an edit left the object byte-identical (code + relocations + all section bytes) against git HEAD, or against saved objects for header edits | `objsame.py src/x.cpp [--rev R]`; `--save NAME` / `--compare NAME` |

## Finding what is wrong across many units (read report.json + objects)

| Tool | Solves | Usage |
|---|---|---|
| `regonly.py` | Classifies non-exact functions: register-only, order, immediates, structural | `regonly.py [--min 90]` |
| `framescan.py` | Functions whose stack frame size differs from the target (a real source difference) | `framescan.py [--min 90]` |
| `branchflip.py` | Inverted conditions: same code, opposite `bCC` | `branchflip.py [--min 80]` |
| `switchcases.py` | Switch case labels / compare constants present only in the target | `switchcases.py [--min 85]` |
| `argextra.py` | Calls where we set an argument register the target leaves alone (extra/lost args, wrong prototypes); `--consts` finds wrong constants/orders | `argextra.py [--strict] [--consts]` |
| `signscan.py` | Signed/unsigned and width pairs (`extsb` vs `clrlwi`, `srawi` vs `srwi`, `cmpw` vs `cmplw`) = value bugs | `signscan.py [--unit S]` |
| `bindscan.py` | Symbol binding differences (`static` vs global vs weak) | `bindscan.py [--all-units]` |
| `sdascan.py` | Extern data declared as a scalar where the target uses `lis/@l` (arrays, big objects) | `sdascan.py [--unit S]` |
| `orderscan.py` | Units whose function order differs from the original (REVERSED = deferred file) | `orderscan.py [--all]` |
| `deferscan.py` | Unlinked units that look like `-inline deferred` files (initialiser constants numbered low) | `deferscan.py` |
| `poolscan.py` | Constants with right values in the wrong order: one-function expression order, reversed, permuted | `poolscan.py [-j 8]` |
| `shadowbug.py` | Draft bugs: a local declaration that shadows a file static or an outer local that is read later | `shadowbug.py [--all] [path]` |
| `fnptrscan.py` | Callback / function-pointer parameter width mismatches that break on x64 (source scan, no build needed) | `fnptrscan.py [--src DIR] [--all]` |

## Searching for a fix

| Tool | Solves | Usage |
|---|---|---|
| `labgrid.py` | Lab: compile a small template with `<<<a\|\|\|b>>>` alternatives using a unit's flags; group results by identical code (or `--vars`: each variable's register) | `labgrid.py tmpl.cpp --unit game/menu [--func F] [--want REGEX] [-j 8]` |
| `srcvariants.py` | Try hand-written source variants of one unit; prints the functions whose score changes; restores the file | `srcvariants.py <unit> variants.py [--func F] [--struct]` |
| `regplan.py` | Which VARIABLE sits in the wrong register (from `-sym on` debug info) and a declaration order to try | `regplan.py <unit> <func> [--try]` |
| `regwhere.py` | Which SOURCE LINE defines each misplaced register value (DWARF line table) | `regwhere.py <unit> <func> [--src F]` |
| `patternhunt.py` | Regex over other decomps' matching asm (same compiler), shows the source of each hit | `patternhunt.py 'regex' --corpus DIR[=SRCDIR] [--src]` |

## Moving code around

| Tool | Solves | Usage |
|---|---|---|
| `movefn.py` | Move a function definition (with its `extern`/pragma wrappers) before another, or reorder a file to the asm order (`--reverse` for deferred files) | `movefn.py FILE --asm build/<VER>/asm/<unit>.s [--reverse]` |
| `hoistdecl.py` | After `movefn --asm`: make the reordered file compile again (moves declarations up, adds prototypes) | `hoistdecl.py <unit> <src>` |
| `revfuncs.py` | Reverse the order of function definitions from a given start to the end (deferred files) | `revfuncs.py FILE "first function signature"` |

## Ideas described in the guide only (no code here)

Ordering score for register colouring (`regorder`/`regmodel`), statistics over permuter history (`permlearn`), unit layout
blocker sweeps (`layoutsweep`/`onelayout`), per-register value tracing (`opsrc`), argument/return/store value checkers
(`argsrc`/`retcheck`/`storecheck`), class view/offset tables for ports (`gclayout`/`viewsweep`/`pcsizes`). They depend
on project data (histories, lists of linked units); chapter 12 explains how to build each.

## Notes

- `regplan`/`regwhere` need `dtk dwarf dump` and an `-sym on` compile; they only work for functions that the compiler
  keeps debug info for.
- The scripts match the dtk-template naming of 2024 and later (`build/<ID>/obj`, `build/<ID>/src`, `.fn ... .endfn`
  asm blocks, `Object(NonMatching|MatchingFor("ID"), "path.c")` in `configure.py`). Newer templates that spell the
  status `Matching` need a one-line change to the regex in `bindscan.py`/`deferscan.py`/`patternhunt.py`.
- Output files go to `build/scratch/*.md` or `*.tsv`.
