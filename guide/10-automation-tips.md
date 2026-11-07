# 10 - Automation tips
Applies to: **universal**.

Decompilation has a lot of repetitive, verifiable chores. A few lessons from scripting them (whether the "worker" is a
shell script, a batch search or an assistant program):

- **Make it measurable.** Every automated step should end in a compile + score + checks, never in "looks right". Record
  per stage how many minutes it spent versus how many functions it fixed; reweight time accordingly.
- **Cheapest stage first.** Twin copy -> flag sweep -> mechanical edit search -> randomised search -> manual.
- **Work on private copies.** Searches that rewrite sources must restore the original bytes (also on Ctrl+C and
  errors) and use unique scratch folders per job so parallel jobs do not collide. Delete old scratch folders: they
  add up (hundreds of thousands of 600 KB folders filled a disk once).
- **Verify twice.** A search result is only a candidate: re-compile it in the real build, run layout/const/call/data
  checks, run the full build. Reject candidates that add `volatile`, `goto`, `(0, x)`, empty ifs, or change a constant.
- **Park and remember.** Keep a list of what was tried per function so the same searches are not repeated; only new
  tricks should re-run on parked functions.
- **Short briefs for helpers.** If you delegate drafting to an assistant, give it: the unit, the asm path, 2-3 most
  similar matched functions' source as style templates, the exact compare command, the stop rule (about 5 attempts or
  10 minutes per function) and the verification list. Cap its context: tell it to grep and slice big files.
- **Hot spots for scripts:** accessor/table generators, twin finders, declaration-order search, flag sweeps,
  scanners for the draft-bug list in chapter 03 (H), jump-table / switch-label checks, pool-order checks.
- **Crash-proof long runs.** Write state to disk, log to a file, restart automatically a bounded number of times, and
  keep a "stop file" check so a human can stop it safely.
- **Disk and CPU hygiene.** Use idle priority for background lanes, limit parallelism to cores, avoid compiling the
  same candidate twice (hash the source).
