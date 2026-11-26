# Sequential write load for finding out whether game hitches follow disk saturation; writes under the scratch tree only.
param([Parameter(Mandatory)] [string] $Path, [int] $Gigabytes = 12)
$buffer = New-Object byte[] (64MB)
(New-Object Random).NextBytes($buffer)
$stream = New-Object IO.FileStream($Path, 'Create', 'Write', 'None', 1MB, 'WriteThrough')
try { for ($i = 0; $i -lt $Gigabytes * 16; $i++) { $stream.Write($buffer, 0, $buffer.Length) } } finally { $stream.Dispose(); Remove-Item -LiteralPath $Path -Force }
