param([int]$Port=48880)
$ErrorActionPreference='Stop'
$repo=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$run=Join-Path $repo ('out/test-results/live-'+(Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Force $run | Out-Null
Copy-Item -LiteralPath (Join-Path $repo 'out/build/x86_64-pc-windows-msvc/release/CodexDowngradedMonitor.exe') -Destination $run
Copy-Item -LiteralPath (Join-Path $repo 'src/Web') -Destination $run -Recurse
Copy-Item -LiteralPath (Join-Path $repo 'config.json'),(Join-Path $repo 'data') -Destination $run -Recurse
$fixture=Join-Path $repo 'out/build/x86_64-pc-windows-msvc/release/monitor-fixture.exe'
& $fixture --inspect-db (Join-Path $run 'data/monitor.sqlite') | Set-Content -LiteralPath (Join-Path $run 'before.json') -Encoding utf8
$exe=Join-Path $run 'CodexDowngradedMonitor.exe'
$p=Start-Process -FilePath $exe -ArgumentList "--start --no-open --port $Port" -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $run 'start.txt') -RedirectStandardError (Join-Path $run 'start-error.txt')
if(!$p.WaitForExit(15000)){throw 'Live start timed out'}
if($p.ExitCode -ne 0){throw (Get-Content -LiteralPath (Join-Path $run 'start-error.txt') -Raw)}
Get-Content -LiteralPath (Join-Path $run 'before.json'),(Join-Path $run 'start.txt')
Write-Output "LIVE_DIRECTORY=$run"
