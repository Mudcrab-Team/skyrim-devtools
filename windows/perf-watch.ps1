# Samples host-wide counters once a second into a CSV, so a frame hitch can be set against memory, paging,
# disk, CPU per process, GPU load and the game's thread count.
#   perf-watch.ps1 -Out FILE -Detach    start it detached from this session (survives an ended SSH session or job)
#   perf-watch.ps1 -Out FILE -Stop      stop the sampler that writes FILE
# Without -Detach it runs in the foreground (and ends with its parent).
param(
    [Parameter(Mandatory)] [string] $Out,
    [int] $IntervalMs = 1000,
    [switch] $Detach,
    [switch] $Stop
)
$ErrorActionPreference = 'SilentlyContinue'
if (![IO.Path]::IsPathRooted($Out)) { $Out = Join-Path (Get-Location).Path $Out }
if ($Stop) {
    $id = [int](Get-Content -LiteralPath "$Out.pid")
    $target = Get-CimInstance Win32_Process -Filter "ProcessId=$id"
    # the pid file may outlive the sampler; only a PowerShell running this script is ours to stop
    if ($target -and $target.CommandLine -like '*perf-watch*') { Stop-Process -Id $id -Force }
    return
}
if ($Detach) {
    # A process created through WMI is not a child of the calling session, so it is not killed with it.
    $command = 'powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}" -Out "{1}" -IntervalMs {2}' -f $PSCommandPath, $Out, $IntervalMs
    $made = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $command }
    $made.ProcessId
    return
}
Set-Content -LiteralPath "$Out.pid" -Value $PID
$cores = [Environment]::ProcessorCount
$counters = @(
    '\Memory\Available MBytes', '\Memory\Pages/sec', '\Memory\Page Faults/sec', '\Memory\% Committed Bytes In Use',
    '\PhysicalDisk(_Total)\Avg. Disk Queue Length', '\PhysicalDisk(_Total)\Disk Bytes/sec',
    '\PhysicalDisk(_Total)\Avg. Disk sec/Transfer', '\Processor(_Total)\% Processor Time',
    '\Processor(_Total)\% DPC Time', '\Processor(_Total)\% Interrupt Time', '\Network Interface(*)\Bytes Total/sec',
    '\GPU Engine(*engtype_3D)\Utilization Percentage', '\GPU Adapter Memory(*)\Dedicated Usage',
    '\GPU Adapter Memory(*)\Shared Usage'
)
$previous = @{}
'epochMs,availMB,pagesSec,faultsSec,commitPct,diskQueue,diskBytesSec,diskSecXfer,cpuPct,dpcPct,intPct,netBytesSec,gpu3dPct,gpuDedicatedMB,gpuSharedMB,gameThreads,top' |
    Set-Content -LiteralPath $Out
$interval = [Math]::Max(1, [int]($IntervalMs / 1000))
while ($true) {
    # One streaming query per minute: a single call samples every counter together once per interval, so the rows
    # keep the requested cadence (a query per counter family cost 3-4 s each round). Restarting it re-expands the
    # wildcards, which picks up GPU engines and adapters that appeared since.
    Get-Counter -Counter $counters -SampleInterval $interval -MaxSamples (60 / $interval) | ForEach-Object {
        $v = @{}
        $net = 0.0
        $gpu = 0.0
        $dedicated = 0.0
        $shared = 0.0
        foreach ($s in $_.CounterSamples) {
            $path = $s.Path -replace '^\\\\[^\\]+', ''
            if ($path -like '\network interface*') { $net += $s.CookedValue }
            elseif ($path -like '\gpu engine*') { $gpu += $s.CookedValue }
            elseif ($path -like '\gpu adapter memory*dedicated*') { $dedicated = [Math]::Max($dedicated, $s.CookedValue) }
            elseif ($path -like '\gpu adapter memory*shared*') { $shared = [Math]::Max($shared, $s.CookedValue) }
            else { $v[$path] = $s.CookedValue }
        }
        $now = @{}
        $threads = ''
        $top = foreach ($p in Get-Process) {
            $now[$p.Id] = $p.TotalProcessorTime.TotalSeconds
            if ($p.ProcessName -eq 'SkyrimSE') { $threads = $p.Threads.Count }
            if ($previous.ContainsKey($p.Id)) {
                [pscustomobject]@{ Name = $p.ProcessName; Cpu = ($now[$p.Id] - $previous[$p.Id]) * 100 / $cores / $interval }
            }
        }
        $previous = $now
        $names = ($top | Sort-Object Cpu -Descending | Select-Object -First 4 | ForEach-Object { '{0}:{1:F1}' -f $_.Name, $_.Cpu }) -join ' '
        '{0},{1},{2:F0},{3:F0},{4:F1},{5:F2},{6:F0},{7:F4},{8:F1},{9:F1},{10:F1},{11:F0},{12:F1},{13:F0},{14:F0},{15},{16}' -f
            $_.Timestamp.ToUniversalTime().Subtract([datetime]'1970-01-01').TotalMilliseconds.ToString('F0'),
            $v['\memory\available mbytes'], $v['\memory\pages/sec'], $v['\memory\page faults/sec'],
            $v['\memory\% committed bytes in use'], $v['\physicaldisk(_total)\avg. disk queue length'],
            $v['\physicaldisk(_total)\disk bytes/sec'], $v['\physicaldisk(_total)\avg. disk sec/transfer'],
            $v['\processor(_total)\% processor time'], $v['\processor(_total)\% dpc time'],
            $v['\processor(_total)\% interrupt time'], $net, $gpu, ($dedicated / 1MB), ($shared / 1MB), $threads, $names |
            Add-Content -LiteralPath $Out
    }
}
