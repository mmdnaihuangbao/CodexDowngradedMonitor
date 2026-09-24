[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Version,

    [Parameter(Mandatory = $true)]
    [string]$Changes,

    [string]$OutputDirectory = (Join-Path (Split-Path -Parent $PSScriptRoot) 'release')
)

$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$versionText = $Version.Trim()
if ($versionText.StartsWith('v')) {
    $versionText = $versionText.Substring(1)
}
if ($versionText -notmatch '^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$') {
    throw 'Version must look like 1.2.3, optionally with a prerelease or build suffix.'
}
if ([string]::IsNullOrWhiteSpace($Changes)) {
    throw 'Changes must describe what is included in this release.'
}

$tag = "v$versionText"
$packageName = "CodexDowngradedMonitor-$tag-windows-amd64"
$outputRoot = [IO.Path]::GetFullPath($OutputDirectory)
$stage = Join-Path $outputRoot $packageName
$zipPath = Join-Path $outputRoot "$packageName.zip"
$notesPath = Join-Path $outputRoot "$packageName-release-notes.md"

New-Item -ItemType Directory -Force -Path $outputRoot | Out-Null
if (Test-Path -LiteralPath $stage) {
    Remove-Item -LiteralPath $stage -Recurse -Force
}
if (Test-Path -LiteralPath $zipPath) {
    Remove-Item -LiteralPath $zipPath -Force
}
if (Test-Path -LiteralPath $notesPath) {
    Remove-Item -LiteralPath $notesPath -Force
}
New-Item -ItemType Directory -Force -Path $stage | Out-Null

function Copy-RequiredFile {
    param([string]$RelativePath)

    $source = Join-Path $repoRoot $RelativePath
    if (!(Test-Path -LiteralPath $source -PathType Leaf)) {
        throw "Required release file is missing: $RelativePath"
    }
    $destination = Join-Path $stage $RelativePath
    $parent = Split-Path -Parent $destination
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    Copy-Item -LiteralPath $source -Destination $destination -Force
}

# Runtime-only files. Source documentation, tests and development scripts stay out
# of the end-user package; the package README is copied from the dedicated guide.
$runtimeFiles = @(
    'collector.py',
    'config_store.py',
    'config.json',
    'evidence_index.py',
    'history_store.py',
    'instance_guard.py',
    'process_job.py',
    'icon.png',
    'native_scanner.py',
    'process_discovery.py',
    'start.py',
    'start.bat',
    'start-cpp.bat',
    'stop-cpp.bat',
    'web\index.html',
    'web\style.css',
    'web\app.js',
    'cpp_collector\build\collector_native.exe'
)
foreach ($file in $runtimeFiles) {
    Copy-RequiredFile $file
}

foreach ($license in @('LICENSE', 'LICENSE.txt')) {
    $licensePath = Join-Path $repoRoot $license
    if (Test-Path -LiteralPath $licensePath -PathType Leaf) {
        Copy-Item -LiteralPath $licensePath -Destination (Join-Path $stage $license) -Force
    }
}

Copy-RequiredFile 'docs\RELEASE_USAGE.md'
Move-Item -LiteralPath (Join-Path $stage 'docs\RELEASE_USAGE.md') `
    -Destination (Join-Path $stage 'README.md') -Force
Remove-Item -LiteralPath (Join-Path $stage 'docs') -Recurse -Force

$notes = @"
# CodeDowngradedMonitor $tag

## Changes

$Changes

## Package

- Platform: Windows amd64
- The package includes the prebuilt C++ collector.
- End users only need Python 3.8+ and a running Codex installation.
"@
Set-Content -LiteralPath $notesPath -Value $notes -Encoding utf8

Compress-Archive -Path $stage -DestinationPath $zipPath -Force

Write-Output "PACKAGE_NAME=$packageName"
Write-Output "PACKAGE_DIR=$stage"
Write-Output "PACKAGE_ZIP=$zipPath"
Write-Output "RELEASE_NOTES=$notesPath"
