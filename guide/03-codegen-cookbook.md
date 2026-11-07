# 03 - Codegen cookbook: symptom -> source fix
Applies to: **GameCube / MWCC (PowerPC)**, except section H (draft bugs behind 95-99% scores) and the first-aid lists of "check the value, not just the score", which are **universal**.

Each recipe: what you see in the *target* vs. yours, then the source change that produced the target. Names are
neutral. Recipes were observed on MWCC GC/2.x; confirm each with a quick lab compile.

## A. Control flow and branches

**One-case switch vs `if`.** Target `cmpwi r,K; beq A; b END` -> a `switch (x) { case K: ... }`. Target
`cmplwi r,K; bne END` -> a plain `if (x == K)`.

**Switch case labels are all visible.** MWCC's compare tree / jump table shows *every* case label, including empty
ones and ones that share the default body; decompilers drop them. A target-only compare constant next to our
`K-1` (`cmpwi 2; bge` vs `cmpwi 1; beq; bge`) = a missing `case 1:`. A dead `b END; b END` pair right after the
compare chain = an extra empty case one past the last real one (`case 4: break;`); a `default:` there changes both
code and behaviour. Run `tools/switchcases.py` and `tools/jtcheck.py`.

**Jump table or compare tree?** The table appears when the labels form enough separate runs. Listing every value
0..19 (3 real cases + 17 empty `break` labels) gives a compare tree; leaving a *gap* in the empty labels gives
`cmplwi 0x13; bgt; lwzx; bctr`. Empty labels and absent values both land on the exit, so the table bytes are identical.

**Range test.** `cmpwi x,0; beq F; blt F; cmpwi x,4; bge F` = `if (x != 0 && x >= 0 && x < 4)` with one shared
fail label; `x > 0 && x < 4` gives `ble`. `cmpwi 0; beq A; blt B; cmpwi K; bge` on one byte = a `switch` with
`case 0:` and a range group, not an if-chain.

**Early-exit `||` chain.** Only the *last* compare gets `bCC skip; b end` (branch over a branch). `b end; b end` =
a `return;` ending an arm plus the enclosing `return;`. In peephole-off files `beq A; b B; A:` comes from
`if (a == NULL || b != NULL) return;`; an empty then-branch does not do it.

**Loop with a top test and unconditional back-branch** (`cmplw; bge out; body; b top`) = `while (1) { if (i >= n)
break; ... }`. Plain `while`/`for` loops are rotated to a bottom test even with a `break` inside.

**`bne next; b found` inside a loop** = the search was an inlined helper with an early `return`.

**A guard before a loop whose exit runs a destructor** = `if (n != 0) { loop; rest }`, not an early return (that would
duplicate the destructor call).

**`if (c) x -= K; else x = 0;` is not `x = c ? x - K : 0`**; they compile differently. Likewise
`v = a == K ? 0 : a;` and `if (a == K) v = 0; else v = a;` both become a branchless mask for a `u16` target, but the
ternary masks the zero-extended copy and re-truncates, the if/else masks the raw value straight into the variable.

**Ternary of two constants.** An `int`-typed `c ? 12 : 0` becomes branch-free (`neg/or/srawi/andc`); an *enum*-typed
one keeps `li 0; bne; li 12`. If the target branches around a constant argument, the original passed enum constants.
`x = (a == 1) ? y : 0` is branch-free only if `y` is an integer; a pointer `y` gives branches.

**Same compare twice.** `fcmpo; bge L; bge L` = the condition tested twice (`y < 0.0f && y < 0.0f`, usually a macro).

**A constant compared in a register** (`li r0,K; cmpwi r0,0`, not folded) = a variable assigned in several places.
A single-assignment int/u8/short local or inline parameter folds the test away.

**`(x != 0 || !test)` error shape**: `cmpwi x,0; beq A; b ERR; A: <test>; bne OK; ERR:` is
`switch (x) { case 0: if (test) break; default: error }`.

## B. Types, widths and bool

- `neg; or; srwi 31` = `x != 0` as int/bool in a `-O4,p` file; `subic; subfe` (plus `clrlwi`) = the same in a
  size-optimised (`-O4,s`) file.
- `(bool)f() == 1` keeps the bool materialised (`subic; subfe; clrlwi; cmplwi 1`); `(f() != 0) == 1` folds to
  `cmplwi r3,0`.
- `cntlzw; srwi.` vs `extrwi.` = an int 0/1 vs a bool from `cond ? 0 : 1`.
- `(u8)(x >> 16)` compiles to `srwi + clrlwi`; the target's `extrwi` is `(x >> 16) & 0xFF`.
- `cmplwi i,N` on a signed loop counter = compared with `sizeof(arr)` (unsigned). An `int` and an `s32` loop counter
  differ in the entry guard (`li r0,0; cmpwi r0,12; bge` on one of them).
- A callee returning `u16`/`s8` narrows at the *use*, not at the call (see chapter 02).
- A `u32` bitfield struct read with `lbz`+`extrwi` where the target does one `lwz` and several `rlwinm` = the
  original held a plain `u32` and masked by hand. A bitfield insert shows the getter's width: `clrlwi` precedes
  `rlwimi` only when the getter's return width and the field access width differ.
- Bitfield order: CodeWarrior fills a unit from the top bit (see chapter 09).

## C. Register-only near misses (try in this order)

1. **Declaration order.** Declare the variable that should be higher (r31) earlier; to push one down, declare it
   later or in an inner block.
2. **One loop counter reused.** `li rA,0; mr rB,rA` at a loop start or `mr rX, rCOUNTER` zero stores = the loop reuses
   an earlier counter variable instead of a fresh `int i` (also: several loops each declaring `int i` where the
   original had one function-level `i` shifts the whole colouring).
3. **Pointer vs index.** A walking pointer (`p++`) and `arr[i]` give the same instructions but colour differently.
   Index the table directly (`tbl[i].f`) *or* hold a named row pointer (`row = tbl; row += idx;`) - try both.
4. **Hold a call result first.** `p = t * f(..)` coalesces the result with `f1`; `s = f(..); p = t * s;` gives a
   fresh register.
5. **Route a value through an inline helper** (or take it out of one). Types *inside* the helper matter:
   `u8 on = x ? 1 : 0; if (state != on)` and `int on = ...; if (state != (u8)on)` give identical instructions and
   different registers.
6. **Inline helper that takes a copy of a field** (`free(seq->id)`) colours differently from the same code written
   inline with a local `u16 id = seq->id;`.
7. **`int` vs `s32`**, `unsigned int` vs `u32`.
8. **Optimisation level / flags** for just this function.
9. Only then: permuter (chapter 07).
The solver `tools/regplan.py` + `tools/regwhere.py` shows which variable/line holds the wrong register.

## D. Calls, arguments and prototypes

- Forwarded parameter registers: a caller-saved register passed through untouched (`clrlwi r7,r7,24` or an unchanged
  `r6`) = the function forwards its own parameter as an extra argument; `tools/argextra.py --strict` finds
  arguments *we* set that the target does not (lost/extra args, old-style `T f();` declarations).
- Rotated constants across a run of calls = wrong call order or wrong argument order (`argextra.py --consts`).
- An indirect call whose `r6` is loaded right before the null test = the function pointer takes a 4th argument.
- Function pointer held in a local first (`u8 (*fn)(...) = lookup(w); if (fn(t, ...) == 0)`) when the target reloads
  `w` after the lookup.
- Floats loaded in reverse order (z, y, x) and stored x, y, z = arguments evaluated right to left (an inline
  `Vec(x,y,z)` constructor). Assigning locals z, y, x in that order reproduces it.
- Float products stored in the target's order: write each into its own local, then construct. A constructor call with
  the products as arguments evaluates them right to left.
- `lhs = a + (b - a) * t` as an inline helper returns `fmadds x, t, (b-a), a`; `f32 d = b - a; return d * t + a;`
  gives `fmadds x, (b-a), t, a` (operand order). An inline helper's return value is a temporary: when the variable it
  is assigned to is later swapped or assigned elsewhere, MWCC computes into another register and adds `fmr`. Writing
  the math as statements into the variable removes it. `a = b = expr` computes into `b` and copies to `a`.
- A multiply with the operands the other way round (`fmuls f2,f2,k` vs `fmuls f2,k,f2`) = the original *divided*
  (`x / 128.0f`).
- `% 10`: unsigned -> `mulhwu`, signed -> `mulhw 0x66666667`.

## E. Structs, copies and memory

- Small structs are copied **member by member**. An explicit `u8 pad;` member is copied too (extra `lbz/stb`); real
  implicit padding is skipped. If you see ours-only copy stores at odd offsets inside a struct assign, delete the
  explicit pad members and let alignment pad (check `sizeof`).
- C++ implicit `operator=` of a struct that has a member with a user `operator=` copies member-wise; a POD
  sub-struct is one block copy (loop above 0x40 bytes, unrolled pairs up to 0x40). Split or flatten members until the
  target's block sizes appear.
- 16 zero stores in a strange order, not interleaved with neighbouring code = a nested `for i<4 for j<4 m[i][j] = 0`
  loop (fully unrolled). A run of identical stores that the scheduler does not interleave = an unrolled loop in its
  own block (18 `sth -1` = a 9-step pointer loop, not 18 assignments).
- Unrolled recursion in a draft (6 nested copies + one call) = MWCC inlining a plain recursive function into itself;
  depth depends on how the helpers are written. Callers that must not inline it get a block-scope declaration.
- Stores/loads that will not reorder, or a value spilled and reloaded: the original may have used a
  `volatile`; but treat that only as a last resort and look for the real reason first.
- `memset(&a->name[len+1], ...)` after `a->name[len] = 0xFF` is CSE'd (base+len reused); the target's `add rA, base,
  (len + 9)` came from `(u8*)a + len + 9`.
- Large offsets inside a global: if the target adds the offset to the base *first* (`addis rB,rG,1` then the index),
  index a row pointer built from the address: `((T(*)[N])((u8*)&G + off))[i][j]`.
- `x.field = getter()` / `field = f() != 0` setters: look at `neg/or/srwi` for bool arguments.
- A `delete`/dtor draft with hand-written `if (this != NULL)` or `if (flag > 0) operator delete(this)` has compiler
  generated parts: delete them. `delete p` passes 1 to the destructor, `p->~T()` passes -1.
- A `static` member-looking declaration in a draft makes m2c shift every parameter by one.
- `cmplwi r,0; beq L; beq L` right after `new` / before `delete` = `if (p != NULL) delete p;`.

## F. Floats and SDK conversions

- Paired-single conversions (`psq_l/psq_st` through a stack temporary: `OSf32tou8`, `OSu16tof32` etc.) are the SDK's
  inline helpers, not compiler conversions. Use the real forms: `__OSf32tou8` with a `u32 tmp` gives 4-byte slots;
  a `u8` temp gives byte slots.
- `extern f32 __float_nan[];` (array) gives `lis/lfs`; a scalar declaration gives `@sda21`. Small scalars in the
  small-data area, anything larger or array-typed through `@ha/@l`: declare the real type (`tools/sdascan.py`).
- Signed vs unsigned int->float use different magic doubles (`0x4330000080000000` vs `0x4330000000000000`).
- A GX FIFO declared as a fixed-address object (`volatile PPCWGPipe GXWGFifo : 0xCC008000;`) generates the SDK
  shape; a cast-pointer FIFO makes MWCC reload globals after every store. Copy struct fields into plain locals before
  FIFO loops.
- Cos/sin/sqrt results rounded at the wrong point: `x * (f32)sin(a)` gives `frsp + fmadds`; a file-local
  `inline f32 cosf(f32 x) { return (f32)cos(x); }` reproduces negated in-place cosines.

## G. Inline functions

- A helper inlined into every caller *and* also emitted at the end of the file: make the helper `static inline` for
  callers and define the exported function last as a wrapper.
- Inline member functions defined *after* their callers end up as out-of-line weak functions at the end of the
  object; define them `inline` at the bottom, or above their callers, as the target shows.
- Helpers MWCC refuses to inline (too large once their own helpers are inlined) appear as real calls. If the target
  has the body at each site, the original wrote it out or used a macro.
- `#pragma dont_inline on` also turns compiler-generated `operator=` into a call (`bl __as__...`). Use
  `#pragma auto_inline off` for "do not inline my small helpers".
- Pragmas written inside an `extern "C++" { }` block did not re-enable inlining; put `push/pop` outside the block.
  An unclosed `push; dont_inline on` pair in a draft disables inlining for the rest of the file.
- `mr r0,r3 ... mr rX,r0` after a call = the result goes into an *existing* variable (merge two draft variables).

## H. Draft (m2c) bugs that hide behind 95-99% scores

These are *value* bugs, not code shape; the fuzzy score barely sees them because immediates are normalised.
- **Typed pointer stepped by byte counts:** `u32* p; ... p += 4;` is 16 bytes. Compare the loop's `addi rX,rX,N`.
- **Typed pointer plus offset:** `(T*)(this + 0x80F8C)`, `(s32*)p + 4` scale by element size. Use `(u8*)p + off` or the
  real member. (With a 1-byte placeholder class the byte offset is already exact.)
- **Shadowing declaration:** `} else { int ended = 0; }` declares a new variable; the real one is never assigned.
  Look for an initialised declaration as the last statement of a block, and for a local that shadows a file static
  (`int count = n;` meant `count = n;`) (`tools/shadowbug.py`).
- **Inverted condition:** same code but the opposite `bCC` (`tools/branchflip.py`).
- **Signed vs unsigned pairs** (`extsb` vs `clrlwi`, `srawi` vs `srwi`, `cmpw` vs `cmplw`) (`tools/signscan.py`).
- **Wrong argument order / wrong call order** (`argextra.py --consts`).
- **Placeholder types by value:** `enum Mtx { dummy }` (4 bytes) or `struct Vec { u8 _x[1]; }` makes frames the wrong
  size and keeps cast addresses in saved registers. Use the real `typedef f32 Mtx[3][4]`.
- **Workaround types:** `volatile` locals, "wide enum" tricks and `(0, x)` comma expressions hide the real code.
  Remove them and look at what the target does with the value (raw call result masked at the compare =
  `u32 x = f(); if ((u16)x == K)`; `extsb` before `+ 1` = `(s8)get() + 1`; a value computed early and never used = a
  lost argument).
- **Memory beyond a struct** (`g->arr[+0x50]` past the real size) = the file's *next static arrays*; write the
  statics by name.
- **Zero stores to an array in a loop that the target did with one `li`:** a stack buffer "copied from a pointer"
  is often copied from a global table.

## I. Small things that cost an hour each

- `HSD_ASSERT`-style macros put `#cond` into the object: never change the condition's text.
- Strings: `__FILE__`/`__LINE__` come from the assert call, so line numbers must match; write
  `__assert("file.c", line, "expr")` explicitly if needed.
- `mr r0,r3; li r3,X; mr r4,r0` around a call result in peephole-off files -> store the result in a typed local first.
- A switch over a list's control codes: empty cases (`case 6: case 7: break;`) are control codes the loop skips.
- `if (mode == 1) { return; }` as the whole body keeps a lone `cmpwi r4,1; blr`; an empty `if` or one-case `switch`
  drops the compare.
- A function that falls off the end with a value in `r3` returns whatever was last computed (the m2c draft
  "returns" nothing); the original code was `return gp->field = x;` style.

## J. Shapes that took real digging (solved)

**A compare right before `blr` with no branch** (`lbz r0,flag; li r3,0; cmplwi r0,0; blr`). In a *void* function
`if (a != 0) { return; } if (b != 0) { return; } return;` leaves the last compare; with a return value MWCC makes a
branch-free select instead. Other GC games' sources showed the same `if (model) return;` form.

**`cmplwi r,0; bne A; b NEXT` (a jump as the whole then-block).** In a loop,
`if (x == 0) { continue; } else { while (p) { ... } }` keeps the `bne A; b NEXT` shape; `if (..) continue;` without
the explicit `else`, an empty then-block, a one-case switch, a goto or pragmas all give `beq NEXT`.
For early returns: the tests are *one* `||` chain, `if (a == 0 || b == 0) return;` - only the **last** compare of the
chain gets `bne L; b EXIT`; separate `if (..) return;` statements fold to `beq`.

**`frsp f2,f1` plus a raw `stfs f1`.** The callee really returns `float` but the file declared it `double`. With the
`float` prototype, `dt = getDt(); t += dt;` re-rounds for the arithmetic and stores `f1` as it is.

**`addi rD,rS,0x0` where you emit `mr rD,rS`.** The base register holds a *section* address and the original
referenced several separate static objects of that section (arrays, string literals); MWCC loads the section base once
and the first object is `base + 0`. A struct or pointer over the whole blob gives `mr`. Look in `symbols.txt` for
more objects inside the "struct". Large binary arrays: dtk's `extract:` option + `#include "x.inc"` in the initialiser.

**Hoisted loop constant one instruction early.** The original read a byte through a cast,
`(u8)obj->alpha | 0xFFFFFF00`; dropping the cast moved the constant load.

**Shared "keep" block reached from two tests.** A layout with one copy of a block and the second test *after* it,
jumping backwards, is what `goto` (or a loop construct) produces; `if (c1) A else if (c2) A else B` makes two copies
and `c1 || c2` puts the second test before. If every structured form fails, a single shared label is acceptable -
that is how the original was written.

## K. Rules measured with small labs (GameCube / MWCC)

Each of these came from a miniature compiled in `tools/labgrid.py` (chapter 12), not from guessing.

**Dead compare.** A `switch` with exactly ONE do-nothing case (the same as `default`) emits `cmpwi K; b default`: the
`beq` is dropped. With TWO do-nothing cases (merged or separate labels, any order) the `beq` stays. An empty
`if (st == 0) { DEBUG_MACRO(..); }` disappears with no compare at all, while case bodies made of a debug macro keep
the `beq`. So a leftover `cmpwi K; b L` pair is a one-case switch, not three cases.

**Loop test at the top.** `while (c) step;` is rotated (test at the bottom). A top-tested loop that reloads its
operands is `for (;;) { if (c) break; step; }` (string scans, tag searches).

**Address folding.** `memcpy(&a[1 + n], ..)` folds the `+1` into the index; `&(&a[1])[n]` keeps the `+4` after the
scaled index. A field address the target keeps in a register across a loop (`addi rX,rBase,0x84`) is
`T* p = &obj->field;` before the loop.

**Operand order of a commutative operation** (`add r3,r3,r0` against `add r3,r0,r3`) is the source spelling (which
operand is written first, int + pointer versus pointer + int), not register allocation. Register searches cannot fix it.

**Vtable order.** Vtables are emitted in the reverse order of each class's first non-inline virtual function. If the
target's order is the reverse of the destructor order, the destructor was the first virtual declared in each class.

**Target re-loads a field with no store in between** (two switches on `tex->format`): not an inline-helper boundary in
the cases tried (both switches as helpers, one helper for entries only, one for the special format only: all worse).
Next suspects: the two reads go through different types/expressions (a header view versus the full struct, `s32`
versus `u32`), or the first read is a shared helper call.

**Structure after an exact match is still a hint.** A permuter or search result that matches by adding a no-op
(`if (p == 0) p = 0`, a comma, a volatile) is evidence about *where* the difference is, not the answer: rewrite it as
the code the author would have written, or leave the function parked.
