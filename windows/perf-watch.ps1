# Samples host-wide counters once a second into a CSV, so a frame hitch can be set against memory, paging,
# disk, CPU per process and GPU load. Stop it with: Stop-Process -Name powershell -Id (Get-Content perf-watch.pid)
param(
    [Parameter(Mandatory)] [string] $Out,
    [int] $IntervalMs = 1000
)
$ErrorActionPreference = 'SilentlyContinue'
Set-Content -LiteralPath "$Out.pid" -Value $PID
$cores = [Environment]::ProcessorCount
$counters = @(
    '\Memory\Available MBytes', '\Memory\Pages/sec', '\Memory\Page Faults/sec', '\Memory\% Committed Bytes In Use',
    '\PhysicalDisk(_Total)\Avg. Disk Queue Length', '\PhysicalDisk(_Total)\Disk Bytes/sec',
    '\PhysicalDisk(_Total)\Avg. Disk sec/Transfer', '\Processor(_Total)\% Processor Time',
    '\Processor(_Total)\% DPC Time', '\Processor(_Total)\% Interrupt Time', '\Network Interface(*)\Bytes Total/sec'
)
$previous = @{}
$stamp = [Diagnostics.Stopwatch]::StartNew()
'epochMs,availMB,pagesSec,faultsSec,commitPct,diskQueue,diskBytesSec,diskSecXfer,cpuPct,dpcPct,intPct,netBytesSec,gpu3dPct,gpuDedicatedMB,gpuSharedMB,top' |
    Set-Content -LiteralPath $Out
while ($true) {
    $row = Get-Counter -Counter $counters -SampleInterval 1 -MaxSamples 1
    $v = @{}
    foreach ($s in $row.CounterSamples) { $v[$s.Path -replace '^\\\\[^\\]+', ''] = $s.CookedValue }
    $net = ($row.CounterSamples | Where-Object { $_.Path -like '*network interface*' } | Measure-Object CookedValue -Sum).Sum
    $gpu = (Get-Counter '\GPU Engine(*engtype_3D)\Utilization Percentage' -EA SilentlyContinue).CounterSamples |
        Measure-Object CookedValue -Sum
    $mem = (Get-Counter '\GPU Adapter Memory(*)\Dedicated Usage', '\GPU Adapter Memory(*)\Shared Usage' -EA SilentlyContinue).CounterSamples
    $dedicated = ($mem | Where-Object { $_.Path -like '*dedicated*' } | Measure-Object CookedValue -Maximum).Maximum / 1MB
    $shared = ($mem | Where-Object { $_.Path -like '*shared*' } | Measure-Object CookedValue -Maximum).Maximum / 1MB
    $now = @{}
    $top = foreach ($p in Get-Process) {
        $now[$p.Id] = $p.TotalProcessorTime.TotalSeconds
        if ($previous.ContainsKey($p.Id)) {
            [pscustomobject]@{ Name = $p.ProcessName; Cpu = ($now[$p.Id] - $previous[$p.Id]) * 100 / $cores }
        }
    }
    $previous = $now
    $names = ($top | Sort-Object Cpu -Descending | Select-Object -First 4 | ForEach-Object { '{0}:{1:F1}' -f $_.Name, $_.Cpu }) -join ' '
    $epoch = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
    '{0},{1},{2:F0},{3:F0},{4:F1},{5:F2},{6:F0},{7:F4},{8:F1},{9:F1},{10:F1},{11:F0},{12:F1},{13:F0},{14:F0},{15}' -f
        $epoch, $v['\memory\available mbytes'], $v['\memory\pages/sec'], $v['\memory\page faults/sec'],
        $v['\memory\% committed bytes in use'], $v['\physicaldisk(_total)\avg. disk queue length'],
        $v['\physicaldisk(_total)\disk bytes/sec'], $v['\physicaldisk(_total)\avg. disk sec/transfer'],
        $v['\processor(_total)\% processor time'], $v['\processor(_total)\% dpc time'],
        $v['\processor(_total)\% interrupt time'], $net, $gpu.Sum, $dedicated, $shared, $names |
        Add-Content -LiteralPath $Out
}
