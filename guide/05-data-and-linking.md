# 05 - Data, splits and linking

Code can match 100% and the unit still will not link. These are the data-side reasons.

## Splits and symbols (config files)

| Problem | Fix |
|---|---|
| A 4-byte `fn_XXXX` that is just `blr`, right after a function ending in an unconditional `b` | the original's dead trailing `blr` was split off as a function; grow the previous symbol by 4 in `symbols.txt` and delete the fake one |
| A trailing `gap_*` object of zeros (e.g. 0x1C) at the end of a unit's `.data`, and the next unit's first object is 32-byte aligned | linker **alignment padding**, not data: shrink the unit's section end in `splits.txt` |
| A trailing small `gap_*` in `.sbss`/`.sdata2` | alignment padding; never add filler variables |
| `fn_XXXX` names | rename in `symbols.txt` to the real (mangled) name; the asm regenerates |
| Anonymous struct names (`@class$515File_c`) | rename the *target* symbol to your compiler's name so objdiff pairs them (local to your project; the binary is unaffected) |
| `force_active` in `symbols.txt` did not keep a function | use `#pragma force_active on` in the source |
| A table symbol a few bytes larger than its entries | alignment padding folded in by the disassembler: shrink the split range instead of inventing a field |
| Objects at **odd offsets** in a data blob (byte scripts) | MWCC aligns every data object to >= 4; such blobs cannot be written as separate C objects (use one byte array) |

## Dead stripping (the executable hash fails, units "match")

- Linking a matched unit makes `.text` shrink and the hash fail -> a function nothing in the main binary calls was
  stripped. `#pragma force_active on` (debug menus reachable only through tables).
- Functions and globals referenced **only from relocatable modules (RELs)** have no reference inside the DOL, so the
  linker drops them even with `force_active`. Symptom: every later section is shifted. Test with
  `nm build/<ID>/main.elf | grep <a function of the unit>`. Fix: list them in the `FORCEACTIVE` block of the project's
  linker script (`ldscript.lcf`).
- Strings/constants in `.rodata` with no user in your file -> the original had a function using them that the linker
  stripped; add an unused function at the right position (chapter 04).
- Unreferenced static functions are emitted by MWCC and stripped by the linker, so a "unused in retail" placeholder
  is safe.

## Section placement

| Symptom | Fix |
|---|---|
| A global lands in `.data` instead of `.bss` | it has a dynamic initialiser: give the type an (empty) constructor |
| Five zero-initialised variables in `.sdata` instead of `.sbss` | they were one-field structs initialised `{0}` |
| A block > 8 bytes that must stay in `.sbss` | `#pragma section RW ".sdata" ".sbss"` + `__declspec(section ".sdata")` |
| Small const objects (<= 8 bytes) end up in `.sdata2` | normal; avoid `#pragma section` for them |
| A function-local static came out first in `.sbss` / a file static declared first came out last | statics are emitted in source order; in deferred C files data is in parse order and `.bss` in reverse |
| `.bss`/`.sbss` definitions in reverse order of definition | normal for deferred files |
| vtables (`__vt__`) before jump tables in the target | code was still deferred at end of file (`defer_codegen`) |
| Extra weak vtables / inline dtors in your object | harmless: the linker keeps one copy; judge by the hash |
| Function static `name$1234` after a file static | the numbers differ so tools skip it; statics are emitted in source order: define the file static *after* the function |

## Constants and tables

- objdiff treats anonymous `@N` constants as interchangeable: a function can score 100% and load the wrong float.
  Compare the *values* each function uses (`tools/fcheck.py` CONST check).
- Signed vs unsigned int->float conversions use different magic doubles (see chapter 03).
- Initialisers like `{0xffffffff}` on a byte struct produce `0xff000000`: write all members.
- An explicit `char padN[k]` member in a struct with an *initialiser* is a trap: brace elision feeds the next value
  into the pad. Let the compiler pad.
- Data-only units: write **typed** tables (a struct taken from the code that reads it, or from the load widths in the
  reader's asm), never raw word dumps. A ~30-line script that reads the target asm's `.obj` blocks (`.4byte/.2byte/
  .byte` values and symbol names) and prints the C table is the fastest start. Pointers in a table: declare the symbol
  `extern` with a plausible type and use it. Tables nothing references get dead-stripped: `force_active`.
- Take every field width from the table's own accessors (their `lbz/lhz/lwz` and `stb/sth/stw` widths); merge the
  partial per-file copies of the same struct into one definition in a header; renames and merges keep objects
  byte-identical.
- Several nearly identical struct copies with different sizes in different files are a lead for a wrong view of the
  same data (`tools/` size scans).

## Addressing mode

If the target reaches an extern object with `lis rX,sym@ha` + `sym@l(rX)` but you emit `sym@sda21(r0)`, your
declaration is a small scalar while the real object is bigger or an array (`extern f32 __float_nan[];`). Byte-level
const/call checks do not notice; `tools/sdascan.py` does.

`lis/addi BASE` once, then `addi rX,base,0x0` / `base+0x3a1c` for several things: the original referenced several
separate static objects of one section; MWCC loads the section base once per function.

## Headers shared between files

Moving a class into a shared header can **renumber anonymous constants** in files that include it even when no code
changes: an unnamed union gets a number, and inline function bodies number their float constants, in every file that
sees them. Keep such parts visible only where they are used. Verify header refactors object-by-object, not only with
the executable hash: `@N` shifts show up only in object symbol tables.

## Checklist before marking a unit matching

All functions exact - layout (function order, symbol offsets, section sizes) - constants - calls - data bytes ->
flip the object to matching -> full build, hash OK -> compare with the session baseline -> commit.
