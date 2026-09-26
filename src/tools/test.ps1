param([string]$Executable = '',[int]$PerformanceRuns = 20)
$ErrorActionPreference = 'Stop'
$repo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
if (!$Executable) { $Executable = Join-Path $repo 'out/run/CodexDowngradedMonitor.exe' }
$fixtureExe = Join-Path $repo 'out/build/x86_64-pc-windows-msvc/release/monitor-fixture.exe'
$run = Join-Path $repo ('out/test-results/e2e-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Force $run | Out-Null
$install = Join-Path $run '中文 空格 portable'
$other = Join-Path $run 'second copy'
foreach ($folder in @($install,$other)) {
    New-Item -ItemType Directory -Force $folder | Out-Null
    Copy-Item -LiteralPath $Executable -Destination (Join-Path $folder 'CodexDowngradedMonitor.exe')
    Copy-Item -LiteralPath (Join-Path $repo 'src/Web') -Destination $folder -Recurse
}
$exe = Join-Path $install 'CodexDowngradedMonitor.exe'
$exe2 = Join-Path $other 'CodexDowngradedMonitor.exe'
$checks = [Collections.Generic.List[string]]::new()
function Check($condition,[string]$message) { if (!$condition) { throw $message }; $checks.Add($message); Write-Host "PASS $message" }
function Spawn([string]$file,[string[]]$arguments) {
    $info = [Diagnostics.ProcessStartInfo]::new($file)
    $info.UseShellExecute = $false; $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true; $info.RedirectStandardError = $true; $info.RedirectStandardInput = $true
    $info.WorkingDirectory = $run
    foreach ($argument in $arguments) { $info.ArgumentList.Add($argument) }
    $p = [Diagnostics.Process]::new(); $p.StartInfo = $info
    if (!$p.Start()) { throw "Failed to start $file" }
    return $p
}
function Command([string]$file,[string[]]$arguments) {
    $watch = [Diagnostics.Stopwatch]::StartNew(); $p = Spawn $file $arguments
    $stdout = $p.StandardOutput.ReadToEndAsync(); $stderr = $p.StandardError.ReadToEndAsync()
    if (!$p.WaitForExit(15000)) { throw "Command timed out: $arguments" }
    $watch.Stop()
    return @{Code=$p.ExitCode;Text=$stdout.GetAwaiter().GetResult();Error=$stderr.GetAwaiter().GetResult();Seconds=$watch.Elapsed.TotalSeconds}
}
$handler = [Net.Http.HttpClientHandler]::new(); $handler.UseProxy = $false
$http = [Net.Http.HttpClient]::new($handler); $http.Timeout = [TimeSpan]::FromSeconds(5)
$port = Get-Random -Minimum 30000 -Maximum 40000
$reserved = [Collections.Generic.List[Net.Sockets.TcpListener]]::new()
try {
    foreach ($candidatePort in $port..($port+11)) { $probe=[Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback,$candidatePort);$probe.Start();$reserved.Add($probe) }
} finally { foreach ($probe in $reserved) { $probe.Stop() } }
$base = "http://127.0.0.1:$port"
function GetJson([string]$path) { return ($http.GetStringAsync($base+$path).GetAwaiter().GetResult() | ConvertFrom-Json) }
function PostJson([string]$path,$body) {
    $content = [Net.Http.StringContent]::new(($body | ConvertTo-Json -Compress),[Text.Encoding]::UTF8,'application/json')
    $response = $http.PostAsync($base+$path,$content).GetAwaiter().GetResult()
    return @{Status=[int]$response.StatusCode;Body=($response.Content.ReadAsStringAsync().GetAwaiter().GetResult() | ConvertFrom-Json)}
}
$fixture = $null; $streams = [Collections.Generic.List[IDisposable]]::new(); $hostProcessId = 0
try {
    $existing = Command $exe @('--status')
    Check ($existing.Code -eq 1) 'No existing service before isolated tests'
    $fixture = Spawn $fixtureExe @()
    Check ($fixture.StandardOutput.ReadLine() -match '^ready ') 'Read-only fixture ready'
    $seed = @{version='0.1';host='127.0.0.1';cpp_port=$port;expect='expected';min_interval_ms=25;workers=2;custom='preserved'} | ConvertTo-Json
    [IO.File]::WriteAllText((Join-Path $install 'config.json'),$seed,[Text.UTF8Encoding]::new($false))
    $startArgs = @('--start','--no-open','--no-evidence','--pid',"$($fixture.Id)")
    $started = Command $exe $startArgs
    Check ($started.Code -eq 0) "Cold start succeeded: $($started.Text.Trim()) $($started.Error)"
    $health = GetJson '/api/health'; $hostProcessId = $health.pid
    Check ($health.backend -eq 'rust' -and $health.service -eq 'codex-model-monitor') 'Rust service identity'
    $deadline = [DateTime]::UtcNow.AddSeconds(15)
    do { $snap = GetJson '/api/snapshot';if ($snap.stats.rounds -ge 3) { break };Start-Sleep -Milliseconds 50 } while ([DateTime]::UtcNow -lt $deadline)
    $snap | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath (Join-Path $run 'snapshot.json') -Encoding utf8
    Check ($snap.stats.rounds -ge 3) 'Persistent worker completed multiple sweeps'
    $rows = @{}; foreach ($row in $snap.responses) { $rows[$row.rid] = $row }
    Check ($rows['resp_down'].verdict -eq 'downgrade') 'Paired mismatch classified downgrade'
    Check ($rows['resp_sub'].verdict -eq 'subtask') 'Expected-model subtask classification'
    Check ($rows['resp_unknown'].verdict -eq 'incomplete') 'Missing evidence remains incomplete'
    Check ($rows['resp_normal'].req_model -eq 'expected') 'Request pairing preserved'
    Check ($null -ne $rows['resp_boundary']) 'Cross-block response captured'
    $diag = GetJson '/api/diagnose'
    Check ($diag.ok -and $diag.sweep.bytes -gt 64MB) 'Diagnostic reports actual read-only scan'
    Check ($diag.sweep.native.merge_cost -gt 0 -and $diag.sweep.native.serialize_cost -gt 0) 'Scan merge and serialization timings are measured'
    Check (@($diag.sweep.request_candidates | Where-Object model -eq 'late-large').Count -ge 1) 'Supplemental read captures large request'
    $fixture.StandardInput.WriteLine('check');$fixture.StandardInput.Flush()
    Check ($fixture.StandardOutput.ReadLine() -eq 'unchanged') 'Observed process memory unchanged'
    $reused = Command $exe2 @('--start','--no-open','--port',"$($port+1)")
    Check ($reused.Code -eq 0 -and (GetJson '/api/health').pid -eq $hostProcessId) 'Second installation and different port reuse one user instance'
    Check (!(Test-Path -LiteralPath (Join-Path $other 'data'))) 'Reused installation creates no data'
    $request = [Net.Http.HttpRequestMessage]::new([Net.Http.HttpMethod]::Get,$base+'/api/stream')
    $response = $http.SendAsync($request,[Net.Http.HttpCompletionOption]::ResponseHeadersRead).GetAwaiter().GetResult();$streams.Add($response)
    $reader = [IO.StreamReader]::new($response.Content.ReadAsStream());$streams.Add($reader)
    $line = $reader.ReadLineAsync().WaitAsync([TimeSpan]::FromSeconds(3)).GetAwaiter().GetResult()
    Check ($line.StartsWith('data: ') -and ($line.Substring(6) | ConvertFrom-Json).type -eq 'snapshot') 'SSE starts with snapshot'
    $eventDeadline=[DateTime]::UtcNow.AddSeconds(5)
    do {
        $line=$reader.ReadLineAsync().WaitAsync([TimeSpan]::FromSeconds(3)).GetAwaiter().GetResult()
        if($line.StartsWith('data: ') -and ($line.Substring(6) | ConvertFrom-Json).type -eq 'patch'){break}
    } while([DateTime]::UtcNow -lt $eventDeadline)
    Check ($line.StartsWith('data: ') -and ($line.Substring(6) | ConvertFrom-Json).type -eq 'patch') 'SSE delivers live patches after initial snapshot'
    $changed = PostJson '/api/config' @{workers=1;min_interval_ms=50;expect='changed'}
    Check ($changed.Status -eq 200 -and $changed.Body.ok) 'Configuration persists and applies'
    Check ((GetJson '/api/responses').responses[0].expect -eq 'expected') 'Historical captured expectation remains unchanged'
    $bad = PostJson '/api/config' @{workers=$true}
    Check ($bad.Status -eq 400) 'Invalid boolean worker count rejected'
    $configFile=Join-Path $install 'config.json'
    $configAttributes=[IO.File]::GetAttributes($configFile)
    try {
        [IO.File]::SetAttributes($configFile,($configAttributes -bor [IO.FileAttributes]::ReadOnly))
        $failedConfig=PostJson '/api/config' @{expect='must-not-apply'}
        Check ($failedConfig.Status -eq 500) 'Unwritable config reports storage failure'
        Check ((GetJson '/api/snapshot').stats.expect -eq 'changed') 'Failed config write does not partially apply in memory'
    } finally { [IO.File]::SetAttributes($configFile,$configAttributes) }
    $before = (GetJson '/api/snapshot').stats.stored_total
    $clear = PostJson '/api/clear' @{}
    Check ($clear.Body.history_preserved -and (GetJson '/api/snapshot').stats.stored_total -eq $before) 'Clear retains archived history'
    $stopResult = Command $exe2 @('--stop')
    Check ($stopResult.Code -eq 0) "Stop from second installation succeeds ($($stopResult.Seconds.ToString('F3')) s)"
    Check ($null -eq (Get-Process -Id $hostProcessId -ErrorAction SilentlyContinue)) 'Host exited after stop acknowledgement'
    $hostProcessId = 0
    foreach ($stream in $streams) { $stream.Dispose() };$streams.Clear()
    Check ((Command $exe @('--stop')).Code -eq 0) 'Already stopped is idempotent'
    Check ((Get-Content -LiteralPath (Join-Path $install 'config.json') -Raw | ConvertFrom-Json).custom -eq 'preserved') 'Unknown legacy config key preserved'
    $manualWorker = Command $exe @('--worker')
    Check ($manualWorker.Code -ne 0) 'Manual worker cannot bypass singleton'
    $sid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $held=Spawn $fixtureExe @('--hold-mutex',"Global\CodexDowngradedMonitor-$sid")
    try {
        Check ($held.StandardOutput.ReadLine() -eq 'ready mutex') 'Legacy-compatible user mutex held without an HTTP or pipe server'
        $unreadyStart=Command $exe $startArgs
        $unreadyStop=Command $exe @('--stop')
        Check ($unreadyStart.Code -ne 0) 'Held singleton cannot launch a second service'
        Check ($unreadyStop.Code -ne 0 -and $unreadyStop.Error -match '单例仍被占用') 'Unavailable control does not falsely report no task or successful stop'
    } finally { $held.StandardInput.Close();$held.WaitForExit(3000) | Out-Null;$held.Dispose() }
    $held=Spawn $fixtureExe @('--hold-db',(Join-Path $install 'data/monitor.sqlite'))
    try {
        Check ($held.StandardOutput.ReadLine() -eq 'ready db') 'Database write lock held during initialization'
        $initializing=Spawn $exe @('--serve','--no-open','--no-evidence','--pid','1')
        $hostProcessId=$initializing.Id
        $deadline=[DateTime]::UtcNow.AddSeconds(2)
        do {
            $state=Command $exe @('--status')
            if($state.Code -eq 0){break}
            Start-Sleep -Milliseconds 20
        } while([DateTime]::UtcNow -lt $deadline)
        Check ($state.Code -eq 0 -and ($state.Text | ConvertFrom-Json).stage -eq 'initializing') 'Control pipe responds while database initialization is blocked'
        $earlyStop=Command $exe @('--stop')
        Check ($earlyStop.Code -eq 0 -and $earlyStop.Seconds -lt 3) 'Stop works before HTTP readiness even when database is locked'
        $hostProcessId=0
    } finally { $held.StandardInput.Close();$held.WaitForExit(3000) | Out-Null;$held.Dispose() }
    $clients = @(1..6 | ForEach-Object { Spawn $exe $startArgs })
    foreach($client in $clients) { Check ($client.WaitForExit(10000) -and $client.ExitCode -eq 0) 'Concurrent launcher completed successfully' }
    $hostProcessId = (GetJson '/api/health').pid
    $allHosts = @(Get-Process CodexDowngradedMonitor -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $exe })
    Check ($allHosts.Count -le 2) 'Concurrent starts create at most one host and one worker'
    $workerProcesses = @($allHosts | Where-Object Id -ne $hostProcessId)
    $crashHost = [Diagnostics.Process]::GetProcessById($hostProcessId)
    $crashHost.Kill()
    $crashHost.WaitForExit(5000) | Out-Null
    $hostProcessId=0
    Start-Sleep -Milliseconds 200
    foreach($workerProcess in $workerProcesses) { Check ($workerProcess.HasExited) 'Job Object removes worker when host is killed' }
    $recover=Command $exe $startArgs
    Check ($recover.Code -eq 0) 'Abandoned mutex recovers after host crash'
    $hostProcessId=(GetJson '/api/health').pid
    Start-Sleep -Milliseconds 150
    $workerProcess=Get-Process CodexDowngradedMonitor -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $exe -and $_.Id -ne $hostProcessId } | Select-Object -First 1
    if ($workerProcess) { $workerProcess.Kill();$workerProcess.WaitForExit(5000) | Out-Null }
    Check ((Command $exe @('--status')).Code -eq 0) 'Control channel survives worker failure'
    Check ((Command $exe @('--stop')).Code -eq 0) 'Service stops after worker failure'
    $hostProcessId=0
    $occupied=[Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback,$port);$occupied.Start()
    try {
        $shifted=Command $exe $startArgs
        Check ($shifted.Code -eq 0) "Occupied configured port does not block startup: $($shifted.Error)"
        $shiftStatus=(Command $exe @('--status')).Text | ConvertFrom-Json
        $hostProcessId=$shiftStatus.pid
        Check ($shiftStatus.port -ne $port) 'Control pipe reports actual shifted port'
        Check ((Command $exe @('--stop')).Code -eq 0) 'Stop works while configured HTTP port belongs to another listener'
        $hostProcessId=0
    } finally { $occupied.Stop() }
    # A copied v0.4 database is opened directly, with no conversion/import step.
    $legacy=Join-Path $run 'legacy source'
    New-Item -ItemType Directory -Force (Join-Path $legacy 'data') | Out-Null
    $prepared=Command $fixtureExe @('--legacy-db',(Join-Path $legacy 'data/monitor.sqlite'))
    Check ($prepared.Code -eq 0) 'Legacy database fixture prepared'
    Copy-Item -LiteralPath (Join-Path $install 'config.json') -Destination (Join-Path $legacy 'config.json')
    $migration=Join-Path $run 'migration copy'
    New-Item -ItemType Directory -Force $migration | Out-Null
    Copy-Item -LiteralPath $exe -Destination (Join-Path $migration 'CodexDowngradedMonitor.exe')
    Copy-Item -LiteralPath (Join-Path $repo 'src/Web') -Destination $migration -Recurse
    Copy-Item -LiteralPath (Join-Path $legacy 'config.json'),(Join-Path $legacy 'data') -Destination $migration -Recurse
    $migrationExe=Join-Path $migration 'CodexDowngradedMonitor.exe'
    $copiedConfig=(Get-FileHash -LiteralPath (Join-Path $migration 'config.json')).Hash
    $started=Command $migrationExe @('--start','--no-open','--no-evidence','--pid','1')
    Check ($started.Code -eq 0) 'Copy config.json and data directory upgrades directly'
    $hostProcessId=(GetJson '/api/health').pid
    $oldSnapshot=GetJson '/api/snapshot'
    Check ($oldSnapshot.stats.data_version -eq '0.3' -and $oldSnapshot.stats.stored_total -eq 1 -and $oldSnapshot.responses[0].verdict -eq 'subtask') 'Copied historical data and classification retained'
    Check ((Get-FileHash -LiteralPath (Join-Path $migration 'config.json')).Hash -eq $copiedConfig) 'Upgrade does not rewrite config.json'
    Check ((Command $exe2 @('--stop')).Code -eq 0) 'Copied installation remains controllable from another directory'
    $hostProcessId=0
    $timings = [Collections.Generic.List[object]]::new()
    $temporary=Join-Path $run 'temporary overrides'
    New-Item -ItemType Directory -Force $temporary | Out-Null
    Copy-Item -LiteralPath $exe -Destination $temporary
    Copy-Item -LiteralPath (Join-Path $repo 'src/Web') -Destination $temporary -Recurse
    $diskConfig=Get-Content -LiteralPath (Join-Path $install 'config.json') -Raw | ConvertFrom-Json
    $diskConfig.host='localhost';$diskConfig.cpp_port=$port+1
    $diskConfig | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $temporary 'config.json') -Encoding utf8
    $temporaryExe=Join-Path $temporary 'CodexDowngradedMonitor.exe'
    $started=Command $temporaryExe ($startArgs+@('--host','127.0.0.1','--port',"$port",'--expect','temporary','--workers','3','--min-interval-ms','30'))
    Check ($started.Code -eq 0) 'Launch with temporary host, port, and scanner overrides'
    $hostProcessId=(GetJson '/api/health').pid
    $updated=PostJson '/api/config' @{workers=1}
    Check ($updated.Status -eq 200 -and $updated.Body.stats.expect -eq 'temporary' -and $updated.Body.stats.min_interval_ms -eq 30) 'Panel update preserves other effective runtime overrides'
    $savedConfig=Get-Content -LiteralPath (Join-Path $temporary 'config.json') -Raw | ConvertFrom-Json
    Check ($savedConfig.host -eq 'localhost' -and $savedConfig.cpp_port -eq $port+1) 'Temporary listener overrides never persist through panel saves'
    Check ($savedConfig.expect -eq $diskConfig.expect -and $savedConfig.min_interval_ms -eq $diskConfig.min_interval_ms -and $savedConfig.custom -eq 'preserved' -and $savedConfig.workers -eq 1) 'Only submitted panel settings persist, including unknown disk keys'
    Check ((Command $temporaryExe @('--stop')).Code -eq 0) 'Temporary listener remains controllable after panel update'
    $hostProcessId=0
    $started=Command $temporaryExe $startArgs
    Check ($started.Code -eq 0) 'Restart after panel save succeeds without CLI overrides'
    $restored=(Command $temporaryExe @('--status')).Text | ConvertFrom-Json
    $hostProcessId=$restored.pid
    $restoredSnapshot=$http.GetStringAsync($restored.url.TrimEnd('/')+'/api/snapshot').GetAwaiter().GetResult() | ConvertFrom-Json
    Check ($restored.port -eq $port+1 -and $restoredSnapshot.stats.expect -eq $diskConfig.expect -and $restoredSnapshot.stats.min_interval_ms -eq $diskConfig.min_interval_ms -and $restoredSnapshot.stats.workers -eq 1) 'Restart restores disk listener and settings with saved worker count'
    Check ((Command $temporaryExe @('--stop')).Code -eq 0) 'Restarted service stops normally'
    $hostProcessId=0
    for ($i=0; $i -lt $PerformanceRuns; $i++) {
        $startResult = Command $exe $startArgs
        if ($startResult.Code -ne 0) { throw "Performance start failed: $($startResult.Error)" }
        $hostProcessId = (GetJson '/api/health').pid
        $reuseResult = Command $exe @('--start','--no-open')
        $stopResult = Command $exe @('--stop')
        if ($reuseResult.Code -ne 0 -or $stopResult.Code -ne 0) { throw 'Performance lifecycle failure' }
        $timings.Add(@{start=$startResult.Seconds;reuse=$reuseResult.Seconds;stop=$stopResult.Seconds})
        $hostProcessId=0
    }
    $timings | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $run 'timings.json') -Encoding utf8
    foreach($key in @('start','reuse','stop')) { $sorted=@($timings | ForEach-Object { $_[$key] } | Sort-Object);if ($sorted.Count) { Write-Host "$key p95=$($sorted[[Math]::Ceiling(.95*$sorted.Count)-1]) seconds" } }
    $checks | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $run 'passed.json') -Encoding utf8
    Write-Host "Integration complete: $($checks.Count) checks; artifacts: $run"
} finally {
    foreach ($stream in $streams) { $stream.Dispose() }
    if ($hostProcessId) { $result = Command $exe @('--stop'); Write-Host "Cleanup stop: $($result.Code) $($result.Error)" }
    if ($fixture -and !$fixture.HasExited) { $fixture.StandardInput.Close(); if (!$fixture.WaitForExit(5000)) { $fixture.Kill(); $fixture.WaitForExit() } }
    $http.Dispose()
}
