# Record, replay and A/B benchmarks

Capture a play-through once, then replay the same path and conditions as often as needed. A replay measures
the *change* between two configurations (a shader toggle, an INI value, a mod feature), not the player's hands.
This is separate from `record_start` / `Record.*`, which log a JSONL trajectory for diagnostics and have no scene.

The tools are `recording_start`, `recording_stop`, `recording_list`, `replay`, `replay_stop` and `bench_ab` (MCP), and
the `Recording.*` methods in [protocol.md](protocol.md#record-and-replay-of-play-throughs).

## Recording

`recording_start` samples, every `interval_ms` (10 to 1 800 000, default 50):

- the player's position and heading/pitch, and the camera's point of view (`firstPerson`, `thirdPerson`, ...) with its
  position and angles;
- **cell transitions** (doors, `coc`/`cow`, loads) as events with the destination cell, how it was reached (`via`: a
  guess from what was seen shortly before) and the pose after it;
- **console commands**, whether typed by a person or sent by a tool, with their time and source (`typed` / `api`);
- the **entry point**: the save that was loaded or the `coc` target, and its *age* before recording began, plus the
  cell, worldspace, time of day and weather at that moment.

`recording_stop` writes `recordings/<name>.json` in the run directory. Recording stops by itself at 60 000 samples or
30 minutes (`limitReached`). It works the same whether a person plays or an agent drives the game through other tools.

### File format (`skyrim-devtools-recording`, version 1)

```json
{"format":"skyrim-devtools-recording","version":1,
 "meta":{"name":"riverwood-walk","intervalMs":100,"runtime":"se","pluginBuild":"...","durationMs":60012,
         "coupling":{"tier":"cell"}},
 "entry":{"kind":"save","save":"Fixture","ageMs":4200,"cell":{"formId":"...","interior":false},
          "worldspace":"Tamriel","pos":[x,y,z],"rot":[yaw,pitch],"gameHour":9.4,"weather":"..."},
 "sampleColumns":["tMs","x","y","z","yawDeg","pitchDeg","pov","camX","camY","camZ","camYawDeg","camPitchDeg"],
 "events":[{"type":"command","t":10120,"command":"tgm","source":"typed"},
           {"type":"cell","t":31000,"via":"door","to":{"formId":"...","interior":true,"editorId":"..."},"pos":[x,y,z]}],
 "samples":[
 [0,100.0,200.0,300.0,90.0,0.0,"thirdPerson",...],
 ...
 ]}
```

One sample per line, so a recording is diffable and hand-editable. `meta.coupling` may pin the coupling tier or its
thresholds for this recording. `skyrim_devtools.recording` loads, validates and summarises a file without the game.

## Replay

`replay` first puts the scene back, then walks the player along the recorded poses on the recording's own clock:

1. **Restore.** For a `save` entry the save is loaded; for a `coc` entry the target is entered. With
   `cleanTransition` (default on) a `coc` first bounces through a small cell (`cleanTransitionCell`, default `QASmoke`)
   to force a real loading screen: a raw `coc` can stream between scenes without the teardown some mods need.
2. **Settle.** `loadSettleMs` (default 3000) lets the world finish loading. It is hardware dependent, so it is a
   setting, not part of the file.
3. **Scene coupling**, chosen by the entry's age before recording began (`couplingAnchorMs` 10 000, `couplingCellMs`
   60 000, or the recording's own `meta.coupling`, or the `coupling` argument):

   | Tier | Entry age | What the replay does |
   | --- | --- | --- |
   | `anchored` | up to 10 s | restores the entry and sets the recorded time of day and weather again |
   | `cell` | up to 60 s | restores the entry |
   | `worldspace` | older, or unknown | does not restore; only checks the player is in the recorded worldspace (or interior cell) |

   A scene mismatch (the player is not where the recording starts) fails the replay with `sceneMismatch` unless
   `force`. `restoreScene=false` behaves like `worldspace`.
4. **Run.** `beforeRun` console commands run (this is where a benchmark's toggle goes: after a load, so the load cannot
   undo it), then the clock starts. Each frame the player is put on the interpolated recorded pose and facing, the
   point of view is applied, recorded console commands run at their times (a `coc`/`cow` command is not repeated; the cell
   event reproduces it), and a recorded cell change is reproduced with `coc <interior>` or `cow <worldspace> x y`, pausing
   the clock until the destination is loaded.

The replay is pose playback: it does not press keys, so physics, animations and AI are the game's own. The poses
the engine reports each frame before the next one is applied are kept as the *observed* path (`Recording.replayTrace`);
`engine drift` in the result is how far the engine moved the player from the pose just set (collision, ground snapping).
`replay` with `wait` also reports `divergenceVsRecording`: median, p95 and max distance between observed and recorded
poses at the same times. Two replays can be compared with `skyrim_devtools.recording.divergence`.

### Events

| Event | When | Data |
| --- | --- | --- |
| `record.started` | recording began | name, intervalMs, entry |
| `record.stopped` | file written | name, path, samples, events, durationMs |
| `replay.started` | the run clock starts, after the restore | name, tier, durationMs, restore info |
| `replay.step` | about every `stepEveryMs` (1000) | tMs, index, pos |
| `replay.finished` | the replay ended | finished, tier, drift, cellMismatches, commandsRun, error? |

A profiler or video capture aligns to `replay.started` and `replay.finished` (and `Profile.mark` if wanted).

### Hotkeys and autorun

Settings are read at start from `recording.json` in the plugin's run directory and can be changed with
`Recording.configure`:

```json
{"recordHotkey": 120, "recordHotkeyShift": true, "replayHotkey": 121, "replayHotkeyShift": true,
 "replayPath": "", "replayRestoreScene": true, "recordIntervalMs": 50,
 "autoRunPath": "riverwood-walk", "autoRunRestoreScene": true, "loadSettleMs": 3000,
 "couplingAnchorMs": 10000, "couplingCellMs": 60000,
 "cleanTransition": true, "cleanTransitionCell": "QASmoke"}
```

- Hotkeys are Windows virtual-key codes (0 disables; 120 is F9). They are ignored while the console is open or the game
  window is in the background. A HUD message confirms each action. `replayPath` empty means the newest recording.
- **Autorun** replays `autoRunPath` once, on the first successful load of the session (an unattended benchmark). If the
  recording's entry save is the save that was just loaded, the restore is skipped instead of loading it twice.

## A/B benchmarks

`bench_ab {recording, a, b, runs=3}` replays the recording `runs` times under configuration A and `runs` times under B,
alternating A B A B so slow drift (thermals, background work) falls on both (`order: "blocks"` runs all A first). Each
run restores the scene, applies its configuration, replays, and reads the frame statistics of the run part only
(`Perf.reset` when the run starts). With the profiler available (`Profile.start` in the plugin and the profiling module
installed) each run is also profiled for GPU pass times; without it the report says so and goes on.

A configuration is a list of console commands or

```json
{"label": "shadows-off",
 "console": ["setini ...", "set GameHour to 12"],
 "rpc": [{"method": "Mod.setFeature", "params": {"enabled": false}}],
 "cleanup": {"console": [], "rpc": []}}
```

`console` commands run in the plugin after the restore and before the run, so a toggle that lives in a save cannot be
undone by the load; `rpc` calls (a mod's own tool) are made before the replay starts and survive only what the restore does
not reset; `cleanup` is undone once after all runs. Make console toggles idempotent (`set X to 1`, not a toggle command):
the same command runs on every run of that side.

The report gives, for p50, p95, p99, mean, max, fps, hitches (frames over 50 ms) and GPU time and passes, each side's mean and run-to-run standard
deviation and the delta B - A with a percentage. A delta is called real (`B better` / `B worse`) only when it is more
than twice its standard error, `within noise` otherwise, and with fewer than two runs per side the verdict says that noise
cannot be judged. A warning appears when the run-to-run noise exceeds 5% of the p50 frame time. Failed runs are excluded and
named in `warnings`. Plan about `2 x runs x (recording length + a load)`; use a short recording.

Limitations: the window between the replay entering its run phase and the first `Perf.reset` is up to one poll (0.25 s) of
a run; GPU pass times need frame boundaries from the profiler's Present hook and are absent on hardware it cannot time.

## Python

```python
from skyrim_devtools import bench, recording

doc = recording.load("riverwood-walk.json")          # validate, summarise
recording.divergence(trace_rows, doc["samples"])      # median/p95/max distance
report = await bench.bench_ab(call, "riverwood-walk", ["set GameHour to 12"], ["set GameHour to 0"], runs=3)
print(bench.render_text(report))
```
