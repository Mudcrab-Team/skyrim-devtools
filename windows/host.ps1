param(
    [Parameter(Mandatory)]
    [ValidateSet('install-task', 'start', 'run', 'status', 'stop', 'evidence', 'crashes', 'active', 'idle',
                 'install-capture-task', 'capture-start', 'capture-run', 'capture-stop', 'capture-status',
                 'health', 'reset', 'restore-profile')]
    [string] $Action,
    [ValidatePattern('^[a-f0-9]{32}$')]
    [string] $Session,
    [ValidateSet('', 'headless', 'windowed')]
    [string] $Mode = '',
    [ValidateRange(5, 60)]
    [int] $Fps = 30,
    [ValidateRange(320, 3840)]
    [int] $Width = 1920,
    [ValidateRange(1, 120)]
    [int] $Grace = 12,
    [switch] $Force
)
$ErrorActionPreference = 'Stop'
$taskName = 'SkyrimDevTools'
$captureTask = 'SkyrimDevToolsCapture'
$root = $PSScriptRoot
$games = Join-Path $root 'game'
$profile = Join-Path $root 'profile'
$runs = Join-Path $root 'runs'
$lease = Join-Path $root 'active'
$captureLease = Join-Path $root 'capture-active'
$pin = '846efccf0c1374d71f892907f46549560f2fcb0a75cb87a3eed438baa0f1402f'
$account = [Security.Principal.WindowsIdentity]::GetCurrent().Name

function Read-Json($Path) {
    Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
}

function Write-Json($Value, $Path) {
    $temporary = "$Path.new"
    [IO.File]::WriteAllText($temporary, ($Value | ConvertTo-Json -Compress))
    if ([IO.File]::Exists($Path)) {
        [IO.File]::Replace($temporary, $Path, [NullString]::Value)
    } else {
        [IO.File]::Move($temporary, $Path)
    }
}

function Result($Phase, $Reason = '', $ExitCode = $null) {
    @{ sessionId = $Session; phase = $Phase; reason = $Reason; exitCode = $ExitCode }
}

function Assert-PrivateDirectory($Path) {
    $entry = Get-Item -LiteralPath $Path
    if (!$entry.PSIsContainer -or ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'Managed directory must be a real directory in the scratch tree'
    }
}

function Refuse-Launch($Check, $Message) {
    $failure = [InvalidOperationException]::new($Message)
    $failure.Data['LaunchCheck'] = $Check
    throw $failure
}

function Assert-InteractiveDesktop($Headless) {
    # A scheduled task can still run in a disconnected desktop session.
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Text;

public static class SkyrimDevToolsDesktop
{
    [DllImport("wtsapi32.dll", EntryPoint = "WTSQuerySessionInformationW")]
    private static extern bool QuerySession(IntPtr server, int session, int infoClass, out IntPtr value, out int bytes);
    [DllImport("wtsapi32.dll", EntryPoint = "WTSFreeMemory")]
    private static extern void FreeSessionMemory(IntPtr value);
    [DllImport("user32.dll")]
    private static extern IntPtr OpenInputDesktop(uint flags, bool inherit, uint access);
    [DllImport("user32.dll", EntryPoint = "GetUserObjectInformationW", CharSet = CharSet.Unicode)]
    private static extern bool DesktopInfo(IntPtr desktop, int index, StringBuilder value, int bytes, out int needed);
    [DllImport("user32.dll")]
    private static extern bool CloseDesktop(IntPtr desktop);

    public static bool IsActive(int session)
    {
        const int ConnectState = 8;
        const int Active = 0;
        IntPtr value;
        int bytes;
        if (session <= 0 || !QuerySession(IntPtr.Zero, session, ConnectState, out value, out bytes))
            return false;
        try { return bytes >= sizeof(int) && Marshal.ReadInt32(value) == Active; }
        finally { FreeSessionMemory(value); }
    }

    public static bool IsUnlocked()
    {
        const uint ReadObjects = 1;
        const int Name = 2;
        IntPtr desktop = OpenInputDesktop(0, false, ReadObjects);
        if (desktop == IntPtr.Zero)
            return false;
        try {
            var name = new StringBuilder(64);
            int bytes;
            return DesktopInfo(desktop, Name, name, name.Capacity * sizeof(char), out bytes) &&
                   String.Equals(name.ToString(), "Default", StringComparison.OrdinalIgnoreCase);
        } finally { CloseDesktop(desktop); }
    }
}
'@
    $sessionId = (Get-Process -Id $PID).SessionId
    if (![SkyrimDevToolsDesktop]::IsActive($sessionId)) {
        Refuse-Launch 'desktop-inactive' 'The test account must have an active interactive desktop session'
    }
    # Headless runs inject input inside the game, so only the window needs a live session.
    if (!$Headless -and ![SkyrimDevToolsDesktop]::IsUnlocked()) {
        Refuse-Launch 'desktop-locked' 'The interactive test desktop is locked or inaccessible'
    }
}

# The game resolves My Games\Skyrim Special Edition from the Windows Documents folder and has no switch to move it
# (sLocalSavePath only moves saves; INIs and the SKSE and Papyrus logs stay). The isolated profile is therefore
# reached through a junction that exists only while a run owns the game, so a modlist installed next to the test
# setup never writes into it. The real folder is renamed, never emptied, and a real folder that is not a junction
# is never touched beyond that one rename.
function Get-DocsPaths {
    $link = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'My Games\Skyrim Special Edition'
    @{ link = $link; backup = "$link.DevTools-Real" }
}

function Get-DocsLinkState($Path) {
    $entry = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    if (!$entry) { return 'missing' }
    if ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        $target = @($entry.Target)
        if ($entry.LinkType -eq 'Junction' -and $target.Count -eq 1 -and [IO.Path]::GetFullPath($target[0]) -eq $profile) {
            return 'junction-profile'
        }
        return 'junction-other'
    }
    if ($entry.PSIsContainer) { return 'real' }
    return 'file'
}

# Decides what entering the isolated profile has to do; anything ambiguous is an error, never a guess.
function Get-EnterPlan([string] $State, [bool] $BackupExists) {
    switch ($State) {
        'missing' { return 'link' }
        'junction-profile' { return 'reuse' }
        'real' {
            if ($BackupExists) { throw 'Both the Documents Skyrim folder and its DevTools-Real backup are real folders; resolve by hand' }
            return 'backup-and-link'
        }
        default { throw "The Documents Skyrim folder is in an unexpected state ($State); resolve by hand" }
    }
}

function Test-RealBackup($Path) {
    $entry = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    [bool]($entry -and $entry.PSIsContainer -and !($entry.Attributes -band [IO.FileAttributes]::ReparsePoint))
}

# Plugins.txt and the content catalog under LOCALAPPDATA are shared with other installs and cannot be redirected,
# so the files present before a run are kept aside and put back afterwards.
function Get-LocalDataPaths {
    @{ live = Join-Path $env:LOCALAPPDATA 'Skyrim Special Edition'; kept = Join-Path $root 'localappdata-kept' }
}

function Restore-LocalData {
    $paths = Get-LocalDataPaths
    if (!(Test-Path -LiteralPath $paths.kept)) { return }
    foreach ($file in Get-ChildItem -LiteralPath $paths.kept -File -Force) {
        $target = Join-Path $paths.live $file.Name
        $same = (Test-Path -LiteralPath $target) -and
            (Get-FileHash -LiteralPath $target).Hash -eq (Get-FileHash -LiteralPath $file.FullName).Hash
        if (!$same) { Copy-Item -LiteralPath $file.FullName -Destination $target -Force }
        Remove-Item -LiteralPath $file.FullName -Force
    }
    Remove-Item -LiteralPath $paths.kept -Force
}

function Keep-LocalData {
    $paths = Get-LocalDataPaths
    # Leftovers from an interrupted run hold the real content, so they are restored before anything is kept.
    Restore-LocalData
    if (!(Test-Path -LiteralPath $paths.live)) { return }
    New-Item -ItemType Directory -Path $paths.kept | Out-Null
    foreach ($file in Get-ChildItem -LiteralPath $paths.live -File -Force) {
        Copy-Item -LiteralPath $file.FullName -Destination (Join-Path $paths.kept $file.Name)
    }
}

function Enter-IsolatedProfile {
    $docs = Get-DocsPaths
    $plan = Get-EnterPlan (Get-DocsLinkState $docs.link) (Test-Path -LiteralPath $docs.backup)
    if ($plan -eq 'reuse') { Keep-LocalData; return }
    if ($plan -eq 'backup-and-link') { [IO.Directory]::Move($docs.link, $docs.backup) }
    try {
        New-Item -ItemType Junction -Path $docs.link -Target $profile | Out-Null
    } catch {
        if ($plan -eq 'backup-and-link' -and !(Test-Path -LiteralPath $docs.link)) { [IO.Directory]::Move($docs.backup, $docs.link) }
        throw
    }
    Keep-LocalData
}

# Removes the junction (only the link, through rmdir) and renames the backup back. Idempotent, and a no-op while a
# game process exists because the junction is then in use.
function Exit-IsolatedProfile {
    if (Get-Process SkyrimSE, skse64_loader -ErrorAction SilentlyContinue) { return 'game-running' }
    $docs = Get-DocsPaths
    $state = Get-DocsLinkState $docs.link
    $backup = Test-Path -LiteralPath $docs.backup
    if ($state -eq 'real' -and $backup) { throw 'Both the Documents Skyrim folder and its DevTools-Real backup are real folders; resolve by hand' }
    if ($state -eq 'junction-other' -or $state -eq 'file') { throw "The Documents Skyrim folder is in an unexpected state ($state); resolve by hand" }
    if ($state -eq 'junction-profile') {
        & cmd.exe /c rmdir "`"$($docs.link)`"" | Out-Null
        if (Test-Path -LiteralPath $docs.link) { throw 'The isolated profile junction could not be removed' }
        $state = 'missing'
    }
    $restored = $false
    if ($state -eq 'missing' -and $backup) {
        if (!(Test-RealBackup $docs.backup)) { throw 'The DevTools-Real backup is not a plain folder' }
        [IO.Directory]::Move($docs.backup, $docs.link)
        $restored = $true
    }
    Restore-LocalData
    if ($restored) { 'restored' } else { 'clean' }
}

function Assert-LocalProfile {
    $docs = [Environment]::GetFolderPath('MyDocuments')
    $link = Get-Item -LiteralPath (Join-Path $docs 'My Games\Skyrim Special Edition')
    $destination = @($link.Target)
    if ($link.LinkType -ne 'Junction' -or $destination.Count -ne 1 -or
        [IO.Path]::GetFullPath($destination[0]) -ne $profile) {
        throw 'The interactive test account needs an isolated Skyrim profile junction'
    }
    Assert-PrivateDirectory $profile
    Assert-PrivateDirectory (Join-Path $profile 'Saves')
    foreach ($name in @('Skyrim.ini', 'SkyrimPrefs.ini')) {
        $ini = Get-Item -LiteralPath (Join-Path $profile $name)
        if ($ini.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw 'Test settings must be private copies'
        }
    }
    $settings = Get-Content -LiteralPath (Join-Path $profile 'SkyrimPrefs.ini') -Raw
    foreach ($key in @('bSaveOnPause', 'bSaveOnRest', 'bSaveOnTravel', 'bSaveOnWait')) {
        $lines = [regex]::Matches($settings, "(?im)^\s*$key\s*=\s*([^\r\n]+)")
        if ($lines.Count -ne 1 -or $lines[0].Groups[1].Value.Trim() -ne '0') {
            throw 'Test profile must disable autosaves'
        }
    }
    $localSavePath = [regex]::Matches(
        (Get-Content -LiteralPath (Join-Path $profile 'Skyrim.ini') -Raw),
        '(?im)^\s*sLocalSavePath\s*=\s*([^\r\n]+)')
    if ($localSavePath.Count -ne 1 -or $localSavePath[0].Groups[1].Value.Trim() -ne 'Saves\') {
        throw 'Test profile must use its private Saves directory'
    }
}

function Assert-OfflineSteam {
    $steam = @(Get-Process steam -ErrorAction SilentlyContinue |
        Where-Object SessionId -EQ (Get-Process -Id $PID).SessionId)
    if ($steam.Count -ne 1 -or !$steam[0].Path) {
        Refuse-Launch 'steam-unavailable' 'Steam must already be running on the interactive test desktop'
    }
    try {
        $login = Get-Content -LiteralPath (Join-Path (Split-Path $steam[0].Path) 'config\loginusers.vdf') -Raw
    } catch {
        Refuse-Launch 'steam-offline-unconfirmed' 'Steam Offline Mode could not be confirmed'
    }
    $accountId = (Get-ItemProperty -LiteralPath 'HKCU:\Software\Valve\Steam\ActiveProcess' -ErrorAction SilentlyContinue).ActiveUser
    $active = @([regex]::Matches($login, '"([0-9]{1,19})"\s*\{([^{}]*)\}') |
        Where-Object { $accountId -and ([uint64]$_.Groups[1].Value % 4294967296) -eq $accountId })
    if ($active.Count -ne 1 -or $active[0].Groups[2].Value -notmatch '"WantsOfflineMode"\s+"[01]"') {
        Refuse-Launch 'steam-offline-unconfirmed' 'The active Steam account could not be confirmed'
    }
    if ($active[0].Groups[2].Value -notmatch '"WantsOfflineMode"\s+"1"') {
        Refuse-Launch 'steam-online' 'The active Steam account must be in Offline Mode'
    }
}

function Set-IniValue([string] $Text, [string] $Section, [string] $Key, [string] $Value) {
    $line = "$Key=$Value"
    $header = [regex]::Match($Text, "(?im)^\s*\[$([regex]::Escape($Section))\]\s*$")
    if (!$header.Success) {
        return $Text.TrimEnd() + "`r`n[$Section]`r`n$line`r`n"
    }
    $bodyStart = $header.Index + $header.Length
    $next = [regex]::Match($Text.Substring($bodyStart), '(?m)^\s*\[')
    $bodyEnd = if ($next.Success) { $bodyStart + $next.Index } else { $Text.Length }
    $body = $Text.Substring($bodyStart, $bodyEnd - $bodyStart)
    $pattern = "(?im)^[ \t]*$([regex]::Escape($Key))[ \t]*=[^\r\n]*"
    if ([regex]::IsMatch($body, $pattern)) {
        $body = [regex]::Replace($body, $pattern, $line.Replace('$', '$$'))
    } else {
        $body = "`r`n$line" + $body
    }
    $Text.Substring(0, $bodyStart) + $body + $Text.Substring($bodyEnd)
}

function Write-ProfileSettings($Headless, $Config) {
    # Every run starts from the pristine copies, so overrides never accumulate in the profile.
    $originals = Join-Path $profile 'ini-original'
    if (!(Test-Path -LiteralPath $originals)) {
        New-Item -ItemType Directory -Path $originals | Out-Null
        foreach ($name in @('Skyrim.ini', 'SkyrimPrefs.ini')) {
            Copy-Item -LiteralPath (Join-Path $profile $name) -Destination (Join-Path $originals $name)
        }
    }
    Assert-PrivateDirectory $originals
    $main = [IO.File]::ReadAllText((Join-Path $originals 'Skyrim.ini'))
    $prefs = [IO.File]::ReadAllText((Join-Path $originals 'SkyrimPrefs.ini'))
    # Script errors are the first sign of a broken mod, so every run records them with line numbers.
    $main = Set-IniValue $main 'Papyrus' 'bEnableLogging' '1'
    $main = Set-IniValue $main 'Papyrus' 'bEnableTrace' '1'
    $main = Set-IniValue $main 'Papyrus' 'bLoadDebugInformation' '1'
    if ($Headless) {
        $width = if ($Config.width -is [int]) { $Config.width } else { 1280 }
        $height = if ($Config.height -is [int]) { $Config.height } else { 720 }
        # The setting is read from either file depending on the runtime; both copies are harmless.
        $main = Set-IniValue $main 'General' 'bAlwaysActive' '1'
        $prefs = Set-IniValue $prefs 'General' 'bAlwaysActive' '1'
        $prefs = Set-IniValue $prefs 'Display' 'bFull Screen' '0'
        $prefs = Set-IniValue $prefs 'Display' 'bBorderless' '1'
        $prefs = Set-IniValue $prefs 'Display' 'iSize W' "$width"
        $prefs = Set-IniValue $prefs 'Display' 'iSize H' "$height"
        if ($Config.muteAudio -ne $false) {
            $prefs = Set-IniValue $prefs 'AudioMenu' 'fAudioMasterVolume' '0.0000'
        }
    } else {
        if ($Config.disableAudio -eq $true) {
            # With no audio output device present the engine's XAudio2 layer keeps spawning threads (about
            # 2 per second) until the process hangs or crashes inside XAudio2 after 15-45 minutes.
            $main = Set-IniValue $main 'Audio' 'bEnableAudio' '0'
        }
        # Exclusive fullscreen drew the game into a corner of the monitor with black margins (the HUD placed for
        # a larger frame) and made desktop capture grab black; a borderless window at the desktop size has neither
        # problem and takes the resolution from the profile's own iSize W/H.
        $prefs = Set-IniValue $prefs 'Display' 'bFull Screen' '0'
        $prefs = Set-IniValue $prefs 'Display' 'bBorderless' '1'
    }
    [IO.File]::WriteAllText((Join-Path $profile 'Skyrim.ini'), $main)
    [IO.File]::WriteAllText((Join-Path $profile 'SkyrimPrefs.ini'), $prefs)
}

function Reset-GameLogs {
    # A run's evidence must hold only that run's lines, so logs from earlier runs are removed.
    $scripts = Join-Path $profile 'Logs\Script'
    if (Test-Path -LiteralPath $scripts) {
        Assert-PrivateDirectory $scripts
        Get-ChildItem -LiteralPath $scripts -File -Filter 'Papyrus*.log' | Remove-Item -Force
    }
}

function Get-CrashEvents($Since) {
    try {
        $found = @(Get-WinEvent -FilterHashtable @{ LogName = 'Application'; StartTime = $Since } -ErrorAction Stop |
            Where-Object { $_.ProviderName -in @('Application Error', 'Windows Error Reporting', 'Application Hang') -and
                           $_.Message -match 'SkyrimSE' })
    } catch {
        return @()
    }
    foreach ($record in ($found | Select-Object -First 10)) {
        $entry = @{ time = $record.TimeCreated.ToUniversalTime().ToString('o'); id = $record.Id; provider = $record.ProviderName }
        if ($record.Id -eq 1000 -and $record.Properties.Count -ge 8) {
            $entry.faultingModule = [string] $record.Properties[3].Value
            $entry.exceptionCode = [string] $record.Properties[6].Value
            $entry.faultOffset = [string] $record.Properties[7].Value
        }
        $entry.message = (($record.Message -split "`r?`n") | Select-Object -First 12) -join "`n"
        $entry
    }
}

function Get-CrashLogs($Since, $Count) {
    # CrashLogger writes crash-<time>.log into the profile's SKSE folder (the isolated profile, not Documents).
    $dir = Join-Path $profile 'SKSE'
    if (!(Test-Path -LiteralPath $dir)) { return @() }
    $files = Get-ChildItem -LiteralPath $dir -File -Filter 'crash-*.log' | Sort-Object LastWriteTime -Descending
    if ($Since) { $files = $files | Where-Object { $_.LastWriteTime -ge $Since } }
    foreach ($file in ($files | Select-Object -First $Count)) { Read-CrashLog $file }
}

function Read-CrashLog($File) {
    # Names of modules and functions only: the top frames of the probable call stack, never memory contents.
    $lines = @(Get-Content -LiteralPath $File.FullName -TotalCount 400 -ErrorAction SilentlyContinue)
    $header = @($lines | Where-Object { $_ -match '^Unhandled exception' } | Select-Object -First 1)
    $start = -1
    for ($i = 0; $i -lt $lines.Count; $i++) { if ($lines[$i] -match '^CALL STACK') { $start = $i; break } }
    $frames = @()
    if ($start -ge 0) {
        # keep module, offset and the Address Library id of each probable frame; drop the instruction text
        $frames = @($lines[($start + 1)..([Math]::Min($lines.Count - 1, $start + 40))] |
            Where-Object { $_ -match '^\s*\[\s*\d+\]\[P\]' } | Select-Object -First 12 |
            ForEach-Object { (($_.Trim() -replace '^\[\s*(\d+)\]\[P\]\s+0x[0-9A-Fa-f]+\s+', '$1 ') -split "`t")[0] })
    }
    @{ name = $File.Name; time = $File.LastWriteTimeUtc.ToString('o'); bytes = $File.Length
       exception = if ($header) { [string] $header[0] } else { $null }; topFrames = $frames }
}

function Save-Tail($Source, $Destination, $Lines) {
    if (Test-Path -LiteralPath $Source) {
        Get-Content -LiteralPath $Source -Tail $Lines -ErrorAction SilentlyContinue |
            Set-Content -LiteralPath $Destination -Encoding UTF8
    }
}

function Save-Evidence($Run, $Phase, $Reason, $ExitCode, $Started, $Hung) {
    # Everything that explains an abnormal end, gathered before the next launch can overwrite it.
    $evidence = Join-Path $Run 'evidence'
    New-Item -ItemType Directory -Path $evidence -Force | Out-Null
    $scripts = Join-Path $profile 'Logs\Script'
    $papyrus = if (Test-Path -LiteralPath $scripts) {
        Get-ChildItem -LiteralPath $scripts -File -Filter 'Papyrus*.log' | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    }
    if ($papyrus) { Save-Tail $papyrus.FullName (Join-Path $evidence 'papyrus-tail.log') 300 }
    Save-Tail (Join-Path $profile 'SKSE\skse64.log') (Join-Path $evidence 'skse64-tail.log') 200
    Save-Tail (Join-Path $Run 'rpc.jsonl') (Join-Path $evidence 'rpc-tail.jsonl') 100
    Save-Tail (Join-Path $Run 'devtools.log') (Join-Path $evidence 'plugin-tail.log') 200
    $summary = @{
        phase = $Phase; reason = $Reason; exitCode = $ExitCode; hung = $Hung
        exitCodeHex = if ($null -ne $ExitCode) { '0x{0:X8}' -f [uint32] ([BitConverter]::ToUInt32([BitConverter]::GetBytes([int] $ExitCode), 0)) } else { $null }
        crashEvents = @(if ($Started) { Get-CrashEvents $Started })
        crashLogs = @(if ($Started) { Get-CrashLogs $Started.AddSeconds(-5) 3 })
        files = @(Get-ChildItem -LiteralPath $evidence -File | ForEach-Object Name)
    }
    Write-Json $summary (Join-Path $evidence 'evidence.json')
}

function Stage-Saves {
    # Saves the game wrote during an earlier run are disposable; only fixtures are restored.
    $saves = Join-Path $profile 'Saves'
    foreach ($entry in Get-ChildItem -LiteralPath $saves -Force) {
        if ($entry.PSIsContainer -or ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw 'The scratch save directory contains an unexpected entry'
        }
        Remove-Item -LiteralPath $entry.FullName -Force
    }
    $destination = Join-Path $saves 'Fixture.ess'
    Copy-Item -LiteralPath (Join-Path $root 'fixture.ess') -Destination $destination
    $fixtures = Join-Path $root 'fixtures'
    if (Test-Path -LiteralPath $fixtures) {
        Assert-PrivateDirectory $fixtures
        foreach ($entry in Get-ChildItem -LiteralPath $fixtures -File) {
            if ($entry.Extension -in @('.ess', '.skse') -and $entry.Name -match '^[A-Za-z0-9_ .-]+$' -and
                !($entry.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
                Copy-Item -LiteralPath $entry.FullName -Destination (Join-Path $saves $entry.Name) -Force
            }
        }
    }
    (Get-FileHash -LiteralPath $destination).Hash.ToLowerInvariant()
}

function Read-Grace($StopFile) {
    # The stop request carries how many seconds the game gets to quit by itself before it is killed.
    $text = if (Test-Path -LiteralPath $StopFile) { ([IO.File]::ReadAllText($StopFile)).Trim() } else { '' }
    $value = 0
    if ([int]::TryParse($text, [ref] $value) -and $value -ge 1 -and $value -le 120) { $value } else { 12 }
}

function Read-Seconds($Value, $Default) {
    if ($null -eq $Value) { return $Default }
    if ($Value -isnot [int] -or $Value -lt 0) { throw 'Timeouts must be non-negative whole seconds' }
    $Value
}

function Find-OwnedGame($LoaderPid) {
    Get-CimInstance Win32_Process -Filter "Name = 'SkyrimSE.exe' AND ParentProcessId = $LoaderPid" |
        Where-Object ExecutablePath -EQ (Join-Path $games 'SkyrimSE.exe') |
        Select-Object -First 1
}

function Preflight($Config, $Headless) {
    Assert-InteractiveDesktop $Headless
    Assert-OfflineSteam
    if ($Config.cloudSyncDisabled -ne $true) {
        Refuse-Launch 'steam-cloud-unconfirmed' 'Disable Steam Cloud for Skyrim and confirm it in the local configuration'
    }
    if ($Config.port -isnot [int] -or $Config.port -lt 1024 -or $Config.port -gt 65535) {
        throw 'Set a valid loopback server port'
    }
    if (Get-Process SkyrimSE, skse64_loader -ErrorAction SilentlyContinue) {
        throw 'A game or loader process is already running'
    }
    Enter-IsolatedProfile
    Assert-LocalProfile
    Write-ProfileSettings $Headless $Config
    Assert-LocalProfile
    Reset-GameLogs
    Assert-PrivateDirectory $games
    $data = Join-Path $games 'Data'
    $skse = Join-Path $data 'SKSE'
    $plugins = Join-Path $skse 'Plugins'
    foreach ($directory in @($data, $skse, $plugins)) {
        Assert-PrivateDirectory $directory
    }
    $image = Join-Path $games 'SkyrimSE.exe'
    if ((Get-FileHash -LiteralPath $image).Hash -ne $pin) {
        throw 'Staged executable does not match the pinned image'
    }
    $loader = Get-Item -LiteralPath (Join-Path $games 'skse64_loader.exe')
    if ($loader.VersionInfo.FileVersionRaw -ne [version]'0.2.3.1' -or
        !(Test-Path -LiteralPath (Join-Path $games 'skse64_1_7_104.dll') -PathType Leaf)) {
        throw 'Stage official SKSE 2.3.1 for the pinned runtime'
    }
    if (!(Test-Path -LiteralPath (Join-Path $plugins 'versionlib-1-7-104-0.bin') -PathType Leaf)) {
        throw 'The pinned Address Library table is missing'
    }
    # The crash logger is the one diagnostic plugin allowed next to the test plugin.
    $dlls = @(Get-ChildItem -LiteralPath $plugins -File -Filter '*.dll' | ForEach-Object Name)
    if ($dlls -notcontains 'SkyrimDevTools.dll' -or @($dlls | Where-Object { $_ -notin @('SkyrimDevTools.dll', 'CrashLogger.dll', 'msdia140.dll', 'SkyrimDevToolsAdlx.dll') }).Count) {
        throw 'The test plugin and the crash logger (with its symbol reader) must be the only SKSE plugin DLLs'
    }
    Stage-Saves
}

function Run-Game {
    $script:Session = [IO.File]::ReadAllText($lease)
    if ($Session -notmatch '^[a-f0-9]{32}$') { throw 'Invalid session lease' }
    $run = Join-Path $runs $Session
    $state = Join-Path $run 'state.json'
    $stop = Join-Path $run 'stop'
    $loaderProcess = $null
    $game = $null
    $phase = 'failed'
    $reason = 'preflight'
    $exitCode = $null
    $digest = $null
    $started = $null
    $hung = $false
    $stopPath = $null
    try {
        if (Test-Path -LiteralPath $stop) {
            $phase = 'cancelled'
            $reason = ''
            return
        }
        $config = Read-Json (Join-Path $root 'config.json')
        $request = Read-Json (Join-Path $run 'request.json')
        $headless = if ($request.mode) { $request.mode -eq 'headless' } else { $config.headless -eq $true }
        $watchdog = Read-Seconds $config.watchdogMinutes 240
        $digest = Preflight $config $headless
        $env:SKYRIM_DEVTOOLS_TOKEN = $request.token
        Remove-Item -LiteralPath (Join-Path $run 'request.json')
        $env:SKYRIM_DEVTOOLS_SESSION = $Session
        $env:SKYRIM_DEVTOOLS_IMAGE = $pin
        $env:SKYRIM_DEVTOOLS_PORT = $config.port
        $env:SKYRIM_DEVTOOLS_RUN_DIR = $run
        $env:SKYRIM_DEVTOOLS_SAVES_DIR = Join-Path $profile 'Saves'
        $env:SKYRIM_DEVTOOLS_HEADLESS = if ($headless) { '1' } else { '0' }
        $env:SKYRIM_DEVTOOLS_IDLE_TIMEOUT = Read-Seconds $config.idleTimeoutSeconds 1800
        $env:SKYRIM_DEVTOOLS_CONNECT_TIMEOUT = Read-Seconds $config.connectTimeoutSeconds 600
        $reason = 'loader'
        $loaderProcess = Start-Process -FilePath (Join-Path $games 'skse64_loader.exe') -WorkingDirectory $games -PassThru
        $deadline = [DateTime]::UtcNow.AddSeconds(25)
        do {
            $child = Find-OwnedGame $loaderProcess.Id
            if ($child) { $game = Get-Process -Id $child.ProcessId -ErrorAction SilentlyContinue }
            if (!$game) { Start-Sleep -Milliseconds 250 }
        } until ($game -or [DateTime]::UtcNow -ge $deadline -or (Test-Path -LiteralPath $stop))
        if (!$game) { throw 'Loader did not produce a staged game process' }
        # Keep the handle open so the exit code remains available after the process exits.
        $null = $game.Handle
        $started = $game.StartTime

        $running = Result 'running'
        $running.fixtureSha256 = $digest
        $running.headless = $headless
        Write-Json $running $state
        $reason = 'watchdog'
        $deadline = if ($watchdog -gt 0) { [DateTime]::UtcNow.AddMinutes($watchdog) } else { [DateTime]::MaxValue }
        $nextProbe = [DateTime]::UtcNow.AddSeconds(5)
        $unresponsiveSince = $null
        while (!$game.WaitForExit(250)) {
            if ([DateTime]::UtcNow -ge $nextProbe) {
                # A window that stops pumping messages for a minute is a hang, not a slow load.
                $nextProbe = [DateTime]::UtcNow.AddSeconds(5)
                $game.Refresh()
                if ($game.Responding) {
                    $unresponsiveSince = $null
                } elseif (!$unresponsiveSince) {
                    $unresponsiveSince = [DateTime]::UtcNow
                } elseif (!$hung -and [DateTime]::UtcNow -ge $unresponsiveSince.AddSeconds(60)) {
                    $hung = $true
                    $running.hangSuspectedSince = $unresponsiveSince.ToString('o')
                    Write-Json $running $state
                }
            }
            if ((Test-Path -LiteralPath $stop) -or [DateTime]::UtcNow -ge $deadline) {
                $reason = if (Test-Path -LiteralPath $stop) { 'requested' } else { 'watchdog' }
                $grace = Read-Grace $stop
                if ($game.CloseMainWindow() -and $game.WaitForExit($grace * 1000)) { $stopPath = 'graceful'; break }
                $game.Refresh()
                if (!$game.HasExited -and !$game.Responding) { $hung = $true }
                $phase = 'forced'
                $stopPath = "killed-after-${grace}s"
                return
            }
        }
        $exitCode = $game.ExitCode
        if ($null -eq $exitCode) {
            $phase = 'failed'
            $reason = 'exit-code-unavailable'
        } elseif ($exitCode -ne 0) {
            $phase = 'crashed'
            $reason = 'nonzero-exit'
        } elseif (Test-Path -LiteralPath $stop) {
            $phase = 'stopped'
            $reason = ''
        } else {
            $phase = 'exited'
            $reason = 'unrequested-exit'
        }
    } catch {
        if ($_.Exception.Data['LaunchCheck']) { $reason = $_.Exception.Data['LaunchCheck'] }
        $_.Exception.Message | Set-Content -LiteralPath (Join-Path $run 'host-error.txt') -Encoding UTF8
    } finally {
      try {
        if ($loaderProcess -and !$loaderProcess.HasExited) {
            $loaderProcess.Kill()
            $loaderProcess.WaitForExit()
        }
        if (!$game -and $loaderProcess) {
            $child = Find-OwnedGame $loaderProcess.Id
            if ($child) { $game = Get-Process -Id $child.ProcessId -ErrorAction SilentlyContinue }
        }
        if ($game -and !$game.HasExited) {
            $phase = 'forced'
            $game.Kill()
            if (!$game.WaitForExit(5000)) { throw 'Owned game process did not exit after stop' }
        }
        Remove-Item -LiteralPath (Join-Path $run 'request.json') -ErrorAction SilentlyContinue
        $final = Result $phase $reason $exitCode
        if ($digest) { $final.fixtureSha256 = $digest }
        $final.hung = $hung
        if ($stopPath) {
            # graceful: the game quit within the grace period; killed-after-Ns: it did not; closing-crashed: it died while quitting
            $final.stopPath = if ($stopPath -eq 'graceful' -and $phase -eq 'crashed') { 'closing-crashed' } else { $stopPath }
        }
        if ($game -and ($phase -in @('crashed', 'forced') -or ($phase -eq 'exited' -and $reason -eq 'unrequested-exit'))) {
            try {
                Save-Evidence $run $phase $reason $exitCode $started $hung
                $final.evidence = 'evidence'
            } catch {
                $_.Exception.Message | Set-Content -LiteralPath (Join-Path $run 'evidence-error.txt') -Encoding UTF8
            }
        }
        Write-Json $final $state
      } finally {
        # Last, so the junction goes away on every path (stop, crash, kill, failed preflight) before the lease is freed.
        try { $null = Exit-IsolatedProfile } catch {
            $_.Exception.Message | Add-Content -LiteralPath (Join-Path $run 'host-error.txt') -Encoding UTF8
        }
        Remove-Item -LiteralPath $lease
      }
    }
}

function Now-Ms { [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() }

# Leftovers of a hung or killed game keep the next start from working: the scheduled task stays "Running"
# (start then refuses with "Interactive task is busy") and the lease file blocks a new lease. Nothing is touched
# while a game process exists, unless -KillGame says the session being reset is ours and must go.
# MinAgeSeconds keeps a lease that was written moments ago (a launch still coming up) from being taken for stale.
function Reset-StaleHost([switch] $KillGame, [int] $MinAgeSeconds = 0) {
    $cleared = @()
    $games = @(Get-Process SkyrimSE, skse64_loader -ErrorAction SilentlyContinue)
    if ($games.Count -and $KillGame) {
        $games | Stop-Process -Force -ErrorAction SilentlyContinue
        $until = [DateTime]::UtcNow.AddSeconds(10)
        while ((Get-Process SkyrimSE, skse64_loader -ErrorAction SilentlyContinue) -and [DateTime]::UtcNow -lt $until) { Start-Sleep -Milliseconds 250 }
        $cleared += 'game-killed'
        $games = @(Get-Process SkyrimSE, skse64_loader -ErrorAction SilentlyContinue)
    }
    if ($games.Count) { return @{ gameRunning = $true; cleared = $cleared } }
    $leaseFile = Get-Item -LiteralPath $lease -ErrorAction SilentlyContinue
    $young = $leaseFile -and ([DateTime]::UtcNow - $leaseFile.LastWriteTimeUtc).TotalSeconds -lt $MinAgeSeconds
    if ($young) { return @{ gameRunning = $false; cleared = $cleared } }
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($task -and $task.State -ne 'Ready') {
        Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        $until = [DateTime]::UtcNow.AddSeconds(10)
        while ((Get-ScheduledTask -TaskName $taskName).State -ne 'Ready' -and [DateTime]::UtcNow -lt $until) { Start-Sleep -Milliseconds 250 }
        $cleared += 'task-stopped'
    }
    if ($leaseFile) {
        Remove-Item -LiteralPath $lease -ErrorAction SilentlyContinue
        $cleared += 'lease-removed'
    }
    # A junction left by a run whose host died would keep other installs writing into the test profile.
    if ((Exit-IsolatedProfile) -eq 'restored') { $cleared += 'profile-restored' }
    return @{ gameRunning = $false; cleared = $cleared }
}

function Find-Ffmpeg {
    $found = Get-Command ffmpeg -ErrorAction SilentlyContinue
    if ($found) { return $found.Source }
    $packages = Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages'
    $file = Get-ChildItem -LiteralPath $packages -Recurse -Filter ffmpeg.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if (!$file) { throw 'ffmpeg is not installed (winget install Gyan.FFmpeg)' }
    $file.FullName
}

function Capture-Paths($SessionId) {
    $dir = Join-Path (Join-Path $runs $SessionId) 'capture'
    New-Item -ItemType Directory -Path $dir -Force | Out-Null
    @{ dir = $dir; state = (Join-Path $dir 'state.json'); stop = (Join-Path $dir 'stop'); request = (Join-Path $dir 'request.json') }
}

function Restore-GameFocus {
    # Starting the capture task briefly takes the foreground, and an unfocused windowed game stops answering.
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class SkyrimDevToolsFocus
{
    [DllImport("user32.dll")] private static extern bool SetForegroundWindow(IntPtr window);
    [DllImport("user32.dll")] private static extern bool ShowWindow(IntPtr window, int command);
    [DllImport("user32.dll")] private static extern void keybd_event(byte key, byte scan, uint flags, UIntPtr extra);
    public static void Focus(IntPtr window)
    {
        // a synthetic Alt press lets a background process take the foreground
        keybd_event(0x12, 0, 0, UIntPtr.Zero);
        ShowWindow(window, 9);
        SetForegroundWindow(window);
        keybd_event(0x12, 0, 2, UIntPtr.Zero);
    }
}
'@ -ErrorAction SilentlyContinue
    $game = Get-Process SkyrimSE -ErrorAction SilentlyContinue | Where-Object MainWindowHandle -NE 0 | Select-Object -First 1
    if ($game) { [SkyrimDevToolsFocus]::Focus($game.MainWindowHandle) }
}

function Stop-Tree($Process) {
    # the cmd wrapper's child (ffmpeg) survives a plain Kill and would keep the screen grab running
    if ($Process -and !$Process.HasExited) { & taskkill.exe /T /F /PID $Process.Id 2>&1 | Out-Null }
}

function Run-Capture {
    # Runs in the interactive desktop session: desktop duplication does not work from an SSH session.
    $id = [IO.File]::ReadAllText($captureLease)
    if ($id -notmatch '^[a-f0-9]{32}$') { throw 'Invalid capture lease' }
    $paths = Capture-Paths $id
    $request = Read-Json $paths.request
    $out = Join-Path $paths.dir $request.file
    $log = Join-Path $paths.dir ($request.file + '.log')
    $process = $null
    $state = @{ phase = 'failed'; reason = 'start'; file = $request.file; path = $out }
    try {
        $ffmpeg = Find-Ffmpeg
        $scale = "scale=$($request.width):-2:flags=bilinear,format=nv12"
        # Desktop duplication loses access when the game owns the display, so GDI capture goes first.
        $sources = @(
            @('gdigrab', "-f gdigrab -framerate $($request.fps) -draw_mouse 0 -i desktop -vf ""$scale"""),
            @('ddagrab', "-filter_complex ""ddagrab=output_idx=0:framerate=$($request.fps):draw_mouse=0,hwdownload,format=bgra,$scale"""))
        $encoders = @(
            @('h264_amf', '-quality speed -rc cbr -b:v 8M'),
            @('libx264', '-preset ultrafast -crf 24 -pix_fmt yuv420p'))
        $attempts = foreach ($source in $sources) { foreach ($encoder in $encoders) { , @($source, $encoder) } }
        foreach ($attempt in $attempts) {
            $source, $encoder = $attempt
            Remove-Item -LiteralPath $out, $log -ErrorAction SilentlyContinue
            $command = '"{0}" -hide_banner -y {1} -c:v {2} {3} -movflags +frag_keyframe+empty_moov+default_base_moof "{4}" 2> "{5}"' -f
                $ffmpeg, $source[1], $encoder[0], $encoder[1], $out, $log
            $info = New-Object Diagnostics.ProcessStartInfo 'cmd.exe', ('/d /c "{0}"' -f $command)
            $info.UseShellExecute = $false
            $info.CreateNoWindow = $true
            $info.RedirectStandardInput = $true
            $process = [Diagnostics.Process]::Start($info)
            $started = $null
            $opened = $null
            $deadline = [DateTime]::UtcNow.AddSeconds(8)
            while (!$started -and !$process.HasExited -and [DateTime]::UtcNow -lt $deadline) {
                Start-Sleep -Milliseconds 40
                $text = if (Test-Path -LiteralPath $log) { Get-Content -LiteralPath $log -Raw -ErrorAction SilentlyContinue } else { '' }
                if (!$opened -and $text -match 'Output #0') { $opened = Now-Ms }
                # frames must actually flow before the recording counts as started
                if ($opened -and $text -match 'frame=\s*[1-9]') { $started = $opened }
            }
            if ($started) {
                $state = @{ phase = 'recording'; file = $request.file; path = $out; encoder = $encoder[0]; source = $source[0]
                            fps = $request.fps; width = $request.width; startEpochMs = $started }
                Write-Json $state $paths.state
                Restore-GameFocus
                break
            }
            Stop-Tree $process
            $process = $null
        }
        if (!$process) {
            $tail = if (Test-Path -LiteralPath $log) { (Get-Content -LiteralPath $log -Tail 8) -join "`n" } else { '' }
            $state = @{ phase = 'failed'; reason = 'no encoder started'; detail = $tail }
            return
        }
        # a recording must not outlive its game if nobody calls capture-stop
        $gameGoneSince = $null
        $nextCheck = [DateTime]::UtcNow
        while (!$process.HasExited -and !(Test-Path -LiteralPath $paths.stop)) {
            Start-Sleep -Milliseconds 100
            if ([DateTime]::UtcNow -ge $nextCheck) {
                $nextCheck = [DateTime]::UtcNow.AddSeconds(2)
                if (Get-Process SkyrimSE -ErrorAction SilentlyContinue) { $gameGoneSince = $null }
                elseif (!$gameGoneSince) { $gameGoneSince = [DateTime]::UtcNow }
                elseif ([DateTime]::UtcNow -ge $gameGoneSince.AddSeconds(20)) { break }
            }
        }
        $stoppedAt = Now-Ms
        $diedEarly = $process.HasExited -and !(Test-Path -LiteralPath $paths.stop)
        if (!$process.HasExited) {
            # q makes ffmpeg finish the file cleanly
            $process.StandardInput.WriteLine('q')
            $process.StandardInput.Flush()
            if (!$process.WaitForExit(20000)) { Stop-Tree $process }
        }
        $final = $state.Clone()
        $final.phase = if ($diedEarly) { 'failed' } else { 'stopped' }
        if ($diedEarly) {
            $final.reason = 'the recorder exited while recording'
            $final.detail = (Get-Content -LiteralPath $log -Tail 6 -ErrorAction SilentlyContinue) -join "`n"
        }
        $final.stopEpochMs = $stoppedAt
        $final.bytes = if (Test-Path -LiteralPath $out) { (Get-Item -LiteralPath $out).Length } else { 0 }
        $state = $final
    } catch {
        $state = @{ phase = 'failed'; reason = $_.Exception.Message }
    } finally {
        Stop-Tree $process
        Write-Json $state $paths.state
        Remove-Item -LiteralPath $captureLease -ErrorAction SilentlyContinue
    }
}

function Capture-State($Paths) {
    if (Test-Path -LiteralPath $Paths.state) { Get-Content -LiteralPath $Paths.state -Raw } else { '{"phase":"idle"}' }
}

switch ($Action) {
    'install-task' {
        $arguments = '-NoProfile -NonInteractive -ExecutionPolicy RemoteSigned -File "{0}" run' -f $PSCommandPath
        $task = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arguments
        $principal = New-ScheduledTaskPrincipal -UserId $account -LogonType Interactive
        $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
        Register-ScheduledTask -TaskName $taskName -Action $task -Principal $principal -Settings $settings | Out-Null
    }
    'start' {
        New-Item -ItemType Directory -Path $runs -Force | Out-Null
        $Session = [guid]::NewGuid().ToString('N')
        $run = Join-Path $runs $Session
        $bytes = New-Object byte[] 32
        $random = [Security.Cryptography.RandomNumberGenerator]::Create()
        try { $random.GetBytes($bytes) } finally { $random.Dispose() }
        $token = [BitConverter]::ToString($bytes).Replace('-', '').ToLowerInvariant()
        $config = Read-Json (Join-Path $root 'config.json')
        $stale = Reset-StaleHost -MinAgeSeconds 90
        if ((Get-ScheduledTask -TaskName $taskName).State -ne 'Ready') { throw 'Interactive task is busy' }
        # Never start next to somebody else's game or capture: refusing is the only action taken.
        if (Get-Process SkyrimSE, skse64_loader -ErrorAction SilentlyContinue) { throw 'A game or loader process is already running; start refused' }
        if ((Test-Path -LiteralPath $captureLease) -or (Get-ScheduledTask -TaskName $captureTask -ErrorAction SilentlyContinue).State -eq 'Running' -or
            (Get-Process ffmpeg -ErrorAction SilentlyContinue)) {
            throw 'A capture is running; start refused'
        }
        $stream = [IO.File]::Open($lease, 'CreateNew', 'Write', 'None')
        try {
            $id = [Text.Encoding]::ASCII.GetBytes($Session)
            $stream.Write($id, 0, $id.Length)
        } finally { $stream.Dispose() }
        try {
            New-Item -ItemType Directory -Path $run | Out-Null
            Write-Json @{ token = $token; mode = $Mode } (Join-Path $run 'request.json')
            Write-Json (Result 'starting') (Join-Path $run 'state.json')
            Start-ScheduledTask -TaskName $taskName
        } catch {
            if (Test-Path -LiteralPath $run) {
                Remove-Item -LiteralPath (Join-Path $run 'request.json') -ErrorAction SilentlyContinue
                Write-Json (Result 'failed' 'scheduler') (Join-Path $run 'state.json')
            }
            Remove-Item -LiteralPath $lease
            throw
        }
        $started = @{ sessionId = $Session; port = $config.port; token = $token }
        if ($stale.cleared.Count) { $started.staleCleared = @($stale.cleared) }
        $started | ConvertTo-Json -Compress
    }
    'health' {
        # Read from outside the game on purpose: a hung main thread cannot answer a plugin call, and the thread
        # count is the lead on the slow degradation of long sessions.
        $game = Get-Process SkyrimSE -ErrorAction SilentlyContinue | Select-Object -First 1
        if (!$game) {
            '{"running":false}'
        } else {
            $before = $game.TotalProcessorTime.TotalSeconds
            $clock = [Diagnostics.Stopwatch]::StartNew()
            Start-Sleep -Milliseconds 500
            $game.Refresh()
            $seconds = [Math]::Max($clock.Elapsed.TotalSeconds, 0.001)
            @{
                running = $true; threads = $game.Threads.Count; handles = $game.HandleCount
                privateBytes = $game.PrivateMemorySize64; workingSetBytes = $game.WorkingSet64
                cpuSeconds = [Math]::Round($game.TotalProcessorTime.TotalSeconds, 1)
                cpuCores = [Math]::Round(($game.TotalProcessorTime.TotalSeconds - $before) / $seconds, 2)
                responding = [bool]$game.Responding
                uptimeSeconds = [int]([DateTime]::Now - $game.StartTime).TotalSeconds
            } | ConvertTo-Json -Compress
        }
    }
    'reset' {
        (Reset-StaleHost -KillGame:$Force) | ConvertTo-Json -Compress
    }
    'run' { Run-Game }
    'restore-profile' {
        @{ result = (Exit-IsolatedProfile) } | ConvertTo-Json -Compress
    }
    'install-capture-task' {
        $arguments = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy RemoteSigned -File "{0}" capture-run' -f $PSCommandPath
        $task = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arguments
        $principal = New-ScheduledTaskPrincipal -UserId $account -LogonType Interactive
        $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
        Register-ScheduledTask -TaskName $captureTask -Action $task -Principal $principal -Settings $settings -Force | Out-Null
    }
    'capture-start' {
        if (!$Session) { throw 'Session id is required' }
        $paths = Capture-Paths $Session
        if ((Get-ScheduledTask -TaskName $captureTask).State -ne 'Ready') { throw 'A capture is already running' }
        Remove-Item -LiteralPath $paths.stop, $paths.state -ErrorAction SilentlyContinue
        $file = 'capture-{0}.mp4' -f [DateTime]::UtcNow.ToString('yyyyMMdd-HHmmss')
        Write-Json @{ fps = $Fps; width = $Width; file = $file } $paths.request
        Write-Json @{ phase = 'starting' } $paths.state
        [IO.File]::WriteAllText($captureLease, $Session)
        Start-ScheduledTask -TaskName $captureTask
        $deadline = [DateTime]::UtcNow.AddSeconds(20)
        do {
            Start-Sleep -Milliseconds 200
            $current = Read-Json $paths.state
        } until ($current.phase -ne 'starting' -or [DateTime]::UtcNow -ge $deadline)
        if ($current.phase -eq 'starting') {
            Remove-Item -LiteralPath $captureLease -ErrorAction SilentlyContinue
            $current = @{ phase = 'failed'; reason = 'capture task did not start' }
        }
        $current | ConvertTo-Json -Compress
    }
    'capture-run' { Run-Capture }
    'capture-status' {
        if (!$Session) { throw 'Session id is required' }
        (Capture-State (Capture-Paths $Session)).Trim()
    }
    'capture-stop' {
        if (!$Session) { throw 'Session id is required' }
        $paths = Capture-Paths $Session
        $current = Read-Json $paths.state
        if ($current.phase -eq 'recording') {
            [IO.File]::WriteAllText($paths.stop, '')
            $deadline = [DateTime]::UtcNow.AddSeconds(30)
            do {
                Start-Sleep -Milliseconds 200
                $current = Read-Json $paths.state
            } until ($current.phase -ne 'recording' -or [DateTime]::UtcNow -ge $deadline)
        }
        $current | ConvertTo-Json -Compress
    }
    'crashes' {
        # The latest crash logs of the isolated profile, newest first (read-only, safe during a run).
        ConvertTo-Json -InputObject @(Get-CrashLogs $null 5) -Compress -Depth 5
    }
    'active' {
        # The session holding the lease, so a caller whose own program died can still stop that game.
        $id = $null
        if (Test-Path -LiteralPath $lease) {
            $text = [IO.File]::ReadAllText($lease)
            if ($text -match '^[a-f0-9]{32}$') { $id = $text }
        }
        @{ sessionId = $id } | ConvertTo-Json -Compress
    }
    'idle' {
        # Idle means the lease is released and no game or loader process is left holding files.
        $count = @(Get-Process SkyrimSE, skse64_loader -ErrorAction SilentlyContinue).Count
        $leased = Test-Path -LiteralPath $lease
        @{ idle = (!$leased -and $count -eq 0); lease = [bool]$leased; processes = $count } | ConvertTo-Json -Compress
    }
    'status' {
        if (!$Session) { throw 'Session id is required' }
        Get-Content -LiteralPath (Join-Path $runs "$Session\state.json") -Raw
    }
    'evidence' {
        if (!$Session) { throw 'Session id is required' }
        $file = Join-Path $runs "$Session\evidence\evidence.json"
        if (Test-Path -LiteralPath $file) {
            $summary = Read-Json $file
            foreach ($name in @('papyrus-tail.log', 'plugin-tail.log', 'rpc-tail.jsonl')) {
                $path = Join-Path (Split-Path $file) $name
                if (Test-Path -LiteralPath $path) {
                    $summary | Add-Member -NotePropertyName $name -NotePropertyValue (@(Get-Content -LiteralPath $path -Tail 40) -join "`n") -Force
                }
            }
            $summary | ConvertTo-Json -Compress -Depth 5
        } else {
            '{}'
        }
    }
    'stop' {
        if (!$Session) { throw 'Session id is required' }
        $run = Join-Path $runs $Session
        $state = Read-Json (Join-Path $run 'state.json')
        if ($state.phase -in @('starting', 'running')) {
            [IO.File]::WriteAllText((Join-Path $run 'stop'), "$Grace")
        }
        if ($state.phase -eq 'starting' -and (Get-ScheduledTask -TaskName $taskName).State -eq 'Ready' -and
            (Test-Path -LiteralPath $lease) -and [IO.File]::ReadAllText($lease) -eq $Session) {
            $state = Read-Json (Join-Path $run 'state.json')
            if ($state.phase -eq 'starting') {
                $state = Result 'cancelled' 'task-did-not-start'
                Remove-Item -LiteralPath (Join-Path $run 'request.json') -ErrorAction SilentlyContinue
                Write-Json $state (Join-Path $run 'state.json')
                Remove-Item -LiteralPath $lease
            }
        }
        $state | ConvertTo-Json -Compress
    }
}
