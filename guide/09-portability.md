# 09 - Portability: compiling the decompiled source for PC

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
