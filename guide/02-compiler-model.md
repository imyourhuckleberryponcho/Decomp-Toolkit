# 02 - A working model of MWCC code generation
Applies to: **GameCube / MWCC (PowerPC)**. Read it as an example of *what to model* for any compiler (register allocation, evaluation order, stack layout, section layout); the rules themselves are MWCC's.

These rules were measured with small lab files (see `tools/labgrid.py`) on a GC 2.x compiler at `-O4,p`, and
partly cross-checked against a decompilation of the compiler's own back end ([rayanht/mwcc](https://github.com/rayanht/mwcc), GC 1.x). They are a *model*: use them to
decide what to try, then let the compiler confirm.

## Registers

**Virtual registers are numbered in creation order.** Every value gets a virtual register when the front end creates
it (named arguments first, then named locals, then expression temporaries, inline-helper parameters and locals).
- *Coalescing* merges `mr`/`fmr` copies greedily in instruction order; the lower number survives.
- *Colouring*: nodes with fewer neighbours than registers are pushed first, so in a small function the **newest**
  virtual register is coloured first. Colour = the lowest free volatile register (`r0, r3..r12`) that does not
  interfere; values that live across a call fall through to callee-saved registers, taking the lowest already
  claimed one that is free, else claiming a new one from `r31` downward.
- In big functions, high-degree nodes (long live ranges: loop pointers, accumulators) are coloured earlier.

**Consequences that are usable as rules of thumb:**
- Among plain variables (assigned once from a call, then only passed along), the **first declared local gets r31**,
  the next r30, ... Assignment order does not matter; declaration order does. Block-scoped locals count where they are
  declared. Parameters come *after* all locals. For volatile (no call in between) values the first declared gets the
  *lowest* free register.
- A variable that is reassigned, updated (`b += 5`), used as an arithmetic operand, or holds a computed value ranks
  **above** every plain variable, and among those the later-created one gets the higher register.
- Each inline expansion **prepends** its own locals and parameters to the list: the most recently expanded helper is
  coloured first. Passing a variable into an inline makes it rank like a helper parameter. This is why moving code
  into or out of an inline helper reshuffles a whole function.
- Loop-hoisted constants and addresses get the top callee-saved registers, above any declared local.
- Loop-created pointers (strength-reduced `p[i]`) rank above a plain local read before the loop. A value obtained
  through an inline accessor ranks above them.
- Use count, `register`, and "used in loops" do **not** change which callee-saved register a variable gets.
- A **named** local/parameter never coalesces with a temporary, only with a physical register (r3 etc.). Inline-helper
  parameters/locals and expression temps merge freely. This is why "call result as an inline argument" and "one
  variable reused across blocks" change `mr` detours (below).
- MWCC normalises many surface forms to identical code: `this->x` vs `x`; `a->n[i]` vs `i[a->n]` vs `*(a->n + i)`;
  a same-type copy `T* b = a;`; taking an address in an earlier statement. Do not spend time on those. What changes
  registers is live ranges, inline boundaries, extra calls, and *types*.
- In most stuck functions the *named* variables already have the target's registers; the misplaced values are
  unnamed (inline-helper locals, loop pointers, expression temporaries). Look for the source *line* that creates the
  misplaced value (`tools/regwhere.py`), not for a variable name.

**The r0 detour.** `a = f(x); b = g(x);` emits `bl f; mr r0,r3; mr r3,rX; mr rA,r0`: a call result assigned to a
named local detours through r0 when argument setup follows, if that local is *first assigned here*. If the same
variable was already assigned earlier in the function (e.g. reused by an earlier block), the copy coalesces:
`bl f; mr rA,r3; mr r3,rX`. A result passed straight on as an argument goes direct. So:
- target has detours, ours has not -> the original used a fresh variable there (block-local / per-block variables);
- ours has detours, target not -> the original reused one function-level variable across the blocks (declare it once
  at the top), or the result went straight into an argument.

**Copies that survive.** A same-type copy is propagated away; a copy that survives in the target (`mr r26,r30` before
a branch) usually comes from a *type change* (`void*` list data cast to a struct pointer).

## Evaluation order

- Assignment `lhs = rhs`: the right side is generated first, then the left address, then the store.
- Binary `a OP b`: `a` then `b`, unless `b` contains a call, in which case `b` first. Integer subtraction is built as
  `subf(right, left)`: `a - b` evaluates `b` first unless `a` contains a call. Float subtraction evaluates `a` first.
- **Call arguments that contain calls are evaluated right to left**, before the simple ones: `h(f(1), f(2))` calls
  `f(2)` first. Read nesting from call order. (The C standard leaves this unspecified and other compilers differ - it matters for ports, chapter 09.)
- Inline accessors change this: `if (t == 4) return p; return conv();` inlined into `f(a.get(), b.get())` is
  evaluated left to right; the ternary form `return t == 4 ? p : conv();` keeps right to left.
- Constants only get a register when forced into one; immediates in address arithmetic never do.
- Float: `x * 0.5f` puts the constant first (`fmuls f,k,x`); a division by a power of two (`x / 128.0f`) puts the
  variable first. A literal `+ 0.0f` is folded; a surviving `fadds` with zero means a variable held it.
- Several results computed *before* any store (loads and divides interleaved, stores last) = write them into locals
  first; the compiler cannot move a load past a store through a pointer that may alias.

## Optimiser levels and what they hide

`-O4,p` (level 4, speed) normalises most expressions before code generation. A lower
`#pragma optimization_level N` on a function or file explains a loop-invariant that is *not* hoisted, or one function
that refuses every rewrite while its neighbours match. At level 0 registers follow **declaration order** (f31/r31
down), calls inside the right side of a binary operator go to compiler temporaries first, unused-but-assigned locals
live on the stack and `bool x = a && b` is temp + copy. Level-0 files also show `_savegpr_N` helper calls
(`#pragma use_lmw_stmw off`) and no `fmadds` (`#pragma fp_contract off`).

## Stack layout

- Frame slots are handed out from the top in this order: (1) the function's own locals, including block-nested ones,
  (2) locals of inlined helpers in call-site source order, (3) by-value *return temporaries* of inline helpers last.
- Inline locals are laid out **level by level**: all level-1 inline locals of every call site first, then level 2, etc.
  If the target groups helper temporaries by role across call sites, the original nested its helpers.
- Locals of the same size get addresses in **reverse declaration order** (later declared = lower address); size groups
  ascend. Reorder declarations to put arrays where the target has them.
- A **frame size** different from the target is never "just registers": it is a missing/extra local, a wrong array
  size, a by-value temporary, or nested vs flat helpers. Fix frame size first (`tools/framescan.py`). +-16 is often
  just one more saved register.
- A by-value small struct parameter is passed as a pointer to a copy; the callee reads it once and keeps it in a
  register. Try by-value when the target loads a pointed-to struct exactly once.
- Return-by-value helpers: count the target's temp slots. `vadd(a, b)` returning a new object per step produces one
  slot each; writing through an out pointer produces none.

## Calls and types

- A wrong **callee prototype** silently changes the *caller*: `double f()` for a function returning `float` gives
  `frsp`/`stfs` shapes; `f(s32)` for `f(enum)` moves the parameter copy to another register. Read the callee's own asm
  (`fdivs`/`frsp` before `blr` = float) and its other callers before blaming the caller. Exact functions are often
  tuned around a wrong prototype: fix the prototype and the locals/casts together.
- Narrow callee types show in the caller: a parameter passed with `extsb`/`extsh` in the target means the caller's
  value is *wider* than the callee's parameter; a result passed on without `clrlwi` although the definition returns
  `u16` means the caller declared it wider.
- Return width and `clrlwi` placement: `u16 v = f()` with a `u32`-returning `f` emits `clrlwi` right at the call;
  with a `u16`-returning `f` there is none at the call, but one at every *use* that needs the zero-extended value
  (compare, wider argument, mask) and none when passed to a `u16` parameter. Read where the target's `clrlwi` sits to
  choose the prototype.
- `int` vs `s32`: in many SDK headers `s32` is `long`. MWCC colours `int` and `long` locals differently and treats
  `int -> long` argument conversions differently. If registers are swapped or copied oddly, try `int`. Likewise
  `unsigned int` is not `u32` (unsigned long): keep a permuter's exact spelling.
- Block-scope prototypes (a declaration inside the function) let one call site use a different parameter width
  without disturbing other, already-exact callers in the file.
- `NULL` defined as `(void*)0` does not convert to typed pointers in C++: use `0`.
