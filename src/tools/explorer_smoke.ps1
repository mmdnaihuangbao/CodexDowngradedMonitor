param([Parameter(Mandatory)][string]$InstallDirectory)
$ErrorActionPreference='Stop'
$exe=Join-Path ([IO.Path]::GetFullPath($InstallDirectory)) 'CodexDowngradedMonitor.exe'
$shell=New-Object -ComObject Shell.Application
$window=@($shell.Windows() | Where-Object { $_.FullName -like '*explorer.exe' }) | Select-Object -First 1
if(!$window){throw 'This optional desktop test requires an open Explorer window.'}
function Status {
    $info=[Diagnostics.ProcessStartInfo]::new($exe)
    $info.ArgumentList.Add('--status');$info.UseShellExecute=$false;$info.CreateNoWindow=$true;$info.RedirectStandardOutput=$true;$info.RedirectStandardError=$true
    $p=[Diagnostics.Process]::Start($info);$output=$p.StandardOutput.ReadToEndAsync()
    if(!$p.WaitForExit(3000)){throw 'Status command timed out'}
    if($p.ExitCode -eq 0){return ($output.GetAwaiter().GetResult() | ConvertFrom-Json)}
    if($p.ExitCode -ne 1){throw "Status failed: $($p.StandardError.ReadToEnd())"}
    return $null
}
$before=Status
if(!$before){throw 'Start the isolated live smoke copy before testing Explorer reuse/stop.'}
$results=[Collections.Generic.List[object]]::new()
foreach($command in @('--start --no-open','--stop','--start --no-open --port 48880','--stop')) {
    $watch=[Diagnostics.Stopwatch]::StartNew()
    $window.Document.Application.ShellExecute($exe,$command,$InstallDirectory,'open',0)
    do {
        Start-Sleep -Milliseconds 50
        $status=Status
        if($command -eq '--stop' -and !$status){break}
        if($command -ne '--stop' -and $status.stage -eq 'ready'){break}
    }while($watch.Elapsed.TotalSeconds -lt 10)
    if($watch.Elapsed.TotalSeconds -ge 10){throw "Explorer $command did not complete"}
    if($results.Count -eq 0 -and $status.pid -ne $before.pid){throw 'Explorer reuse created a different host'}
    $results.Add(@{command=$command;seconds=$watch.Elapsed.TotalSeconds;pid=$status.pid})
}
$results | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $InstallDirectory 'explorer-smoke.json') -Encoding utf8
$results | Format-Table
