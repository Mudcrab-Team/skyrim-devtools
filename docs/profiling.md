# Profiling the game

A sampling CPU profiler built into the plugin: which game functions take the frame, with call stacks, self
and total time, GPU time per frame and per pass (GPU- or CPU-bound), the split between the game, plugins, SKSE, the GPU driver and Windows, what the main thread
waits on, and the stacks of every slow frame. Agents drive it through MCP (`profile_*` tools) and people
through the `skyrim-devtools-profile` command. There are three views: a live terminal dashboard (`top`,
in the spirit of `nvidia-smi`), a timeline of threads, frames and events (`timeline.html`, in the spirit of
Nsight Systems) and the [Firefox Profiler](https://profiler.firefox.com) with flame graph SVGs for the deep dive.

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
skyrim-devtools-profile top --config host.json [--save NAME] [--cell C] [--phase P] [--window 3] [--count N] [--json]
skyrim-devtools-profile timeline DIR [--recorder TIMELINE.jsonl] [--out FILE]
skyrim-devtools-profile export DIR --sqlite OUT.sqlite [--recorder TIMELINE.jsonl]
skyrim-devtools-profile query DB.sqlite "SELECT ..." [--limit 200] [--json]
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
| `timeline.html` | Self-contained timeline viewer (see below) |
| `profile.sqlite` | The profile as SQL tables (see SQLite export) |
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
writes the files on the MCP host), `profile_status`, `profile_watch`, `profile_hitches`, `profile_top` (one
live snapshot), `profile_mark` (a named span on the running profile) and `profile_query {sql, path?, limit?}`
(read-only SQL on the last `profile_stop`'s `profile.sqlite`, or on `path`). Raw RPC methods are listed in
[protocol.md](protocol.md).

### Live view: `top`

`top` launches the game (like `record`) and redraws once a second:

* fps and frame time p50/p99/max over the window (Present hook);
* main thread busy vs waiting %, the module split and its top functions by self % (with total %) over the
  last `--window` seconds, plus what it waited in;
* process CPU and the busiest threads by CPU % (`GetThreadTimes` deltas, thread descriptions as names);
* memory: working set, private bytes, *resident %* (working set / private), page faults/s and free system
  memory. A resident share that drops while faults/s climb means Windows trimmed the game's working set and
  every touched page now faults back in, which shows as a freeze;
* GPU utilisation of the busiest engine and dedicated VRAM, from the WDDM performance counters (`GPU Engine`,
  `GPU Process Memory`); shown as `-` where the counters are missing;
* GPU frame time p50/p95 from timestamp queries next to the sampled main-thread CPU time per frame, and
  whether the window was GPU-bound, CPU-bound or capped by the frame limiter (see GPU timing).

Each `Profile.top` call keeps the main-thread sampler of the hitch watch (250 Hz) running for 10 s, so it
costs nothing once nobody is looking; the first snapshot after a pause has few samples. GPU counters are read
once a second by a background collector for as long as snapshots keep coming, so a snapshot never waits for
the WDDM counter enumeration, and symbols are resolved through a cache that is rebuilt only when the loaded
module set changes. Each `--json` snapshot carries `costMs` (`samples`, `memory`, `threads`, `total` inside
the game, `resolve`, the `rpc` round trip and `resolverRebuilt`), so a slow refresh shows where it went. `--count N` stops
after N refreshes and `--json` prints one snapshot per line, for scripts. Agents use `profile_top`.

### Timeline: `timeline.html`

Every `record`/`profile_stop` writes `timeline.html`, a single file with the data embedded that opens in any
browser without a server. It has one time axis with:

* presented frames as boxes with their number and length once wide enough; frames over 1.5x the run's median
  frame time are orange and hitches (over the threshold) red;
* marker lanes: cell loads (`load`, the loading screen), other menus, RPC handlers that ran on the game or UI
  thread (`rpc`), hitches, and client markers (`Profile.mark`: `alert`, `narrate` or any kind). `profile_stop`
  also overlays the skyrim-use timeline's tool calls and narration that fall in the profile window; for a saved
  profile, `timeline DIR --recorder FILE.jsonl` does the same;
* one row per sampled thread with its samples as a stacked flame strip (root at the top, coloured by module
  kind, grey while waiting);
* below the threads, `GPU frames` (each frame's GPU span) and `GPU passes` (spans between clears, coloured by
  pass kind), placed on the CPU time axis as described under GPU timing.

Dragging over the rows or the ruler selects a time range, shows its length, and zooms to it on release. The
ruler moves from whole seconds to milliseconds and, when zoomed in below 10 ms, labels one tick with the
absolute time and the rest as offsets (`+1.250 ms`, `+50 µs`). A crosshair follows the pointer with the exact
time on the ruler and each row's value at that time (frame number and length, the marker, each thread's leaf
function or wait). Keys: wheel zooms around the pointer, Shift+wheel, Alt/Shift-drag or middle-drag pan,
Backspace (or `Back`) returns to the previous zoom, `-`/`+` zoom, `0`, `f`, `Fit` or double-click fit the whole
profile, arrows pan, Esc clears the selection, `?` lists the keys. Clicking a frame box or a thread row
selects that frame and lists its markers and, per thread, the top self functions and hottest stacks.

## GPU timing

While a profile, the hitch watch or `top` runs, the Present hook brackets every frame's GPU work with D3D11
queries: a disjoint query (for the GPU clock rate and to drop frames where it changed) and timestamps at
the frame start, at every render-target and depth-stencil clear (`ID3D11DeviceContext::ClearRenderTargetView`
and `ClearDepthStencilView`, hooked only while timing) and just before Present. Results are read up to eight
frames later without flushing, so the game never waits on them; a frame whose results are not back after 30
frames is counted as stale and dropped. It is plain D3D11, so it works the same on AMD, NVIDIA and Intel.

* **Passes** are the spans between consecutive stamps and are named after the clear that opened them, for
  example `dsv 2048x2048 array` or `rtv 1920x1080`. The kind is a guess from the target size: a square or
  array depth target that is not the backbuffer size is `shadows`, a backbuffer-size depth target `main`
  (depth prepass and opaque geometry), a backbuffer-size render target `scene`, a target of half the
  backbuffer or less `post`, anything else `offscreen`. Work that does not start with a clear is part of the
  preceding pass. At most 32 stamps are taken per frame; later clears are counted as dropped.
* **Alignment**: GPU timestamps use their own clock. A query cannot run before the CPU issued it, so the
  offset to the CPU clock is the largest (CPU issue time - GPU time) over the profile, which places the GPU as
  early as the data allows. When the GPU ran behind the CPU in every frame of the profile, the true GPU times
  are later than drawn. Durations are exact; positions are a lower bound.
* **Bound**: per frame, `cpu_ms` is the main thread's on-CPU samples inside the frame and `gpu_ms` the
  frame's GPU span. A frame is `capped` when both are under 85% of the frame time (vsync or the frame limiter
  set the pace), otherwise `gpu` when `gpu_ms > cpu_ms` and `cpu` when not. The main thread's busy wait in
  the frame limiter counts as CPU, so a capped game can read `cpu`; compare `frame_ms` with the target rate.
* **Overhead**: about 35 queries per frame and two hooked context calls; `hookUs` per frame is in the
  report. The CPU overhead budget (under 3% fps at 1 kHz) includes it.
* **Counters**: profiles also carry page faults/s, I/O MB/s, process CPU, working set and free memory every
  250 ms and GPU utilisation and VRAM every second, as `Counters` rows in the SQLite export.

Not covered: async compute and copy queues, the driver's own scheduling (an ETW `DxgKrnl` trace through WPR
shows that), and D3D12 games.

### NVTX

When `nvToolsExt64_1.dll` is loaded in the game or found through `NVTOOLSEXT_PATH` (part of the NVIDIA tools),
the plugin emits NVTX ranges for RPC handlers that run on the game or UI thread and for menus, and an NVTX
mark for each `Profile.mark`, so Nsight Systems shows them next to its own trace. Without the DLL these
calls do nothing.

## SQLite export

Every `record`/`profile_stop` writes `profile.sqlite`; `export DIR --sqlite OUT` writes one from a saved
profile. The layout follows Nsight Systems' export. Times are milliseconds from the profile start.

| Table | Columns |
|-------|---------|
| `Meta` | key, value: plugin build, sample rate, symbol sources, credits, GPU alignment offset |
| `StringIds` | id, value: every name, module and label once |
| `ThreadNames` | threadId, nameId, isMain, isFrameThread, onCpuSamples, waitingSamples |
| `Frames` | frame, start, end, frame_ms, cpu_ms, gpu_ms, gpu_start, gpu_end, hitch, bound |
| `CpuSamples` | timestamp, threadId, callchainId, state (`running`/`waiting`), weight (samples in the run), end |
| `Callchains` | id, depth (0 = leaf), symbolId, moduleId |
| `Symbols` | id, rva, nameId, moduleId, kind (module kind), sourceId (where the name came from) |
| `Markers` | id, kind (`load`, `menu`, `rpc`, `hitch`, client kinds), start, end, textId |
| `GpuRanges` | frame, passId, kind, start, end, ms |
| `Counters` | timestamp, nameId, value |

Views with the strings joined in: `SymbolsV`, `ThreadsV`, `MarkersV`, `GpuRangesV`, `CountersV` and
`LeafSamplesV` (each sample with its leaf function). Query with any SQLite client, `query DB "SQL"`, or
`profile_query` (read-only: writes, `ATTACH` and `PRAGMA` are refused, 10 s time limit).

Top self functions during hitches:

```sql
SELECT s.name, s.module, SUM(c.weight) AS samples
FROM Frames f
JOIN CpuSamples c ON c.timestamp >= f.start AND c.timestamp < f.end AND c.state = 'running'
JOIN Callchains cc ON cc.id = c.callchainId AND cc.depth = 0
JOIN SymbolsV s ON s.id = cc.symbolId
WHERE f.hitch = 1
GROUP BY s.id ORDER BY samples DESC LIMIT 20;
```

Frames where the GPU took longer than the CPU:

```sql
SELECT frame, frame_ms, cpu_ms, gpu_ms, bound FROM Frames WHERE gpu_ms > cpu_ms ORDER BY gpu_ms DESC;
```

What ran during cell loads:

```sql
SELECT s.name, s.module, SUM(c.weight) AS samples
FROM MarkersV m
JOIN CpuSamples c ON c.timestamp >= m.start AND c.timestamp < m.end AND c.state = 'running'
JOIN Callchains cc ON cc.id = c.callchainId AND cc.depth = 0
JOIN SymbolsV s ON s.id = cc.symbolId
WHERE m.kind = 'load'
GROUP BY s.id ORDER BY samples DESC LIMIT 20;
```

The heaviest GPU passes: `SELECT pass, kind, AVG(ms), COUNT(*) FROM GpuRangesV GROUP BY pass ORDER BY 3 DESC;`.

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

## Limits

* User-mode stacks only; time inside the kernel is attributed to the system-call stub (`NtWaitFor...`).
* A frame without unwind data (JIT code, some packed DLLs) ends the stack at that frame.
* Sampling has the usual error: at 1 kHz a function needs about 50 samples (50 ms of CPU) for a stable share.
* DbgHelp names some driver DLL exports by the nearest symbol it finds, which can be unrelated (an AMD
  driver frame showing as a `boost::serialization` template); treat `gpu-driver` names as approximate.
* `threads=all` suspends other game threads at `--other-hz`; with many busy threads lower it, or use
  `threads=main` for the cheapest capture.
* A throttled tick repeats the previous stack, so at high budget pressure short functions are undercounted.
