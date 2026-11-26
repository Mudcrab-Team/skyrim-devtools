# Profiling the game

A sampling CPU profiler built into the plugin: which game functions take the frame, with call stacks, self
and total time, the split between the game, plugins, SKSE, the GPU driver and Windows, what the main thread
waits on, and the stacks of every slow frame. Agents drive it through MCP (`profile_*` tools) and people
through the `skyrim-devtools-profile` command. Output opens in the [Firefox Profiler](https://profiler.firefox.com)
and as flame graph SVGs.

## How it works

A sampler thread inside the game process wakes on a high-resolution timer (1 kHz by default), suspends each
target thread, reads its registers, walks its stack with the modules' own unwind tables (`.pdata`) and
resumes it. While a thread is suspended the sampler takes no lock and allocates nothing, because the thread
may own the heap or loader lock. Stacks are interned into a prefix tree and stored as runs, so an idle
thread costs a few bytes per second. A thread that used no CPU since the last tick is not suspended again:
its last stack is reused and the sample is marked *waiting* (off-CPU).

Running inside the plugin, rather than as an ETW session or an external sampler like samply, needs no
administrator rights or extra installs on the test PC and knows the game's frames: the Present hook marks
frame boundaries, so a profile has frame and hitch markers and the hitch watch can keep the stacks of any
frame over the threshold. What it cannot see is the kernel side of a wait or a driver's call stack below a
system call; for those, record an ETW trace with Windows Performance Recorder (`wpr -start CPU`) next to it.

The plugin sends raw data: module, RVA, the unwind function start and, for game code, the Address Library ID
of the containing function. Names are attached afterwards on the analysing machine, so the plugin stays free
of symbol tables and the same raw profile can be re-symbolised later as names improve.

## Commands

```
skyrim-devtools-profile record --config host.json --seconds 30 [--phase idle|walk|coc:<cell>[,<back>]]
                               [--threads main|frame|all|<ids or names>] [--hz 1000] [--save NAME]
                               [--cell Riverwood] [--out DIR] [--keep]
skyrim-devtools-profile report DIR [--rewrite] [--json]
skyrim-devtools-profile diff A B [--threads main|all] [--json]
```

`record` launches the game with a window, optionally loads a save and moves to a cell, starts the profiler,
applies a load (`walk` turns and walks, `coc:` loads a cell and returns, repeatedly) and writes into `DIR`
(default `.local/profiles/<stamp>`, never part of a commit):

| File | What |
|------|------|
| `report.txt`, `report.json` | Top functions by self and total time, hottest stacks, module split, waits, hitches, overhead, symbol coverage, credits |
| `profile.json.gz` | Firefox Profiler format: drop it on profiler.firefox.com or run `samply load` |
| `cpu-flame.svg`, `cpu.folded` | On-CPU flame graph and Brendan Gregg folded stacks |
| `wait-flame.svg`, `wait.folded` | The same for waiting (off-CPU) samples |
| `raw.json.gz` | The raw profile; `report --rewrite` regenerates everything from it |

`diff` compares two profiles (for example the same scene with and without a mod) and lists the largest
changes in self and total share and in the module split.

MCP tools in `skyrim-use`: `profile_start {threads, hz, duration_s}`, `profile_stop` (returns the report and
writes the files on the MCP host), `profile_status`, `profile_watch` and `profile_hitches`. Raw RPC methods
are listed in [protocol.md](protocol.md).

## Profiling a mod

1. Load the save where the mod does its work and let the scene settle (cell loads, scripts starting).
2. Profile the same activity twice, once without the mod's plugin and once with it, at the same place and
   for the same time (30 s or more): `record --save S --phase walk --out .local/profiles/without`, then
   again with the mod `--out .local/profiles/with`.
3. `diff .local/profiles/without .local/profiles/with`. Functions of the mod's DLL appear under its module
   name, with names when its PDB lies next to the DLL or on `_NT_SYMBOL_PATH`. The module split shows how
   much of the main thread moved into the mod; total time of the game function that calls it (a hook site)
   shows where it is invoked.
4. For hitches, turn the watch on (`profile_watch`) and play; `profile_hitches` gives each slow frame's
   stacks and, when the main thread was blocked, the Windows wait chain (which thread or lock it waited on).

## Reading a report

* **Self** is time with the function at the top of the stack; **total** includes everything it called.
  Shares are of on-CPU samples of the selected threads.
* **Waiting** samples are times the thread was not running (blocked on a lock, an event, I/O, or idle in
  `Sleep`/vsync). They are kept apart from on-CPU time; the wait flame graph shows where threads block.
* **Modules**: `game` is SkyrimSE.exe; `plugin` this plugin; `skse` the SKSE loader; `gpu-driver` the
  graphics driver and D3D runtime; `kernel` ntdll/win32u (system-call stubs); `system` other Windows DLLs.
* **Names**: a game function shows its name when a symbol table knows it, a guessed name with `(guess)`,
  `id:N+0xOFF` when only the Address Library ID is known, and `sub_<rva>` (function start from `.pdata`)
  otherwise. A name is a label, not proof of what a function does.
* **Overhead**: `mainSuspendedPct` is the share of wall time the main thread spent suspended by the sampler
  and `samplerCpuPct` the sampler thread's own CPU. With vsync on, fps hides small overhead, so look at these
  and at the frame-time percentiles in `context`.

## Symbols

Game code names come from local files that are never part of this repository. Point the tools at a checkout
of the decompilation project (skyrim-research), which contains generated CrashLogger PDB tables and project
names:

* `--symbols PATH`, or
* `SKYRIM_DEVTOOLS_RESEARCH=PATH`, or
* `"researchCheckout": "PATH"` in the host config JSON.

Lookup order for a game address: CrashLogger PDB names (`reconstruction/generated/crashlogger_pdb/names`,
function bounds from `.pdata` and `boundaries`), then project names (`data/progress.json`, guesses marked),
then aligned sibling symbols (`data/symbol-alignment.json`), then the Address Library ID, then `sub_<rva>`.
Reports name their sources, for example `CRASHLOGGER_PDB(59818@2026.10.24)` for CrashLogger names. Other
DLLs are named with DbgHelp inside the game from their PDBs (next to the DLL or on `_NT_SYMBOL_PATH`) and
their export tables. Without a checkout everything still works with IDs and `sub_` labels.

Keep profiles local: they contain addresses and names of the game, so they belong in `.local/` or the test
PC's scratch tree, not in commits, issues or PRs.

## Limits

* User-mode stacks only; time inside the kernel is attributed to the system-call stub (`NtWaitFor...`).
* A frame without unwind data (JIT code, some packed DLLs) ends the stack at that frame.
* Sampling has the usual error: at 1 kHz a function needs about 50 samples (50 ms of CPU) for a stable share.
* `threads=all` suspends every game thread each tick; use it for short captures. `main` is the cheap default.
