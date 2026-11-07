# 04 - Whole-file causes: flags, pragmas, deferred inlining, function order

If **every** function of a file is close but none is exact, suspect the file's build settings before the code.

## Symptom -> setting

| Symptom in the target | Likely cause |
|---|---|
| `stmw`/`lmw` where you get separate `stw`/`lwz` | file built size-optimised: `-O4,s` (or `#pragma optimize_for_size on`, close but not always identical) |
| `subic/subfe` (+ `clrlwi`) for `!= 0` instead of `neg/or/srwi` | `-O4,s`. In such a file `x ? 1 : 0` with a `u8`/`bool` return gives the trailing `clrlwi` |
| Unmerged `srwi` + `clrlwi` (instead of `extrwi`), `clrlwi` + `cmplwi` (instead of `clrlwi.`), unscheduled prologue (`stw r0,0x14(r1)` late, `li` before it, no `stwu`/`mr.` merges) | `#pragma peephole off` (common in debug/tool code) |
| `mr r0,r3; li r3,X; mr r4,r0` around a call result (peephole off) | store the result in a typed local first |
| `const` local struct table + peephole off: every `r = tbl.r` loads into a temp and copies | non-const locals load straight into the variable |
| No `fmadds` anywhere | `#pragma fp_contract off` |
| `_savegpr_N` / `_restgpr_N` calls | `#pragma use_lmw_stmw off` (often together with `-opt level=0`) |
| Loop invariants not hoisted, registers in declaration order | lower `optimization_level` for the file/function (`tools/` level sweeps) |
| Functions come out in reverse order, constants numbered "backwards", static data reversed | `-inline deferred` (see below) |
| Small functions called instead of inlined, or inlined where the target calls | `-inline` mode: `auto`, `noauto`, `off`, `deferred`; `#pragma dont_inline`, `auto_inline` |
| A string is `@N` where yours is `@stringBase0` | `#pragma pool_strings off` |
| `#pragma` on a per-function basis has no effect in a deferred file | in `-inline deferred`, function-level `peephole` pragmas are ignored and deferred bodies are compiled with the pragma state at **end of file** |

Per-library base flags in `configure.py` differ (SDK libraries often have their own); look at the real ninja command
(`ninja -t commands build/<ID>/src/<unit>.o`) before assuming. Check a new flag against more than one function: a
file-wide `-O4,s` can be wrong if `#pragma optimize_for_size off` at the top improves some functions and changes none.

## `-inline deferred`

Deferred inlining compiles function bodies *after* the whole file is parsed. Observable effects:
- functions are emitted in **reverse** source order, and `.bss`/`.sbss` data in reverse order of definition, while
  initialised tables follow parse order;
- local aggregate initialisers (`GXColor c = {..}`, `u8 tbl[3] = {..}`) get the **lowest** `@N` numbers and sit at the
  start of `.sdata2`/`.rodata` before every float constant, even if their functions come late;
- functions defined *later* can be inlined into earlier ones;
- a `#pragma pop` / `dont_inline on` left at end of file stops inlining of the deferred functions.

How to spot a deferred file: constant numbering / pool order wrong although values are right; function order exactly
reversed (`tools/orderscan.py` prints `REVERSED`); `tools/deferscan.py` looks at initialiser constants. How to write
one: write the file in the *reverse* of the asm order (`tools/movefn.py --asm ... --reverse`,
`tools/revfuncs.py`), add `-inline noauto,deferred` (or `deferred`) to the object's `extra_cflags`, and fix up
declarations with `tools/hoistdecl.py` (it adds prototypes for functions used before their definition).

Related partial form: `#pragma defer_codegen on` before the first function whose jump table/data must follow the
vtable, through to the end of file, with later definitions in reverse order. Weak inlines of other classes that the
target has at the very end become plain non-inline definitions there.

Many SDK libraries (MSL, Runtime, TRK) and engine-library code (HSD baselib, C mode, `-lang=c`) are deferred files.

## Function order and constants

- The order of functions inside a unit decides constant numbering and the string/float pool layout. Compare it with
  the original *before* fighting pool diffs.
- Constants are numbered when created: aggregates and function statics at parse time, float literals at code
  generation. A deferred file whose first-numbered constant belongs to a later function means an unused (stripped)
  function was generated first: put a **placeholder static function** with those constants at that position. The
  linker removes it again (unreferenced), so judge such files by the final executable hash.
- An unused static function's string literals still land in `.rodata` (with `pool_strings off`): a placeholder can
  also supply strings that sit between two used groups.
- A map file of any build of the game (a debug or demo build, if one exists) lists dead-stripped functions and
  constants as `UNUSED` in their original order: that is the best source for where placeholders go.

## C++ specifics

- `extern "C" void f() {...}` as a prefix on a *definition* makes MWCC emit `f` one function later than its position;
  put C-linkage definitions inside an `extern "C" { ... }` block.
- A function-local `static` with an initialiser in C++ gets a runtime guard byte (`init$N`): fine when the target has
  one, otherwise move it to file scope.
- A hand-written `static void __sinit_file_cpp()` gets **no** `.ctors` entry (the link fails although every function is
  exact). Let the compiler generate it: give the type a constructor (`static Vec v(0.0f, 0.0f, 6.0f);`).
- The vtable pointer sits where the first virtual function is declared (probe it rather than guess).
- A global with a dynamic initialiser goes to `.data` unless the type has a constructor; give it an empty one to land in
  `.bss`. A global initialised with a `stfsu` sequence is a class constructor with an init list.
- `__declspec(section ".ctors") extern void* const x = f;` for ctors/dtors entries (no `#pragma section`, no `$10`
  suffix; suffixed sections get dropped by the linker).
- Runtime `memcpy`/`memset` live in `.init`: `#pragma section code_type ".init"`.
- A member declared in a draft as `static` makes m2c shift all parameters.
