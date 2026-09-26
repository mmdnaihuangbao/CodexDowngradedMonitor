$ErrorActionPreference='Stop'
$repo=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$output=Join-Path $repo ('out/test-results/staging-'+(Get-Date -Format 'yyyyMMdd-HHmmss'))
$version=((Select-String -LiteralPath (Join-Path $repo 'Cargo.toml') -Pattern '^version = "([^"]+)"$').Matches[0].Groups[1].Value)
$existing=Join-Path $output "CodexDowngradedMonitor-v$version-windows-amd64"
New-Item -ItemType Directory -Force (Join-Path $existing 'data') | Out-Null
[IO.File]::WriteAllText((Join-Path $existing 'config.json'),'{"custom":"keep"}')
[IO.File]::WriteAllText((Join-Path $existing 'data/sentinel.txt'),'existing user data')
[IO.File]::WriteAllText((Join-Path $existing 'CodexDowngradedMonitor.exe'),'existing installation sentinel')
$before=@(Get-ChildItem -LiteralPath $existing -Recurse -File | Sort-Object FullName | Get-FileHash | ForEach-Object { $_.Path+'|'+$_.Hash })
& (Join-Path $PSScriptRoot 'package_release.ps1') -Version $version -OutputDirectory $output
$after=@(Get-ChildItem -LiteralPath $existing -Recurse -File | Sort-Object FullName | Get-FileHash | ForEach-Object { $_.Path+'|'+$_.Hash })
if(Compare-Object $before $after){throw 'Packaging changed the existing extracted installation'}
Write-Host 'PASS packaging preserves existing extracted EXE, config.json and data byte-for-byte'
