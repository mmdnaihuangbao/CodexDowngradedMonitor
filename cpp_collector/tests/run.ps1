$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot '..\build.ps1')
$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
$vs = & $vswhere -latest -version '[17.0,18.0)' -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
$devcmd = Join-Path $vs 'Common7\Tools\VsDevCmd.bat'
$buildDir = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\build'))
$command = 'call "{0}" -no_logo -arch=x64 -host_arch=x64 && cl /nologo /std:c++17 /EHsc /O2 /W4 /utf-8 "{1}" /Fe:"{2}" /Fo:"{3}" /link /INCREMENTAL:NO' -f $devcmd,(Join-Path $PSScriptRoot 'extract_tests.cpp'),(Join-Path $buildDir 'extract_tests.exe'),(Join-Path $buildDir 'extract_tests.obj')
& $env:ComSpec /d /s /c $command
if ($LASTEXITCODE -ne 0) { throw 'Test build failed.' }
& (Join-Path $buildDir 'extract_tests.exe')
if ($LASTEXITCODE -ne 0) { throw 'Native tests failed.' }
python (Join-Path $PSScriptRoot 'test_collectors.py')
if ($LASTEXITCODE -ne 0) { throw 'Integration tests failed.' }
