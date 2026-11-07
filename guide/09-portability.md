# 09 - Portability: compiling the decompiled source for PC
Applies to: **universal** for a port of any decompiled game to a modern 64-bit platform (pointer widths, endianness, bitfields, evaluation order); the examples use GameCube sources.

A matching decompilation is written for a 32-bit big-endian PowerPC with CodeWarrior's rules. If you later compile the
same source for a 64-bit little-endian desktop target (MSVC, GCC, Clang), these are the things that break. The central
discipline: **GameCube output must stay byte-identical**, so every portability fix is either behind `__MWERKS__` or a
type/macro that is the very same type on the GameCube.

## Pointers and integer widths

- Pointers are 8 bytes on x64. Structs that embed pointers change size and every later field moves. Code that reads
  fields at raw GameCube offsets (`*(T*)((u8*)p + 0x2C)`, m2c's `M2C_FIELD(p, T*, 0x2C)`) lands on the wrong field once a
  pointer sits before the offset. Replace with named members; keep layout-only "view" headers (and `static_assert` them
  on PC) for opaque records.
- Pointer -> `int`/`s32` -> pointer **sign-extends** if the address is above 2 GB (`0x80000000+` game memory mapped
  into a 64-bit process). `u32` round trips survive if everything lives below 4 GB; stack and library pointers are not
  guaranteed to. Use a typedef that is the same `s32`/`int`/`u32` on the GameCube and pointer-sized elsewhere
  (`GCsptr`, `GCuptr`-style). `int` and `long` mangle differently in C++, so keep two typedefs.
- m2c pointer arithmetic on typed pointers (`(T*)(this + 0x278)`) scales by `sizeof(T)`; use `(u8*)this + 0x278` or the member.
- `THIS_F(s32*, off, ptrMember) = x` style macros that write through a cast onto a *pointer member* only write half of it.
- A class built on the stack from a "blob" declaration (`class X { u8 unk[0x34]; }`) overruns on PC if the real class
  contains pointers: size the blob with `sizeof` on PC.
- Prototypes that pass a pointer as `u32` (draft style) truncate on PC.

## Narrow arguments, return values and callbacks (x64 calling convention)

On the GameCube a caller always sets the whole 32-bit register. On x64, passing or returning a `u8`/`u16`/`bool`/enum
sets only the low part, and the other side may read garbage in the rest:
- **Return width:** callee returns `u8`, caller declares `s32`/`u32` (or the reverse) -> garbage above AL/AX under
  `/O2`. Debug builds hide it because they zero the upper bits.
- **Argument width:** caller passes `u8`, callee reads `u16`/`u32`.
- **Callbacks:** a function declared `void f(MyEnum)` called through `void (*)(u8, ...)` works on GameCube and freezes on
  x64. Scan function-pointer typedefs and assignments (`tools/fnptrscan.py`).
- **Varargs:** `va_arg(ap, void*)` reading a 32-bit value that was pushed as `int` gives a garbage upper half.
- Declarations that disagree between translation units are the root cause. Fix the declaration; if you cannot touch
  GameCube code, add per-platform widening macros on the callee's return / caller's parameter.

## Endianness of data from disc

Anything read from disc images (tables, scripts, models, saves) is big-endian. Do **not** swap in the middle of
game logic; give disc-record fields types that convert on access (`gc_be32`, `gc_ptr32` wrappers) and keep them plain
`u32`/`u16` on the GameCube. Watch for:
- **Colours:** GameCube code treats a `GXColor` (bytes r,g,b,a) and a `u32` (`0xRRGGBBAA`) as the same four bytes
  (casts, `memcpy`, unions). On little endian each of those reverses the channels.
- Tables of pointers inside disc data are 4 bytes each; use a 32-bit pointer wrapper, otherwise `*p` reads 8 bytes.
- Bitfields over disc/console bytes (see below).
- Tables you built *in the program* with big-endian fields (`double`, `u64` as two words) need the matching wrapper.
- Records reachable both from disc data and from program statics need one layout (make the static the same big-endian layout).

## Bitfields

CodeWarrior (PowerPC) fills a bitfield unit from the **top** bit; MSVC/GCC on x86/ARM from the bottom. A bitfield over
console-written bytes reads the wrong bits on PC. A macro that declares one unit's fields as written under
`__MWERKS__` and in reverse order elsewhere keeps GameCube output identical (pad a not-full unit with an unnamed field
first, or the reversal moves the fields). The bits then match the unit's **value**, so `u16`/`u32` units still need their
byte swap first.

## Evaluation order and other language differences

- The order in which call arguments are evaluated is unspecified in C/C++. MWCC evaluates arguments that contain calls
  from last to first (see chapter 02) and MSVC/GCC/Clang each do their own thing, so code that depends on it (two
  `rand()` calls in one argument list, `f(g(), h())`) needs explicit temporaries in the right order.
- `NULL` is `(void*)0` in some SDK headers; C++ rejects the conversion: use `0`.
- Draft-style "falls off the end with a value in r3" functions need a PC-only `return`.
- `sizeof(long) == 8` on LP64 Linux/macOS (not on Windows): `s32` defined as `long` breaks there.
- Unions with `long` and pointers change size. Unnamed unions/structs given a `@N` name by MWCC are a numbering concern
  (chapter 05); on PC they are plain C++.
- Inline assembly, paired-single (`psq_*`) and MSL copies need `#ifdef __MWERKS__` and a portable fallback.
- Hardware addresses (`0xCC008000` GX FIFO) become function calls or a memory-mapped buffer.

## Finding the problems early

- Compile everything with the PC compiler regularly (even without linking): `tools/pccheck.py` style compile checks,
  warnings as a draft-bug finder (missing return, uninitialised use, implicit declaration in C files that turns float
  results into `int`, truncation warnings for pointer casts).
- Scan: callback widths (`fnptrscan.py`), declarations that disagree across files, raw offset reads, `u8 pad[4]`
  standing for a head pointer followed by a non-pointer field (overlaps on PC), views of one class in several files
  whose PC offsets differ.
- Re-run the GameCube build and compare **objects byte for byte** after every portability edit.

## Views of one class in many files

Applies to: universal for any port; the offsets are GameCube's. Decompiled files often declare their own partial view
of a class at the original offsets. On a platform with different pointer size or alignment, those views disagree about
where a field is. Build a table of every class that occurs in more than one file (field, original offset, new-platform
offset, per file) and look only at offset mismatches; a size-only mismatch of a short placeholder view is harmless
unless something embeds, sizes or copies it. A pointer hidden in padding (`u8 pad[4]` followed by a non-pointer field)
overlaps its neighbour as soon as pointers grow to 8 bytes. Mark data read straight from disc as big-endian with explicit
types, so the port does not swap it twice.

## Porting what generalises: N64, GBA, PS2, x86 and others

The compiler and the CPU differ, the method does not.

**Take as it is**
- the workflow (cheapest first, park puzzles, only verified commits), the verification discipline (chapter 06),
  the "real difference" triage and the lab method (chapter 12), the permuter and scripted search strategy
  (chapter 07), long unattended runners that write to a review folder, history mining, value checkers, byte-identity
  proof for neutral edits, porting lessons (this chapter);
- the idea of a file-level cause: a whole file almost right = flags, pragmas, function order or a different library
  version before it is bad C.

**Replace**

| Thing | Where it lives | Replace with |
|---|---|---|
| Compiler flags and pragmas | `04`, `fcheck`, `labgrid`, `flagsweep`-style tools | your compiler's: IDO `-O2 -g3 -mips2` plus `#pragma`s (N64); agbcc `-O2` with its own preprocessor step (GBA); GCC `-O2 -fno-...` (PS2 / PSP / old GCC games); MSVC `/O2 /Ob1` etc. (x86) |
| Register conventions and what is "volatile" | `02`, `regplan`, `regorder` ideas | the ABI of your CPU: MIPS `$a0-$a3`, `$s0-$s7`; ARM `r0-r3` scratch and `r4-r11` saved; x86 `eax/ecx/edx` scratch and `ebx/esi/edi` saved |
| Assembler syntax in the scanners | the regexes in `structdist`, `shapedist`, `reldiff`, `argextra`, `signscan`, `branchflip` | edit the register-name regex (`r\d+`, `f\d+`), the stack-pointer pattern (`N(r1)`), the copy instructions (`mr`, `fmr`), the branch mnemonics and the load/store names |
| Small-data / constant pool rules | `05`, `poolscan`, `sdascan` | MIPS `$gp`-relative data, ARM literal pools, x86 `.rdata` constants: same idea (constants numbered in creation order, pools laid out in that order), different sections |
| Project layout and diff tool | `dtkproj.py`, `fcheck.py` (dtk-template, objdiff, `ninja -t commands`) | the equivalent of your project: asm listing per unit, original object per unit, your compile command. N64/GBA projects use the same objdiff; PS2/x86 may use asm-differ or objdiff with their own configs |
| Debug info used by `regplan` / `regwhere` | MWCC `-sym on` DWARF line table | whatever your compiler can emit (stabs, DWARF, `.mdebug`); without it, skip those tools and use the lab |
| Draft generator | m2c | m2c also reads MIPS; for other CPUs use the best decompiler you have, the drafts are always rewritten by hand |

**Typical per-platform notes.** IDO (N64) is very sensitive to statement order and to `register` hints and has a
well-known "permuter-friendly" culture: the permuter ideas here apply directly. agbcc (GBA) is a GCC 2.95 variant:
register allocation depends on declaration order and on `inline` placement, and projects there keep a flag list per file
exactly like chapter 04. MSVC (x86) matching hinges on function order and on `/O` levels, and COMDAT folding
changes what the linker keeps. In all cases: build a lab (chapter 12) before trusting a rule from this guide.
