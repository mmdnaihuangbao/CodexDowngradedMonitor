param([switch]$Debug,[switch]$Tests)
$ErrorActionPreference = 'Stop'
$repo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
Push-Location $repo
try {
    $env:CARGO_HOME = Join-Path $repo 'out/cargo'
    $mode = if ($Debug) { 'debug' } else { 'release' }
    $buildArgs = @('build','--locked','--bin','CodexDowngradedMonitor')
    if (!$Debug) { $buildArgs += '--release' }
    & cargo @buildArgs
    if ($LASTEXITCODE -ne 0) { throw 'Rust build failed' }
    if ($Tests) {
        & (Join-Path $PSScriptRoot 'test_release_version.ps1')
        cargo test --locked
        if ($LASTEXITCODE -ne 0) { throw 'Rust tests failed' }
        cargo build --locked --release --features test-tools --bin monitor-fixture
        if ($LASTEXITCODE -ne 0) { throw 'Fixture build failed' }
    }
    $run = Join-Path $repo 'out/run'
    New-Item -ItemType Directory -Force $run | Out-Null
    Copy-Item -LiteralPath (Join-Path $repo "out/build/x86_64-pc-windows-msvc/$mode/CodexDowngradedMonitor.exe") -Destination $run
    Copy-Item -LiteralPath (Join-Path $repo 'src/Web') -Destination $run -Recurse -Force
    Copy-Item -Path (Join-Path $repo 'src/launchers/*.bat') -Destination $run
} finally { Pop-Location }
