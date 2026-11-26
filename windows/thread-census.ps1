# SPDX-License-Identifier: GPL-3.0-or-later
# Groups a process's threads by where they start, what they wait on and what their stacks show, so a thread
# count that only grows can be traced to the code that creates them. Read-only apart from a short suspend of the
# few threads whose stacks are sampled.
#
#   thread-census.ps1 [-ProcessName SkyrimSE] [-NewestStacks 6] [-AllStacks]
#
# Prints one compact JSON object: total, groups by start address (module+offset, thread state, wait reason,
# thread description) with counts, and the newest threads with a heuristic call-site list. Stack frames are
# return addresses found by scanning the top of the stack for values that follow a call instruction, so they can
# include stale entries; they are evidence of where a thread sits, not a verified backtrace.
param(
    [string] $ProcessName = 'SkyrimSE',
    [int] $NewestStacks = 6,
    [switch] $AllStacks
)
$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;

public static class ThreadCensus
{
    [DllImport("ntdll.dll")] static extern int NtQuerySystemInformation(int cls, IntPtr buffer, int length, out int needed);
    [DllImport("ntdll.dll")] static extern int NtQueryInformationThread(IntPtr thread, int cls, out IntPtr value, int size, IntPtr returned);
    [DllImport("kernel32.dll")] static extern IntPtr OpenThread(uint access, bool inherit, uint tid);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
    [DllImport("kernel32.dll")] static extern uint SuspendThread(IntPtr thread);
    [DllImport("kernel32.dll")] static extern uint ResumeThread(IntPtr thread);
    [DllImport("kernel32.dll")] static extern bool GetThreadContext(IntPtr thread, IntPtr context);
    [DllImport("kernel32.dll")] static extern IntPtr OpenProcess(uint access, bool inherit, uint pid);
    [DllImport("kernel32.dll")] static extern bool ReadProcessMemory(IntPtr process, IntPtr address, byte[] buffer, int size, out IntPtr read);
    [DllImport("kernel32.dll")] static extern int GetThreadDescription(IntPtr thread, out IntPtr description);
    [DllImport("kernel32.dll")] static extern IntPtr LocalFree(IntPtr memory);

    public class Info
    {
        public uint Tid;
        public long Created;
        public int State;
        public int WaitReason;
        public ulong Start;
        public string Description = "";
        public List<ulong> Frames = new List<ulong>();
    }

    public static List<Info> Threads(int pid)
    {
        int size = 1 << 20, needed;
        IntPtr buffer = Marshal.AllocHGlobal(size);
        try {
            while (NtQuerySystemInformation(5, buffer, size, out needed) == unchecked((int)0xC0000004)) {
                Marshal.FreeHGlobal(buffer);
                size = needed + (1 << 16);
                buffer = Marshal.AllocHGlobal(size);
            }
            var result = new List<Info>();
            long offset = 0;
            while (true) {
                IntPtr entry = IntPtr.Add(buffer, (int)offset);
                int next = Marshal.ReadInt32(entry, 0);
                int count = Marshal.ReadInt32(entry, 4);
                long entryPid = Marshal.ReadInt64(entry, 80);
                if (entryPid == pid) {
                    for (int i = 0; i < count; i++) {
                        IntPtr t = IntPtr.Add(entry, 256 + i * 80);
                        result.Add(new Info {
                            Created = Marshal.ReadInt64(t, 16),
                            Tid = (uint)Marshal.ReadInt64(t, 48),
                            State = Marshal.ReadInt32(t, 68),
                            WaitReason = Marshal.ReadInt32(t, 72),
                        });
                    }
                    break;
                }
                if (next == 0) break;
                offset += next;
            }
            return result;
        } finally {
            Marshal.FreeHGlobal(buffer);
        }
    }

    public static void Describe(Info info)
    {
        IntPtr handle = OpenThread(0x0040 | 0x0400 | 0x2000, false, info.Tid);  // query, query limited
        if (handle == IntPtr.Zero) return;
        try {
            IntPtr start;
            if (NtQueryInformationThread(handle, 9, out start, IntPtr.Size, IntPtr.Zero) == 0) info.Start = (ulong)start.ToInt64();
            IntPtr text;
            if (GetThreadDescription(handle, out text) >= 0 && text != IntPtr.Zero) {
                info.Description = Marshal.PtrToStringUni(text) ?? "";
                LocalFree(text);
            }
        } finally {
            CloseHandle(handle);
        }
    }

    // Return addresses near the top of a stopped thread's stack: the value must follow a call instruction.
    public static void Stack(int pid, Info info, int words)
    {
        IntPtr thread = OpenThread(0x0002 | 0x0008 | 0x0040, false, info.Tid);  // suspend, get context, query
        IntPtr process = OpenProcess(0x0010, false, (uint)pid);  // read memory
        if (thread == IntPtr.Zero || process == IntPtr.Zero) {
            if (thread != IntPtr.Zero) CloseHandle(thread);
            if (process != IntPtr.Zero) CloseHandle(process);
            return;
        }
        IntPtr raw = Marshal.AllocHGlobal(1232 + 16);
        try {
            IntPtr context = new IntPtr((raw.ToInt64() + 15) & ~15L);
            for (int i = 0; i < 1232; i++) Marshal.WriteByte(context, i, 0);
            Marshal.WriteInt32(context, 0x30, 0x100001);  // CONTEXT_CONTROL
            byte[] stack = new byte[words * 8];
            IntPtr read = IntPtr.Zero;
            bool ok = false;
            if (SuspendThread(thread) != 0xFFFFFFFF) {
                try {
                    if (GetThreadContext(thread, context)) {
                        long rsp = Marshal.ReadInt64(context, 0x98);
                        ok = ReadProcessMemory(process, new IntPtr(rsp), stack, stack.Length, out read) || read.ToInt64() > 0;
                    }
                } finally {
                    ResumeThread(thread);
                }
            }
            if (!ok) return;
            int got = (int)Math.Min(read.ToInt64(), stack.Length);
            byte[] code = new byte[8];
            for (int i = 0; i + 8 <= got; i += 8) {
                ulong value = BitConverter.ToUInt64(stack, i);
                if (value < 0x10000 || value > 0x7FFFFFFFFFFF) continue;
                IntPtr n;
                if (!ReadProcessMemory(process, new IntPtr((long)value - 8), code, 8, out n) || n.ToInt64() < 8) continue;
                // E8 rel32 sits 5 bytes before the return address; FF /2 (indirect call) 2 to 7 bytes before it.
                bool afterCall = code[3] == 0xE8;
                for (int k = 0; k < 7 && !afterCall; k++) afterCall = code[k] == 0xFF && (code[k + 1] & 0x38) == 0x10;
                if (afterCall) info.Frames.Add(value);
            }
        } finally {
            Marshal.FreeHGlobal(raw);
            CloseHandle(thread);
            CloseHandle(process);
        }
    }
}
'@

$process = Get-Process $ProcessName -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $process) { '{"alive":false}'; return }

$modules = @($process.Modules | ForEach-Object {
    [pscustomobject]@{ Name = $_.ModuleName; Base = [uint64]$_.BaseAddress.ToInt64(); End = [uint64]($_.BaseAddress.ToInt64() + $_.ModuleMemorySize) }
})
function Where-Is([uint64] $Address) {
    foreach ($m in $modules) {
        if ($Address -ge $m.Base -and $Address -lt $m.End) { return '{0}+0x{1:x}' -f $m.Name, ($Address - $m.Base) }
    }
    return 'unmapped:0x{0:x}' -f $Address
}
$systemModules = 'ntdll.dll', 'kernel32.dll', 'KERNELBASE.dll', 'ucrtbase.dll', 'MSVCP140.dll', 'VCRUNTIME140.dll', 'MSVCR110.dll', 'MSVCP110.dll'
function Signature($Info) {
    # Outermost non-system call sites first: they name the code that owns the thread, not the wait inside it.
    $own = @($Info.Frames | ForEach-Object { Where-Is $_ } | Where-Object { $name = ($_ -split '\+')[0]; $systemModules -notcontains $name })
    [array]::Reverse($own)
    ($own | Select-Object -First 3) -join ' < '
}

$states = 'Initialized', 'Ready', 'Running', 'Standby', 'Terminated', 'Waiting', 'Transition', 'DeferredReady', 'GateWaitObsolete', 'WaitingForProcessInSwap'
$threads = [ThreadCensus]::Threads($process.Id)
foreach ($t in $threads) { [ThreadCensus]::Describe($t) }
$newest = @($threads | Sort-Object Created -Descending | Select-Object -First $NewestStacks)
$sample = if ($AllStacks) { $threads } else { $newest }
foreach ($t in $sample) { [ThreadCensus]::Stack($process.Id, $t, 512) }

$now = [DateTime]::UtcNow.ToFileTimeUtc()
$groups = $threads | Group-Object { '{0}|{1}|{2}' -f (Where-Is $_.Start), $_.State, $_.WaitReason } | ForEach-Object {
    $first = $_.Group[0]
    $row = [ordered]@{
        start = Where-Is $first.Start
        state = $states[[Math]::Min($first.State, 9)]
        waitReason = $first.WaitReason
        count = $_.Count
        oldestAgeS = [int](($now - ($_.Group | Measure-Object Created -Minimum).Minimum) / 1e7)
        newestAgeS = [int](($now - ($_.Group | Measure-Object Created -Maximum).Maximum) / 1e7)
        names = @($_.Group | ForEach-Object { $_.Description } | Where-Object { $_ } | Select-Object -Unique -First 3)
    }
    if ($AllStacks) {
        $row.signatures = @($_.Group | ForEach-Object { Signature $_ } | Group-Object | Sort-Object Count -Descending | Select-Object -First 5 |
            ForEach-Object { [ordered]@{ count = $_.Count; frames = $_.Name } })
    }
    $row
} | Sort-Object { -$_.count }

$result = [ordered]@{
    alive = $true
    total = $threads.Count
    handles = $process.HandleCount
    groups = @($groups)
    newest = @($newest | ForEach-Object {
        [ordered]@{ tid = $_.Tid; ageS = [int](($now - $_.Created) / 1e7); start = Where-Is $_.Start
            state = $states[[Math]::Min($_.State, 9)]; waitReason = $_.WaitReason; name = $_.Description
            frames = @($_.Frames | Select-Object -First 14 | ForEach-Object { Where-Is $_ }) }
    })
}
$result | ConvertTo-Json -Depth 6 -Compress
