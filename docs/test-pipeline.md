# Live test pipeline

The game runs on one test PC, and only one session can hold it at a time. When several agents are building
features, each of them waiting for the PC lock to run its own live test serialises everything: with six
implementers, most of the wall-clock time is spent in the lock queue, not building or testing.

This document describes how live testing works in two modes, and when to use which.

## Two modes

| Mode | When | Who runs the game |
|---|---|---|
| **Direct** | 1–2 agents need the PC | Each implementer takes the lock (`tools/pc-build lock` / `hold`) and tests its own change, as before. |
| **Batched** | 3 or more agents need the PC, or the lock queue is longer than about 15 min | Implementers submit test requests; one **Tester** agent holds the PC and tests everything queued in one session. |

The orchestrator picks the mode and says so in each implementer brief. The rule of thumb: batch when
the expected wait for the lock is longer than the batch cycle (about 30–45 min). With few agents,
the queue's friction (writing requests, waiting for a report, a second round for fixes) costs more than
it saves.

Long playtests (a whole quest, a soak) are always their own session. In batched mode they are queue
entries of kind `playtest` that get an exclusive slot between batches.

## Roles (batched mode)

**Implementer.** Builds the feature, writes unit tests, compiles the plugin on the PC in its own build
directory (building needs no lock), and writes the live test. Never launches the game. Submits a test request
and continues with other work (docs, unit tests, the next item) until the report comes back.

**Tester.** The only agent that holds the PC lock. Its sole job is to integrate queued changes, run their live
tests, and report evidence. It does not fix features; it may fix its own harness and trivial test-script
bugs (a typo, a wrong ref), and says so in the report.

**Orchestrator.** Starts the Tester when the queue is non-empty and no batch runs, routes reports back to
implementers, and decides when to switch modes.

## Test request

One file per request in the queue directory (`.local/testq/` in the main checkout, ignored by git):

```yaml
id: stuck-detector-2            # unique; bump the suffix on resubmission
branch: m1-stuck
sha: 1a2b3c4                    # the exact commit to test; a different SHA needs a new request
pr: 21                          # draft PR, if open
implementer: stuck              # who receives the report
summary: >
  Movement stuck detector (no displacement, no progress, oscillation, door loops),
  portal clearance, multi-level nav.
touches: [native/nav_stuck.cpp, native/nav_plan.cpp, skyrim_devtools/use/nav.py]
setup: riverwood-fixture        # a save, a scenario file, or a scenario_setup call
cases:
  - name: wall-walk
    script: tests/live/stuck_wall.py      # preferred: a scripted check
    expect: "stuck detected within 5 s; recovered or stopped with error 'stuck' within 20 s"
  - name: bridge
    steps: "Nav.goto from the river bank to the Riverwood bridge deck, 3 passes"
    expect: "arrives on the deck (z within 50 of the deck) all 3 times; no walking under it"
    judge: visual                         # needs the Tester's judgement from screenshots
est_minutes: 12
needs_fresh_game: false         # true if a case leaves the game in a bad state (crash tests, menus)
artifacts: [timeline, screenshots]   # also: video clip, perf, profile
```

Rules for implementers:

- Prefer scripted cases (`tests/live/<name>.py`, see below). Use `steps` + `judge: visual` only for what a
  script cannot decide.
- `expect` must be checkable: numbers, events, error codes, positions. "Works well" is not a criterion.
- Keep a request under about 15 min of game time. Split larger ones.
- Declare `touches` truthfully; the Tester uses it to predict merge conflicts and to attribute failures.

## Live test scripts

`tests/live/<name>.py` uses a small harness: `run(ctx) -> dict` with `ctx.client` (JSON-RPC client),
`ctx.setup(name)` (load a save or apply a scenario), `ctx.screenshot(label)`, `ctx.mark(text)` (timeline
marker) and `ctx.wait_event(name, timeout)`. It returns `{"pass": bool, "details": {...}}` with the measured
numbers. `skyrim-devtools-livetest run <request.yaml> [--case NAME]` runs one request's cases;
`skyrim-devtools-livetest batch <dir>` runs a whole batch and writes per-request reports. Scripts are short and
deterministic; they never sleep for fixed long times when an event or a poll can decide.

## A batch

1. **Collect.** The Tester takes every request in the queue at start. Requests that arrive during a batch wait
   for the next one.
2. **Integrate.** Make an integration branch from `origin/main` and merge each request's SHA in. A request
   that conflicts or breaks the build is dropped from the batch and reported right away with the error;
   the rest continue. Build the plugin once and deploy it.
3. **Order.** Group cases by `setup` so each save or scenario loads once per group. Run `needs_fresh_game`
   requests last, or restart the game before them.
4. **Run.** For each case: reset to its setup, run, collect artifacts, and record pass/fail with the measured
   numbers. Call `perf_check` at the start and every about 15 min, so a slow PC is noticed, not mistaken for
   a feature failure.
5. **Attribute.** A failure in a case whose code paths another request also touches (overlapping `touches`)
   is ambiguous: rerun that case on a build with only its own request. Crashes are attributed to the
   running case; the Tester restarts the game and continues with the next case.
6. **Report.** One report per request, `.local/testq/results/<id>.md`: per case pass/fail, measured values
   vs `expect`, artifact paths, Tester notes, and anything odd seen in passing. The orchestrator forwards it to
   the implementer.
7. **Release.** Delete the integration branch, close the game unless the next batch starts within a few
   minutes, release the lock.

A batch should take under about 45 min. If the queue holds more than that, the Tester runs the oldest
requests first and leaves the rest for the next batch.

## After the report

- **All pass:** the implementer rebases its branch on `origin/main`. If the rebase changed nothing in native
  code or the tested Python paths, it merges the PR. Otherwise it resubmits with the new SHA (a quick
  request: only the affected cases).
- **Some fail:** the implementer fixes and resubmits (new `id` suffix, new `sha`, only the failing cases plus
  anything the fix could break).
- **Blocked on live iteration** (tuning that needs many short tries): the implementer writes a sweep script
  that tries the parameter values in one case and reports a table, instead of asking for interactive time.
  If that is impossible, it asks the orchestrator for a debug slot (at most 15 min, exclusive, between
  batches).

## The Tester's other output

Because the Tester uses every new tool, it is also the first user. Each batch report ends with:

- bottlenecks it hit, one line each with an estimated time cost;
- tool friction (confusing results, missing fields, slow calls) as short suggestions;
- perf numbers for the session (fps median, p99, hitches).

## Switching modes

- **To batched:** the orchestrator tells running implementers to stop waiting in the lock queue, submit a
  request instead, and continue with their non-live work. Tickets already in the lock queue are cancelled by
  their owners.
- **To direct:** when the queue empties and fewer than 3 implementers are active, the orchestrator stops
  starting Testers; implementers go back to taking the lock themselves.

## Not in scope

- Running more than one game session at a time on the PC.
- A server or database for the queue. Files in a directory are enough at this scale.
- Replacing unit tests: requests must arrive with unit tests passing.
