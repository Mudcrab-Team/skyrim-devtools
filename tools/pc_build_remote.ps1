# SPDX-License-Identifier: GPL-3.0-or-later
# Runs on the Windows test PC under the scratch root; driven by tools/pc-build.
param(
    [Parameter(Mandatory)][ValidateSet('build', 'commonlib', 'lock', 'unlock', 'sync')][string]$Action,
    [string]$Name = 'default',
    [string]$Agent = '',
    [switch]$Deploy,
    [switch]$Wipe,
    [switch]$Reconfigure,
    [switch]$NoPrebuilt,
    [int]$LockWait = 0,
    [switch]$KeepLock
)
$ErrorActionPreference = 'Continue'
$root = Join-Path $env:USERPROFILE '.local\skyrim-devtools'
$deps = Join-Path $root 'deps'
$cmake = Join-Path $deps 'cmake\data\bin\cmake.exe'
$prefix = Join-Path $root 'commonlib-install'
$lockPath = Join-Path $root 'agent.lock'
$clock = [Diagnostics.Stopwatch]::StartNew()

function Step($label, [scriptblock]$body) {
    $t = [Diagnostics.Stopwatch]::StartNew()
    $global:LASTEXITCODE = 0
    & $body
    if ($LASTEXITCODE -ne 0 -and $null -ne $LASTEXITCODE) { Write-Output "FAILED $label (exit $LASTEXITCODE)"; exit 1 }
    Write-Output ("[time] {0} {1:N1}s" -f $label, $t.Elapsed.TotalSeconds)
}

function Import-VcVars {
    $vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
    $vs = & $vswhere -latest -products * -property installationPath
    $bat = Join-Path $vs 'VC\Auxiliary\Build\vcvars64.bat'
    cmd /c "`"$bat`" >nul && set" | ForEach-Object {
        if ($_ -match '^([^=]+)=(.*)$') { Set-Item -Path "env:$($Matches[1])" -Value $Matches[2] }
    }
    $ninja = Join-Path $vs 'Common7\IDE\CommonExtensions\Microsoft\CMake\Ninja'
    $env:Path = "$ninja;$env:Path"
}

function Get-LockOwner { if (Test-Path $lockPath) { (Get-Content $lockPath -Raw).Trim() } else { $null } }

function Test-LockStale {
    if (-not (Test-Path $lockPath)) { return $false }
    $age = (Get-Date) - (Get-Item $lockPath).LastWriteTime
    return ($age.TotalMinutes -gt 25) -and -not (Get-Process SkyrimSE -ErrorAction SilentlyContinue)
}

# Waiting agents take a ticket (a file named by arrival time) next to agent.lock and are served oldest
# first. A ticket nobody has touched for a minute belongs to a dead waiter and is dropped. The lock itself
# is still created atomically, so the queue only decides who may try.
$queuePath = Join-Path $root 'agent.queue'
$ticketTtlSeconds = 60

function Get-Queue {
    if (-not (Test-Path $queuePath)) { return @() }
    $live = @()
    foreach ($file in (Get-ChildItem $queuePath -Filter '*.ticket' | Sort-Object Name)) {
        if (((Get-Date) - $file.LastWriteTime).TotalSeconds -gt $ticketTtlSeconds) {
            Remove-Item $file.FullName -Force -ErrorAction SilentlyContinue
        } else {
            $live += [pscustomobject]@{ Name = $file.Name; Agent = ($file.BaseName -replace '^\d+_', ''); Path = $file.FullName }
        }
    }
    return $live
}

function Acquire-Lock([string]$who, [int]$waitSeconds) {
    $deadline = (Get-Date).AddSeconds($waitSeconds)
    $ticket = $null
    try {
        while ($true) {
            if ((Get-LockOwner) -eq $who) { return $true }
            if (Test-LockStale) { Write-Host "lock held by '$(Get-LockOwner)' is stale; taking it"; Remove-Item $lockPath -Force }
            $queue = @(Get-Queue)
            $mine = $queue | Where-Object { $_.Agent -eq $who } | Select-Object -First 1
            $ahead = @($queue | Where-Object { $_.Agent -ne $who -and (-not $mine -or $_.Name -lt $mine.Name) })
            if ($ahead.Count -eq 0) {
                try {
                    $stream = [IO.File]::Open($lockPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::Read)
                    $bytes = [Text.Encoding]::ASCII.GetBytes($who)
                    $stream.Write($bytes, 0, $bytes.Length)
                    $stream.Close()
                    Write-Host "lock acquired by $who"
                    return $true
                } catch [IO.IOException] { }
            }
            if ((Get-Date) -ge $deadline) { return $false }
            New-Item -ItemType Directory -Force $queuePath | Out-Null
            if ($mine) { $ticket = $mine.Path; (Get-Item $ticket).LastWriteTime = Get-Date }
            else {
                $ticket = Join-Path $queuePath ('{0:D19}_{1}.ticket' -f [DateTime]::UtcNow.Ticks, $who)
                Set-Content $ticket $who
                Write-Host "queued as $who behind $($ahead.Count) waiting agent(s); lock held by '$(Get-LockOwner)'"
            }
            Start-Sleep -Seconds 5
        }
    } finally {
        foreach ($file in @(Get-ChildItem $queuePath -Filter "*_$who.ticket" -ErrorAction SilentlyContinue)) {
            Remove-Item $file.FullName -Force -ErrorAction SilentlyContinue
        }
    }
}

if ($Action -eq 'lock') {
    if (-not $Agent) { Write-Output 'lock needs an agent name'; exit 2 }
    if (Acquire-Lock $Agent $LockWait) { exit 0 }
    Write-Output "agent.lock is held by '$(Get-LockOwner)'"
    exit 4
}
if ($Action -eq 'unlock') {
    if (-not $Agent) { Write-Output 'unlock needs an agent name'; exit 2 }
    $owner = Get-LockOwner
    if ($owner -eq $Agent) { Remove-Item $lockPath -Force; Write-Output 'lock released' }
    elseif ($owner) { Write-Output "agent.lock belongs to '$owner'; left alone"; exit 4 }
    exit 0
}

if ($Action -eq 'sync') {
    $work = Join-Path $root "pc-build\$Name"
    $src = Join-Path $work 'src'
    $tar = Join-Path $root "pc-build-$Name.tar"
    if ($Wipe) { Remove-Item -Recurse -Force $src -ErrorAction SilentlyContinue }
    New-Item -ItemType Directory -Force $src | Out-Null
    # -m: extracted files get the current time, which is what makes Ninja rebuild exactly these.
    tar -xmf $tar -C $src
    if ($LASTEXITCODE) { exit 1 }
    Remove-Item $tar
    foreach ($r in (Get-Content (Join-Path $src 'removed.txt'))) {
        if ($r) { Remove-Item (Join-Path $src $r) -Force -ErrorAction SilentlyContinue }
    }
    Remove-Item (Join-Path $src 'removed.txt')
    exit 0
}

$vcpkg = Join-Path $deps 'vcpkg\scripts\buildsystems\vcpkg.cmake'
$triplet = '-DVCPKG_TARGET_TRIPLET=x64-windows-static-md'

if ($Action -eq 'commonlib') {
    # One shared Release build of the pinned CommonLibSSE-NG that every plugin build links.
    $src = Join-Path $root "pc-build\$Name\src"
    $opts = Join-Path $src 'commonlib-options.cmake'
    if (-not (Test-Path $opts)) { Write-Output 'sync the sources first'; exit 3 }
    $build = Join-Path $root 'commonlib-build'
    Step 'vcvars' { Import-VcVars }
    Step 'configure commonlib' {
        & $cmake -S (Join-Path $deps 'commonlib') -B $build -G Ninja -C $opts -DCMAKE_BUILD_TYPE=Release `
            "-DCMAKE_TOOLCHAIN_FILE=$vcpkg" -DVCPKG_MANIFEST_MODE=OFF $triplet "-DCMAKE_INSTALL_PREFIX=$prefix" 2>&1
    }
    Step 'build commonlib' { & $cmake --build $build 2>&1 }
    Step 'install commonlib' { & $cmake --install $build 2>&1 }
    exit 0
}

$work = Join-Path $root "pc-build\$Name"
$src = Join-Path $work 'src'
$build = Join-Path $work 'build'
if (-not (Test-Path (Join-Path $src 'CMakeLists.txt'))) { Write-Output 'no synced sources'; exit 3 }
if ($Deploy -and -not $Agent) { Write-Output 'deploy needs an agent name'; exit 2 }
if ($Deploy -and (Get-LockOwner) -ne $Agent) {
    # Fail before compiling when nobody could deploy anyway.
    $owner = Get-LockOwner
    if ($owner -and -not (Test-LockStale) -and $LockWait -le 0) { Write-Output "agent.lock is held by '$owner'; not deploying"; exit 4 }
}

Step 'vcvars' { Import-VcVars }
$usePrefix = (-not $NoPrebuilt) -and (Test-Path (Join-Path $prefix 'lib\cmake\CommonLibSSE'))
if (-not $usePrefix) { Write-Output 'note: no prebuilt CommonLib, building it inside this build dir (slow); run tools/pc-build commonlib' }
$stamp = Join-Path $build 'pc-build.prefix'
$mode = if ($usePrefix) { 'prefix' } else { 'source' }
if ((Test-Path $stamp) -and ((Get-Content $stamp -Raw).Trim() -ne $mode)) { $Reconfigure = $true }
if ($Reconfigure -and (Test-Path $build)) { Remove-Item -Recurse -Force $build }
if (-not (Test-Path (Join-Path $build 'build.ninja'))) {
    Step 'configure' {
        $args = @('-S', $src, '-B', $build, '-G', 'Ninja', '-DCMAKE_BUILD_TYPE=Release',
            "-DCMAKE_TOOLCHAIN_FILE=$vcpkg", '-DVCPKG_MANIFEST_MODE=OFF', $triplet,
            "-DCOMMONLIB_SOURCE=$(Join-Path $deps 'commonlib')", "-DHTTPLIB_SOURCE=$(Join-Path $deps 'httplib')")
        if ($usePrefix) { $args += "-DCOMMONLIB_PREFIX=$prefix" }
        & $cmake @args 2>&1
    }
    New-Item -ItemType Directory -Force $build | Out-Null
    Set-Content $stamp $mode
}
Step 'build' { & $cmake --build $build 2>&1 }

$dll = Join-Path $build 'SkyrimDevTools.dll'
if (-not (Test-Path $dll)) { Write-Output "missing $dll"; exit 1 }
Write-Output ("dll {0} bytes" -f (Get-Item $dll).Length)

if ($Deploy) {
    $heldBefore = (Get-LockOwner) -eq $Agent
    if (-not (Acquire-Lock $Agent $LockWait)) { Write-Output "agent.lock is held by '$(Get-LockOwner)'; not deploying"; exit 4 }
    try {
        $dest = Join-Path $root 'game\Data\SKSE\Plugins'
        Step 'deploy' { Copy-Item $dll (Join-Path $dest 'SkyrimDevTools.dll') -Force }
    } finally {
        if (-not $KeepLock -and -not $heldBefore) { Remove-Item $lockPath -Force -ErrorAction SilentlyContinue; Write-Output 'lock released' }
    }
}
Write-Output ("[time] remote total {0:N1}s" -f $clock.Elapsed.TotalSeconds)
