# Profile data model

The files a profile directory holds, field by field, so other tools (a trace recorder, a merger, a viewer)
can read and extend them. Fields are only ever added; a reader must ignore fields it does not know. A change
that alters the meaning of an existing field bumps `version`.

## raw.json.gz

What `Profile.stop` returns, gzip-compressed. `report --rewrite` rebuilds every other file from it.

Top level:

| Field | Meaning |
|-------|---------|
| `format`, `version` | `"skyrim-devtools-profile"`, `1` |
| `hz` | Sampler tick rate |
| `threadsSpec` | What `Profile.start` was given (`main`, `frame`, `all`, or a list) |
| `suspendBudgetPct` | Per-thread suspension budget in percent of wall time; `0` means unlimited |
| `otherEvery` | Threads other than the main one are sampled every this many ticks |
| `startEpochMs` | Wall-clock time (Unix ms) of `startMs` 0 |
| `durationS`, `ticks`, `lateTicks`, `truncated` | Length, ticks run, ticks that woke late, whether a size cap stopped recording |
| `samplerCpuMs` | CPU time of the sampler thread |
| `mainThread`, `frameThread` | Thread ids of the game's main thread and the thread that calls Present |
| `pluginBuild` | Build id of the plugin that recorded it |
| `frames` | Interned code addresses, see below |
| `stacks` | Prefix tree: `[prefixStackIndex or null, frameIndex]`, a stack is its leaf entry, prefixes come first |
| `threads` | One entry per sampled thread, see below |
| `frameMarks` | `[startMs, ms, frameNumber]` per presented frame |
| `hitchThresholdMs`, `hitches`, `hitchFrames` | Slow frames: `{frame, ms, startMs, stacks: [[stack, count, flags]], samples: [[ms, stack, flags]], waitChain}`; `hitchFrames` is a separate frame table for their stacks |
| `modules` | `{name, path, base, size, game, symbols}`; `symbols` is the DbgHelp symbol type (`pdb`, `export`, `none`, ...) |
| `gpu` | GPU timing: `{available, reason, backbuffer: [w, h], labels, disjointFrames, staleFrames, frames}`; a frame is `[frame, cpuBeginMs, cpuEndMs, hookUs, droppedStamps, [[labelIndex, gpuSeconds], ...]]`, where `cpuBeginMs` is when the frame-start stamp was issued (after Present returned) and `cpuEndMs` when the Present stamp was issued, on the profile clock; GPU seconds are on the GPU's own clock |
| `gpuCounters` | `[ms, gpuUtilPct or null, vramMb or null]` about once a second (WDDM performance counters) |
| `counters` | `[ms, pageFaults, workingSetBytes, readBytes, writeBytes, processCpu100ns, availablePhysicalBytes]` every 250 ms |

A frame is `{m, rva, start?, id?, idOff?, sym?, symOff?}`: module index, RVA of the return address (of the
pc for a leaf), unwind function start from `.pdata`, the Address Library id and offset for game code, and for
other modules the DbgHelp symbol and the displacement from it.

A thread is `{tid, name, main, frameThread?, samples, walked, suspendedMs, throttled, runs}`. `runs` holds
`[startMs, stack, count, flags]`: `count` consecutive ticks with the same stack and flags starting at
`startMs`. For threads other than the main one each tick stands for `otherEvery` main-thread ticks, so their
counts are already weighted. Flags:

| Bit | Meaning |
|-----|---------|
| 1 | Waiting: the thread used no CPU since the last tick and was not suspended |
| 2 | Reused: the stack is the previous one, not a fresh walk |
| 4 | Throttled: the suspension budget was spent, so the previous stack was kept |

## profile.json.gz

The [Firefox Profiler](https://profiler.firefox.com) *processed* format, `meta.preprocessedProfileVersion`
49, the version `samply` writes; the profiler upgrades it when it loads. One thread per sampled thread
(main first), each with `frameTable`, `funcTable`, `resourceTable` (one per module), `stackTable`,
`samples` (`time`, `stack`, `weight` = run length), `markers` and `stringArray`. Samples of waiting runs
use the `Idle` category, so "Include idle samples" switches them. Markers: `Frame` per presented frame
(`frame`, `ms`), `Hitch` per slow frame (`ms`, `waitingPct`, `top`), and on the main thread `Mark` per entry
of the raw `marks` (`kind`, `label`: loads, menus, RPC handlers, `Profile.mark` and recorder overlays; a
zero-length mark is an instant marker), and with GPU timing `GpuFrame` per timed frame (its aligned GPU span; `gpuMs`, `gpuBusyMs`, `cpuMs`, `excessMs`, `utilPct`,
`bound`) and `GpuPass` per pass (`pass`, `kind`, `frame`, `ms`; consecutive spans of the same target merged).
GPU tracks: `gpu_frame_ms` (span per frame) and `gpu_util_pct`. Counters (`counters[]`) use the
`display` block of current profiler versions and store values as deltas.

`skyrim_devtools.profile_firefox.Export` builds it; `add_markers` and `add_counter` add marker sets and
counter tracks, and `profiling.profile_firefox_data(profile)` is the one place that assembles a profile's
export, so new data sources extend it there. `profile_firefox.check(data)` validates the tables.

## profile.sqlite

Tables and views are listed in [profiling](profiling.md#sqlite-export). `Frames` carries, besides the frame and
GPU times, the classifier inputs: `excess_ms` (GPU span minus the CPU's issue span), `gpu_busy_ms` (the GPU
span when the GPU fell behind, else a utilisation-based estimate or null), `util_pct` and `bound`
(`gpu`, `cpu` or `capped`).

## report.json, *.folded

`report.json` is the summary `report --json` prints. `cpu.folded` and `wait.folded` are Brendan Gregg folded
stacks (`root;...;leaf count`) with module-run collapsing applied.

## Names

Names are not stored in `raw.json.gz`; they are resolved when a report is written (see
[profiling](profiling.md#symbols)). Frames of modules without real debug symbols are labelled
`module+0xOFFSET` (function start from `.pdata`) or `module!Export` when the address is at or just after an
export (Windows DLLs show the bare export name, such as `NtWaitForSingleObject`), and a run of two or more `module+0x` frames of the same module collapses into one `[module]` frame.
