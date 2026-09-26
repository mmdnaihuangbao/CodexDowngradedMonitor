[CmdletBinding()]
param([string]$Version='0.5.0',[string]$Changes='Rust 重构；便携目录；保留旧配置和历史数据。',[string]$OutputDirectory='')
$ErrorActionPreference='Stop'
$repo=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$versionText=$Version.Trim().TrimStart('v')
if($versionText -notmatch '^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$'){throw 'Invalid version'}
$manifest=Get-Content -LiteralPath (Join-Path $repo 'Cargo.toml') -Raw
. (Join-Path $PSScriptRoot 'release_version.ps1')
Assert-ReleaseVersion $manifest $versionText
if(!$OutputDirectory){$OutputDirectory=Join-Path $repo 'out/release'}
$outputRoot=[IO.Path]::GetFullPath($OutputDirectory)
$packageName="CodexDowngradedMonitor-v$versionText-windows-amd64"
$stagingRoot=Join-Path $outputRoot ('.staging/'+[Guid]::NewGuid().ToString('N'))
$stage=[IO.Path]::GetFullPath((Join-Path $stagingRoot $packageName))
if(!$stage.StartsWith($outputRoot.TrimEnd('\','/')+[IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)){throw 'Invalid staging path'}
New-Item -ItemType Directory -Force $outputRoot | Out-Null
# Never clear the previous extracted package: users may already keep config/data there.
New-Item -ItemType Directory -Force $stage | Out-Null
Copy-Item -LiteralPath (Join-Path $repo 'out/build/x86_64-pc-windows-msvc/release/CodexDowngradedMonitor.exe') -Destination $stage
Copy-Item -Path (Join-Path $repo 'src/launchers/*.bat') -Destination $stage
Copy-Item -LiteralPath (Join-Path $repo 'src/Web') -Destination $stage -Recurse
$licenses=Join-Path $stage 'Web/licenses'
New-Item -ItemType Directory -Force $licenses | Out-Null
Copy-Item -LiteralPath (Join-Path $repo 'LICENSE') -Destination (Join-Path $licenses 'PROJECT-LICENSE.txt')
Push-Location $repo
try{
    $env:CARGO_HOME=Join-Path $repo 'out/cargo'
    $rawMetadata=cargo metadata --locked --format-version 1 --filter-platform x86_64-pc-windows-msvc
    if($LASTEXITCODE -ne 0){throw 'Cannot resolve locked license metadata'}
    $metadata=$rawMetadata | ConvertFrom-Json
    $resolved=@{};foreach($node in $metadata.resolve.nodes){$resolved[$node.id]=$true}
    $notices=[Collections.Generic.List[string]]::new()
    foreach($package in ($metadata.packages | Sort-Object name,version)){
        if(!$package.source -or !$resolved.ContainsKey($package.id)){continue}
        $notices.Add("$($package.name) $($package.version) | $($package.license) | $($package.repository)")
        $source=Split-Path -Parent $package.manifest_path
        $destination=Join-Path $licenses "$($package.name)-$($package.version)"
        New-Item -ItemType Directory -Force $destination | Out-Null
        $files=@(Get-ChildItem -LiteralPath $source -File | Where-Object { $_.Name -match '^(LICENSE|COPYING|NOTICE)' })
        if($package.license_file){$files+=Get-Item -LiteralPath (Join-Path $source $package.license_file)}
        foreach($file in $files){Copy-Item -LiteralPath $file.FullName -Destination $destination -Force}
        if(!$files.Count){throw "No license text found for $($package.name) $($package.version)"}
    }
    $notices.Add('SQLite: public domain; bundled source in libsqlite3-sys. https://sqlite.org/copyright.html')
    [IO.File]::WriteAllLines((Join-Path $licenses 'THIRD-PARTY.txt'),$notices,[Text.UTF8Encoding]::new($false))
    Copy-Item -LiteralPath (Join-Path $repo 'Cargo.lock') -Destination (Join-Path $licenses 'Cargo.lock.txt')
}finally{Pop-Location}
$rootEntries=@(Get-ChildItem -LiteralPath $stage | Select-Object -ExpandProperty Name | Sort-Object)
if(($rootEntries -join '|') -ne 'CodexDowngradedMonitor.exe|start.bat|stop.bat|Web'){throw 'Release root whitelist failed'}
$zipPath=Join-Path $outputRoot "$packageName.zip"
Get-ChildItem -LiteralPath $stage -Recurse -File | Where-Object LastWriteTimeUtc -lt ([DateTime]'1980-01-01') | ForEach-Object { $_.LastWriteTimeUtc=[DateTime]'1980-01-01' }
Compress-Archive -LiteralPath $stage -DestinationPath $zipPath -Force
$notesPath=Join-Path $outputRoot "$packageName-release-notes.md"
[IO.File]::WriteAllText($notesPath,"# v$versionText`n`n$Changes`n`nWindows x64 portable package. No Python, Rust, or Visual Studio runtime installation required.`nUpgrade: stop the old version; copy config.json and the entire data directory into the new package.`n",[Text.UTF8Encoding]::new($false))
Get-FileHash -LiteralPath $zipPath -Algorithm SHA256 | Format-List
Write-Output "PACKAGE_ZIP=$zipPath"
Write-Output "PACKAGE_DIRECTORY=$stage"
Write-Output "RELEASE_NOTES=$notesPath"
