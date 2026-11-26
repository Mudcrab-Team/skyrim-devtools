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

Suspending a thread costs about 15-40 µs, more when it has to be woken from a wait, so each thread has a
suspension budget (`--suspend-budget`, 2.5% of wall time by default, `0` for none). Over a sliding 100 ms
window, a tick that would exceed it keeps the previous stack and is marked *throttled*; the report says what
share of main-thread ticks that was. With `threads=all` (the default) the main thread is sampled at `--hz` and
every other game thread at `--other-hz` (250 by default), each sample weighted so the shares stay comparable.

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
                               [--threads all|main|frame|<ids or names>] [--hz 1000] [--other-hz 250]
                               [--suspend-budget 2.5] [--save NAME] [--cell Riverwood] [--out DIR] [--keep]
skyrim-devtools-profile report DIR [--rewrite] [--json]
skyrim-devtools-profile export DIR --firefox OUT.json[.gz] [--open]
skyrim-devtools-profile diff A B [--threads main|all] [--json]
```

`record` launches the game with a window, optionally loads a save and moves to a cell, starts the profiler,
applies a load (`walk` turns and walks, `coc:` loads a cell and returns, repeatedly) and writes into `DIR`
(default `.local/profiles/<stamp>`, never part of a commit):

| File | What |
|------|------|
| `report.txt`, `report.json` | Top functions by self and total time, hottest stacks, module split, waits, hitches, overhead, symbol coverage, credits |
| `profile.json.gz` | Firefox Profiler format, see below |
| `cpu-flame.svg`, `cpu.folded` | On-CPU flame graph (interactive: click to zoom, Esc or "Reset zoom" to go back, "Search" or Ctrl+F to highlight a regex, hover for samples, % and ms) and Brendan Gregg folded stacks |
| `wait-flame.svg`, `wait.folded` | The same for waiting (off-CPU) samples |
| `raw.json.gz` | The raw profile; `report --rewrite` regenerates everything from it |

### Firefox Profiler

`profile.json.gz` is written on every `record` and `report --rewrite`; `export DIR --firefox OUT.json.gz`
writes it anywhere (plain JSON without `.gz`). Open https://profiler.firefox.com, choose **Load a profile
from file** and pick it, or drop it on the page. `--open` instead serves the file on `127.0.0.1` under a
random path and opens the profiler on it, the way `samply load` does; the page reads the file from this
machine and nothing leaves it. Chrome asks once to allow the page to reach the local network; allow it, or
use the file route. Press Ctrl+C to stop serving.

Never press the profiler's **Upload** (or *Share*) button: it publishes the profile, with the game's
addresses and names, to a public Mozilla server. Keep profiles local.

The export has a track per sampled thread with its call tree, flame graph and stack chart; module names
on every frame; waiting samples as idle (toggle "Include idle samples"); and markers for each frame and
each hitch. The file layout is described in [profile-data.md](profile-data.md).

`diff` compares two profiles (for example the same scene with and without a mod) and lists the largest
changes in self and total share and in the module split.

MCP tools in `skyrim-use`: `profile_start {threads, hz, duration_s}` (threads `all` by default), `profile_stop` (returns the report and
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
* **Modules**: `game` is SkyrimSE.exe; `plugin` this plugin; `skse` the SKSE loader; `skse-plugin` any other
  DLL in `Data\SKSE\Plugins` (a mod); `gpu-driver` the graphics driver and D3D runtime; `kernel` ntdll/win32u
  (system-call stubs); `system` other Windows DLLs.
* **By DLL**: per module, self is time in its own code and total is time with it anywhere on the stack. A mod
  that hooks a game function and calls back into the game shows a small self and a larger total.
* **Names**: a game function shows its name when a symbol table knows it, a guessed name with `(guess)`,
  `id:N+0xOFF` when only the Address Library ID is known, and `sub_<rva>` (function start from `.pdata`)
  otherwise. A name is a label, not proof of what a function does. Other modules without debug symbols
  (graphics drivers, for example) show `module+0xOFFSET` of the function start, or `module!Export` when the
  address is at or within 0x100 bytes of an export; their export tables are too sparse to name anything
  further away. Consecutive frames of such a module collapse into one `[module]` frame, so the time spent
  in a driver shows as one entry.
* **Overhead**: `mainSuspendedPct` is the share of wall time the main thread spent suspended by the sampler,
  `mainThrottledPct` the share of its ticks that kept the previous stack because the budget was spent,
  `otherMaxSuspendedPct` the worst other thread, and `samplerCpuPct` the sampler thread's own CPU. With vsync on, fps hides small overhead, so look at these
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

### Other runtimes

The plugin also loads on 1.6.1170 with SKSE 2.2.6 (each runtime is accepted only with its SKSE build and its
executable hash, and reports its build in `Session.info` and in each profile). The symbol tables above
describe 1.7.104, so a 1.6.1170 profile is named through Address Library IDs: each game frame carries the
nearest ID at or below its address, the 1.7.104 table (`versionlib-1-7-104-0.bin`) maps that ID to its
1.7.104 address, and the function there gives the name. Pass the table with `--addrlib PATH`,
`SKYRIM_DEVTOOLS_ADDRLIB=PATH` or `"addressLibrary": "PATH"` in the host config. A 1.6.1170 address is never
looked up in 1.7.104 tables directly. Functions without an ID shared by both builds stay `id:N` or `sub_`,
and a bridged name is a label for code that may have changed between builds.

When tables generated from a runtime's own CrashLogger PDB exist in the research checkout at
`.local/crashlogger_pdb/<build id>/` (for 1.6.1170: `sse-steam-1-6-1170`; write them with
`python3.13 tools/pdb_types.py generate <SkyrimSE.pdb> --build sse-steam-1-6-1170`), a profile of that build
is named from them directly by RVA, with no flag and no Address Library table; reports credit the version of
those tables. The PDB and the tables stay in ignored `.local/` folders. A build without tables keeps the
bridging described above.

Keep profiles local: they contain addresses and names of the game, so they belong in `.local/` or the test
PC's scratch tree, not in commits, issues or PRs.

## Profiling a mod manager setup

With Mod Organizer 2 put SkyrimDevTools.dll in its own mod (`SKSE\Plugins\SkyrimDevTools.dll`), enable it in a
copy of the profile rather than the profile you play, and start the game through MO2
(`ModOrganizer.exe -p "<profile>" "moshortcut://:<executable title>"`) from a process that has the
`SKYRIM_DEVTOOLS_*` variables set, with MO2 not already running so the game inherits them. Set
`SKYRIM_DEVTOOLS_IMAGE` to the hash of the executable the profile runs. The plugin then works as in the
pinned setup; mod DLLs appear as `skse-plugin` modules, named from the PDBs they ship.

## I/O and load traces

The sampling profiler shows where CPU time goes; during a load the main thread mostly waits, so the I/O trace
records what it waits for. Start the game with `SKYRIM_DEVTOOLS_IOTRACE=1` (or a list such as
`stack=12,events=3000000,counterMs=50,threadMs=250`; the defaults are 10 frames, 3 million events, 50 ms and
250 ms). The plugin then installs inline hooks on the kernelbase file functions while its DLL is being loaded,
before SKSE calls any plugin, and records for every call in the process:

* `CreateFileW/A`, `CreateFile2`: path as asked for, physical path of the handle (under Mod Organizer the mod
  it came from), size, flags, latency;
* `ReadFile`, `ReadFileEx`, `GetOverlappedResult(Ex)`, `NtReadFile` called directly: file, offset, size, bytes,
  latency, thread, call stack; overlapped reads keep their issue time so a wait shows the full latency;
* `SetFilePointer(Ex)` seeks, `CreateFileMapping`/`MapViewOfFile` of files, `CloseHandle` of traced files;
* `LoadLibraryEx` per DLL and each SKSE plugin's `SKSEPlugin_Load`, timed through a wrapper handed out by
  `GetProcAddress`.

A sampler thread adds process page faults, working set, private bytes, available memory and I/O counters every
`counterMs`, the system memory manager's hard-fault page reads, transition and demand-zero faults, and every
thread's CPU time every `threadMs`. SKSE messages, the loading, fader and main menus and `IoTrace.mark` calls
become marks. Hook bookkeeping is timed and reported as `hookOverheadS`.

Name the DLL so it sorts first in `SKSE\Plugins` (for example `!SkyrimDevTools.dll`) to time every other
plugin's load. Mod Organizer's virtual file system hooks the same functions earlier; the trace sits above it,
so its redirection is unaffected and is part of the measured latency.

`IoTrace.dump` writes `iotrace-<n>.bin` and `.json` into the run directory; copy both and run

    skyrim-devtools-io iotrace-2-final.json --addrlib versionlib-1-7-104-0.bin --symbols ../skyrim-research

which writes `io-report.txt`, `io-summary.json`, `io.sqlite` (tables `io_events`, `io_files`, `io_stacks`,
`io_threads`, `io_marks`, `io_phases`, `io_counters`, `io_thread_cpu`, `io_dlls`, `io_plugins`) and
`io-perfetto.json.gz` (open in ui.perfetto.dev: a track per thread with its I/O calls of 50 us or more,
phases, marks, and fault, memory and read-rate counters). Phases come from the marks: `skse-plugins`
(to PostLoad), `data-load` (InputLoaded to DataLoaded), `to-main-menu`, `startup` (to the main menu) and
`load <cell>` for each `coc <cell> begin`/`end` mark pair. The report gives per phase the open and read
totals, a read-size histogram, time by file kind and by mod, the top files with non-sequential and small reads,
the top calling functions, I/O and CPU time per thread, then DLL load times and per-plugin read spans.

An I/O trace holds file paths of the local setup and game addresses; keep it local like a profile. The trace
costs a few microseconds per call; reads are recorded even when they hit the file cache, so latency, not
count, separates disk from memory.

## Limits

* User-mode stacks only; time inside the kernel is attributed to the system-call stub (`NtWaitFor...`).
* A frame without unwind data (JIT code, some packed DLLs) ends the stack at that frame.
* Sampling has the usual error: at 1 kHz a function needs about 50 samples (50 ms of CPU) for a stable share.
* `threads=all` suspends other game threads at `--other-hz`; with many busy threads lower it, or use
  `threads=main` for the cheapest capture.
* A throttled tick repeats the previous stack, so at high budget pressure short functions are undercounted.
