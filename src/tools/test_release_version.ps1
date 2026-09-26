$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'release_version.ps1')
$repo=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$manifest=(Get-Content -LiteralPath (Join-Path $repo 'Cargo.toml') -Raw).Replace("`r`n","`n")
$version=((Select-String -LiteralPath (Join-Path $repo 'Cargo.toml') -Pattern '^version = "([^"]+)"$').Matches[0].Groups[1].Value)
foreach($newline in @("`n","`r`n")) {
    Assert-ReleaseVersion ($manifest.Replace("`n",$newline)) $version
    $rejected=$false
    try { Assert-ReleaseVersion ($manifest.Replace("`n",$newline)) '99.99.99' } catch { $rejected=$true }
    if(!$rejected){throw 'Mismatched release version was accepted'}
    $decoy="[package]`nversion = `"0.0.0`"`n[dependencies.example]`nversion = `"$version`"`n"
    $rejected=$false
    try { Assert-ReleaseVersion ($decoy.Replace("`n",$newline)) $version } catch { $rejected=$true }
    if(!$rejected){throw 'Dependency version was mistaken for package version'}
}
Write-Host 'PASS release version: LF/CRLF accepted, mismatches and dependency decoys rejected (6 checks)'
