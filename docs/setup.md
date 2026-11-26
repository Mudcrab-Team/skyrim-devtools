# Setup: build, host and runner

Detailed build and host setup. The README has the short version.

## Windows build

Target Windows x64 with Visual Studio 2022, CMake, and these local source checkouts.

- COMMUNITY(alandtse/CommonLibSSE-NG@7c27a75da3eb0db080c1f1f63f6df1b7cd733c2a, GPL-3.0-or-later).
  The plugin calls the pinned community headers and linked library. See the upstream
  [source and notices](https://github.com/alandtse/CommonLibSSE-NG/tree/7c27a75da3eb0db080c1f1f63f6df1b7cd733c2a)
  and [linking exceptions](https://github.com/alandtse/CommonLibSSE-NG/blob/7c27a75da3eb0db080c1f1f63f6df1b7cd733c2a/EXCEPTIONS.md).
- COMMUNITY(yhirose/cpp-httplib@7800c3dc5b33285cbb18baa5998de4ff7ef63515, MIT).
  The transport includes the upstream [WebSocket server header](https://github.com/yhirose/cpp-httplib/tree/7800c3dc5b33285cbb18baa5998de4ff7ef63515).
- COMMUNITY(nlohmann/json@55f93686c01528224f448c19128836e7df245f72, MIT).
  [JSON for Modern C++ v3.12.0](https://github.com/nlohmann/json/tree/55f93686c01528224f448c19128836e7df245f72).
- [MinHook](https://github.com/TsudaKageyu/minhook) (BSD-2-Clause) from the same vcpkg tree detours the
  input poll and the console printer.
- The Python client uses [websockets 16.0](https://github.com/python-websockets/websockets/tree/16.0) (BSD-3-Clause).

Install directxmath, directxtk, fmt, minhook, nlohmann-json, rapidcsv and spdlog with the
x64-windows-static-md triplet of [vcpkg 2026.10.16](https://github.com/microsoft/vcpkg/tree/5d4a3dc90c0b3773f3bac81bf41297b54328f471).
Set `$deps` to the directory containing your local dependency checkouts. The Release build selects CommonLib's AE
configuration. Keep the library, dependency tree, generated DLL, and PDB under the owner's
private .local tree. The linked DLL is for local testing.

~~~powershell
$scratch = Join-Path $env:USERPROFILE '.local\skyrim-devtools'
cmake -S native -B "$scratch/build" -G "Visual Studio 17 2022" -A x64 -DCMAKE_TOOLCHAIN_FILE="$deps/vcpkg/scripts/buildsystems/vcpkg.cmake" -DVCPKG_MANIFEST_MODE=OFF -DVCPKG_TARGET_TRIPLET=x64-windows-static-md -DCOMMONLIB_SOURCE="$deps/commonlib" -DHTTPLIB_SOURCE="$deps/httplib"
cmake --build "$scratch/build" --config Release --parallel 6
~~~

### Fast builds from the Mac

`tools/pc-build` (Python, runs on the Mac, same `--config` as the smoke runner or
`SKYRIM_DEVTOOLS_CONFIG`) builds the plugin on the test PC with Ninja in a persistent build
directory per worktree. It sends only the source files whose content changed, so Ninja
recompiles just those. The DLL target uses a precompiled header (`native/pch.h`).

~~~sh
tools/pc-build commonlib          # once per CommonLib pin: shared Release build, installed under the scratch root
tools/pc-build                    # incremental build, prints timings
tools/pc-build --deploy --lock-wait 900   # build, then copy the DLL while holding agent.lock
tools/pc-build lock --lock-wait 900       # hold agent.lock for a live run, release with: tools/pc-build unlock
tools/pc-build lock --refresh             # heartbeat while holding it (non-zero exit if the lock is no longer yours)
tools/pc-build hold --lock-wait 900 -- CMD...  # lock, run CMD with an automatic heartbeat every 4 min, unlock; stops CMD after 20 min
~~~

`agent.lock` counts as stale 25 minutes after its last write when no game is running, and the next waiter
takes it. A holder that works longer than that, or idles between game launches, must run
`lock --refresh` at least every few minutes (`tools/soak.py` does so every cycle) and stop when it
fails. Check the exit code of `lock`: 0 means held, anything else means not held.

Without the shared CommonLib the build falls back to compiling it inside the build directory
(`-DCOMMONLIB_PREFIX` selects the prebuilt one; `commonlib-options.cmake` keeps both paths on the
same ABI options). `--deploy` refuses unless it can create `agent.lock` atomically (or already
owns it) and never copies while another agent holds it; `--keep-lock` keeps it for the live run.
Measured on the test PC: shared CommonLib 2 min once, cold plugin build 16 s, rebuild after
editing one module 2 to 6 s, rebuild of every module 15 s (the Visual Studio generator took 7.6 min cold and
2.5 min after any full source resync).

## Offline desktop setup

On the interactive test account, use %USERPROFILE%\.local\skyrim-devtools as the
scratch root with the following layout.

- **host.ps1** copied from [windows/host.ps1](windows/host.ps1). Use the same account for SSH and the interactive Windows task.
- **game/** with an owner-local stock copy of the executable and assets at the
  [repository target pin](target.json). Stage the official
  [SKSE 2.3.1 Steam release](https://skse.silverlock.org/) in this copy. In its Data/SKSE/Plugins
  directory stage SkyrimDevTools.dll and the matching
  [Address Library](https://www.nexusmods.com/skyrimspecialedition/mods/32444) table
  versionlib-1-7-104-0.bin. Use a clean load order and one plugin DLL.
- **profile/** with private Skyrim.ini and SkyrimPrefs.ini copies and a Saves subdirectory.
  Set [General] sLocalSavePath=Saves\ in Skyrim.ini. Set bSaveOnPause, bSaveOnRest, bSaveOnTravel
  and bSaveOnWait to 0 in SkyrimPrefs.ini. The game offers no switch to move
  My Games\Skyrim Special Edition (sLocalSavePath moves saves only), so each run reaches the private profile
  through a Documents junction that host.ps1 creates when the run starts and removes when it ends.
- **fixture.ess** with a locally owned vanilla save that loads with that load order and starts
  in a quiet location with prompts dismissed. Each launch empties the scratch save directory,
  so saves the game wrote in an earlier run are discarded. It then copies this file in as
  Fixture.ess, along with any .ess and .skse files from an optional **fixtures/** directory.
- **config.json** with {"port":48682,"cloudSyncDisabled":true} after disabling Steam Cloud for Skyrim.
  Optional keys are `headless` (default false), `width` and `height` (1280x720),
  `muteAudio` (true), `watchdogMinutes` (240, 0 turns it off), `idleTimeoutSeconds` (1800) and
  `connectTimeoutSeconds` (600) and `modules` (a comma-separated plugin module allow-list, default all). For the two timeouts, 0 turns them off.

The first launch copies Skyrim.ini and SkyrimPrefs.ini to profile/ini-original/. Every launch
writes the effective INIs from those copies. A headless launch adds bAlwaysActive=1, bFull
Screen=0, bBorderless=1, the configured iSize W/H and fAudioMasterVolume=0. A windowed launch
restores the originals unchanged. Edit settings in ini-original/ from then on.

Put Steam in [Offline Mode](https://help.steampowered.com/en/faqs/view/0E18-319B-E34B-B2C8)
and keep the test account logged in. Run the setup command once as that user. The task runs
in that interactive session because the game needs a desktop window. A windowed launch also
requires the desktop to be unlocked. A headless launch only requires an active session: the
plugin moves the window off-screen and hands focus back to the shell, and the game keeps
rendering there with bAlwaysActive. Preflight reports an inactive or locked desktop,
unavailable Steam, unconfirmed Offline Mode or unconfirmed Steam Cloud settings before it
starts the loader. Headless runs on a locked desktop have not been tested yet.

The junction exists only while a run owns the game. Entering a run renames an existing real
My Games\Skyrim Special Edition folder to `Skyrim Special Edition.DevTools-Real` (a rename, contents untouched)
and creates the junction. Leaving the run (stop, crash, kill or failed preflight) removes the junction link
with `rmdir` and renames the backup back. The next `start` and `host.ps1 restore-profile` finish that
restore if a host died mid-run. If a real folder and the backup both exist, or the folder is a junction to
anywhere else, the host stops with an error and changes nothing. The files directly inside
%LOCALAPPDATA%\Skyrim Special Edition (Plugins.txt, ContentCatalog.txt) cannot be redirected, so they are
copied aside at run start and restored afterwards.

~~~powershell
Copy-Item windows/host.ps1 "$scratch/host.ps1"
powershell -NoProfile -ExecutionPolicy RemoteSigned -File "$scratch/host.ps1" install-task
~~~

## Run from the Mac

Use a key-based SSH connection to the Windows test account. Create
`.local/devtools-mac.json` with your SSH alias.

~~~json
{"sshDestination":"skyrim-test","localPort":48681}
~~~

Set `sshConfig` if the alias is in a separate SSH config file.

~~~sh
python3.13 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[video]'   # [video] adds Pillow for skyrim-devtools-video
skyrim-devtools-smoke --config .local/devtools-mac.json --cycles 5 --tab-trials 20 --mode headless
python -m unittest discover -s tests
~~~

`--screenshot out.png` also saves the world and Inventory views and samples frame times.
Use --session-only to exercise launch, handshake, fixture load and shutdown. --mode overrides
the host's `headless` setting for one run. The runner
prints aggregate counts. The private Windows runs directory holds the launcher status
and RPC transcript. The profile holds the game and loader logs.


## Prior art reviewed

- [SkyrimNet Game Plugin](https://github.com/FatelessAI/SkyrimNet-GamePlugin/tree/c65a32e1b58bf723b3bd003a305cfa79367ff6e9)
  integrates an in-process Skyrim mod with a companion AI engine. Its custom license restricts
  redistribution. The MIT Papyrus API covers integrations with that mod.
- [Skyrim CrowdControl](https://github.com/Superxwolf/Skyrim-CrowdControl/tree/edb26e4e1d17c9196ea9620837d98bd6f3e8b111)
  uses an MIT-licensed connector and Papyrus event handling for Warp World TCP commands.
  It initiates an outbound connection and uses a different command protocol.
- [NL_CMD](https://github.com/MrOctopus/nl_cmd/tree/e13d3c7f0e45fbc6ca8efdffb391a3261a3a3e82)
  provides MIT-licensed Papyrus console registration and execution with an additional game plugin.
- [Skyrim Platform](https://github.com/skyrim-multiplayer/skymp/tree/2849e676fe008925384b26f34476e382a30bd877/skyrim-platform)
  is a GPLv3 JavaScript and TypeScript runtime with a broader scripting surface and Chromium integration.
- [SkyLink AI](https://github.com/maksidze/Skyrim-MCP/tree/82a0a80d31ae7d2254d40c129650d1e260da38a2)
  states MIT in its README. It uses a named pipe and a separate .NET MCP bridge for many game actions.

- [devbench](https://github.com/alandtse/devbench) (GPL-3.0) injects input at the same input
  poll. Its approach was studied. No code was copied.

These projects were reviewed as source references. The runner uses the pinned libraries above
directly and includes original host, protocol, and game adapter code.

