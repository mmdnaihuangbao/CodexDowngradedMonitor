param()
$ErrorActionPreference = 'Stop'
$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
if (!(Test-Path -LiteralPath $vswhere)) { throw 'VS2022 Installer / vswhere not found.' }
$vs = & $vswhere -latest -version '[17.0,18.0)' -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (!$vs) { throw 'Install the VS2022 Desktop development with C++ workload.' }
$devcmd = Join-Path $vs 'Common7\Tools\VsDevCmd.bat'
$buildDir = Join-Path $PSScriptRoot 'build'
New-Item -ItemType Directory -Force -Path $buildDir | Out-Null
# cmd is used only for the MSVC environment and compiler invocation, never file deletion.
$command = 'call "{0}" -no_logo -arch=x64 -host_arch=x64 && cl /nologo /std:c++17 /EHsc /O2 /W4 /utf-8 /DWIN32 /DNDEBUG "{1}" /Fe:"{2}" /Fo:"{3}" /link /INCREMENTAL:NO' -f $devcmd,(Join-Path $PSScriptRoot 'src\main.cpp'),(Join-Path $buildDir 'collector_native.exe'),(Join-Path $buildDir 'main.obj')
& $env:ComSpec /d /s /c $command
if ($LASTEXITCODE -ne 0) { throw 'C++ collector build failed.' }
Write-Host ('Built: ' + (Join-Path $buildDir 'collector_native.exe'))
