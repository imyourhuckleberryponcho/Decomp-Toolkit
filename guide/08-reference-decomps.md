# 08 - Reference decomps (porting from sibling projects)
Applies to: **universal** (sibling games, SDK libraries); the compiler-matching details are GameCube / MWCC.

The fastest large gains come from code someone already matched. Most GameCube games share a lot.

| Shared layer | Where matched source exists | Notes |
|---|---|---|
| Dolphin SDK (OS, GX, DVD, PAD, VI, AI, CARD, GBA ...) | most GameCube decomps | same code, different SDK versions/flags; check the compiler version per library |
| MSL C library / Runtime / MetroTRK | several decomps built with the same MSL | MSL differs from stock fdlibm (errno = EDOM, `NAN`, `ldexp` rather than `scalbn`, inline `fabs`). TRK uses `-inline deferred` (reverse order); its `OSReport` is non-variadic |
| HAL sysdolphin (HSD baselib: jobj, dobj, mobj, tobj, aobj, fobj ...) | projects that use the same library | ports almost 1:1 into C + `-lang=c -inline deferred`; your game may add fields, functions and pool allocators |
| MusyX audio | the `musyx` submodule / reference library | files that differ get an edited local copy |
| Same-engine sibling game | the earlier/later game by the same studio | huge overlap in engine and tool code |

## How to port

1. **Find counterparts by opcode similarity**, not by name (a name-independent matcher like Coddog or a local
   opcode-sequence script). Names can differ and same-named functions can differ in size.
2. **Port the shape, fix the data.** Keep the reference's statement order, local caching (`mw = _pMw` before a loop),
   declaration order, loop forms, inline helpers and early returns. Then fix offsets, field types, globals and callees
   from *your* asm.
3. **Watch for version drift.** Extra/missing statements, bigger structs, different assert line numbers, different
   compiler versions per library. Triage: A = reference exact and same size (direct port); B = within 10% (port and
   adapt); C = size differs a lot (structural adaptation); D = reference itself unmatched (a hint only).
4. **Verify like your own code** (calls! constants!).
5. **Licensing.** Keep the reference's licence and attribution as required.
6. Reference comments often explain the trick ("written as `((hi << 5) + lo) + buffer` because ..."): read them. If the
   reference says a function is hand-written asm, do not try to match it in C.
7. Record the revision you ported from; re-run the similarity finder when the sibling progresses. When a sibling
   reaches 100%, refresh the twin table against its final sources: structure transfers (loop layout, which helper is
   not inlined, int vs bool constants), register-only leftovers usually do not (C vs C++, different compilers).

## Doing it automatically (`tools/refs/`)

The three tools turn the recipe above into a batch job. The idea works for any platform with a fixed compiler.

1. **Compile the references, do not read them.** `refcorpus.py` compiles other projects' library files with *your* compilers,
   each with its own version and flags, and again with your game's main version (the same library may have been built with
   another compiler). Take the flags from their build description as *data*: never run a downloaded build script.
2. **Compare machine code.** `refmatch.py` masks relocated words and compares every reference function with every function of
   the executable. Names, file splits and addresses do not matter. Besides exact and close functions it reports *pieces*
   (shared inlined helpers) and *files* (several functions of one object in order).
3. **Import runs, not functions.** Consecutive functions that match one reference object are one source file (part). They
   become their own unit with the reference's compiler and flags (`refport.py`).
4. **Prove the pairing.** Masked comparison cannot tell two functions apart that differ only in what they call. The game
   can: at each call site the executable says which of your functions the reference callee is. Two answers for one name, or
   a call to the wrong member of the run, means a wrong pairing, however good the score.
5. **Gate on the whole report**, by address: more exact functions, none lost, everything else not worse; else restore.
6. **Then the close ones** (`--close`): where the symbol name is already known for certain, the real source with a small
   version difference is a far better starting point than any draft.

Traps: functions of a few instructions match unrelated code, so never take a NAME from them (an `operator new` once got the
name of a graphics accessor). A wrong name spreads, because later steps trust it. And mind the licenses: no license file
means no permission to redistribute, so keep such imports local. `refport.py` skips them by default and writes the
license it found into each imported file.

Mirrors go stale: fetch them from time to time. A run that failed is worth retrying only when the reference or your own
neighbouring code changed, which the tool remembers.

## Practical SDK/library porting workflow

- Copy the reference file; drop functions your game lacks; mark helpers without a symbol in your game `inline`;
  order functions like your asm; then fix by hand: "UNUSED" stubs your game has, `static` vs global, unused
  initialised arrays leaving rodata templates.
- Compile library code with the reference's include tree added only for those libraries (`-i include/sdk_ref`).
- Compare per unit (code only), then confirm data-only units and `.rodata`/`.sdata2` order with the executable hash;
  a bisect script that flips units one at a time and keeps the good ones saves hours.
- Library assert macros put condition text and line numbers into the object. Use the macro form that takes explicit
  line numbers and never edit the condition text.
- Linker map files of any build of the game list stripped ("UNUSED") functions with sizes and original order: use them to
  find helper names, unused functions and file boundaries.
