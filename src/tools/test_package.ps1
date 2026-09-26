param([Parameter(Mandatory)][string]$ZipPath)
$ErrorActionPreference='Stop'
$repo=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$run=Join-Path $repo ('out/test-results/package-'+(Get-Date -Format 'yyyyMMdd-HHmmss'))
$extract=Join-Path $run '中文 空格 解压'
New-Item -ItemType Directory -Force $extract | Out-Null
Expand-Archive -LiteralPath $ZipPath -DestinationPath $extract
$install=(Get-ChildItem -LiteralPath $extract -Directory | Select-Object -First 1).FullName
$exe=Join-Path $install 'CodexDowngradedMonitor.exe'
$checks=[Collections.Generic.List[string]]::new()
function Check($condition,[string]$message){if(!$condition){throw $message};$checks.Add($message);Write-Host "PASS $message"}
function Command([string]$file,[string[]]$arguments,[switch]$Bat){
    $info=[Diagnostics.ProcessStartInfo]::new($file)
    if($Bat){$info.FileName=Join-Path $env:SystemRoot 'System32/cmd.exe';$info.Arguments='/d /s /c ""'+$file+'" '+($arguments -join ' ')+'"'}
    else{foreach($argument in $arguments){$info.ArgumentList.Add($argument)}}
    $info.UseShellExecute=$false;$info.CreateNoWindow=$true;$info.RedirectStandardOutput=$true;$info.RedirectStandardError=$true;$info.RedirectStandardInput=$true
    $info.WorkingDirectory=$run
    $info.Environment['PATH']=(Join-Path $env:SystemRoot 'System32')+';'+$env:SystemRoot
    foreach($key in @('USERPROFILE','APPDATA','LOCALAPPDATA','TEMP','TMP','HOME','CODEX_HOME')){$info.Environment[$key]=Join-Path $run "unused-$key"}
    $p=[Diagnostics.Process]::Start($info);$p.StandardInput.Close()
    $stdout=$p.StandardOutput.ReadToEndAsync();$stderr=$p.StandardError.ReadToEndAsync()
    if(!$p.WaitForExit(15000)){throw "Packaged command timed out: $file $arguments"}
    return @{Code=$p.ExitCode;Text=$stdout.GetAwaiter().GetResult();Error=$stderr.GetAwaiter().GetResult()}
}
function WaitStopped {
    $deadline=[DateTime]::UtcNow.AddSeconds(3)
    do {
        if((Command $exe @('--status')).Code -eq 1){return $true}
        Start-Sleep -Milliseconds 25
    }while([DateTime]::UtcNow -lt $deadline)
    return $false
}
$running=$false
$handler=[Net.Http.HttpClientHandler]::new();$handler.UseProxy=$false
$http=[Net.Http.HttpClient]::new($handler);$http.Timeout=[TimeSpan]::FromSeconds(5)
try {
    Check (((Get-ChildItem -LiteralPath $install | Sort-Object Name).Name -join '|') -eq 'CodexDowngradedMonitor.exe|start.bat|stop.bat|Web') 'ZIP root contains exactly one EXE, two BAT files and Web'
    Check (!(Get-ChildItem -LiteralPath $install -Recurse -File | Where-Object Extension -in @('.py','.pyd','.dll','.sqlite'))) 'ZIP contains no Python, extra DLL, or user database'
    Check (Test-Path -LiteralPath (Join-Path $install 'Web/licenses/Cargo.lock.txt')) 'Locked dependencies and third-party licenses included'
    Check ((Command $exe @('--status')).Code -eq 1) 'No service running before packaged test'
    $port=Get-Random -Minimum 30000 -Maximum 40000
    $started=Command (Join-Path $install 'start.bat') @('--no-open','--no-evidence','--pid','1','--port',"$port") -Bat
    Check ($started.Code -eq 0) "Packaged BAT starts with only Windows on PATH: $($started.Error)"
    $running=$true
    $status=(Command $exe @('--status')).Text | ConvertFrom-Json
    $base=$status.url.TrimEnd('/')
    $health=$http.GetStringAsync($base+'/api/health').GetAwaiter().GetResult() | ConvertFrom-Json
    Check ($health.backend -eq 'rust') 'Packaged HTTP service ready'
    foreach($path in @('/','/app.js','/style.css','/help.html')){
        $response=$http.GetAsync($base+$path).GetAwaiter().GetResult()
        Check ($response.IsSuccessStatusCode -and $response.Content.ReadAsByteArrayAsync().GetAwaiter().GetResult().Length -gt 0) "Packaged Web asset served: $path"
        $response.Dispose()
    }
    $traversal=$http.GetAsync($base+'/%2e%2e%5cconfig.json').GetAwaiter().GetResult()
    Check (!$traversal.IsSuccessStatusCode) 'Static route cannot escape Web directory'
    $traversal.Dispose()
    $stopped=Command (Join-Path $install 'stop.bat') @() -Bat
    Check ($stopped.Code -eq 0) "Packaged BAT stops actual host: $($stopped.Error)"
    $running=$false
    Check ($null -eq (Get-Process -Id $status.pid -ErrorAction SilentlyContinue)) 'Packaged host actually exited'
    Check ((Test-Path -LiteralPath (Join-Path $install 'config.json')) -and (Test-Path -LiteralPath (Join-Path $install 'data/monitor.sqlite'))) 'Runtime config and database stay beside EXE'
    Check (((Get-ChildItem -LiteralPath $install | Sort-Object Name).Name -join '|') -eq 'CodexDowngradedMonitor.exe|config.json|data|start.bat|stop.bat|Web') 'No other runtime files added to package root'
    $bad=Join-Path $run 'unwritable data'
    New-Item -ItemType Directory -Force $bad | Out-Null
    Copy-Item -LiteralPath $exe -Destination $bad
    [IO.File]::WriteAllText((Join-Path $bad 'data'),'blocked by fixture')
    $failed=Command (Join-Path $bad 'CodexDowngradedMonitor.exe') @('--start','--no-open','--no-evidence')
    Check ($failed.Code -ne 0) 'Unavailable portable data directory fails explicitly'
    Check (WaitStopped) 'Failed initialization releases the singleton after publishing its error'
    $linked=Join-Path $run 'junction install';$outside=Join-Path $run 'outside destination'
    New-Item -ItemType Directory -Force $linked,$outside | Out-Null
    Copy-Item -LiteralPath $exe -Destination $linked
    New-Item -ItemType Junction -Path (Join-Path $linked 'data') -Target $outside | Out-Null
    $failed=Command (Join-Path $linked 'CodexDowngradedMonitor.exe') @('--start','--no-open','--no-evidence')
    Check ($failed.Code -ne 0 -and !(Get-ChildItem -LiteralPath $outside -Force)) 'Data junction is rejected without writing outside the installation'
    foreach($key in @('USERPROFILE','APPDATA','LOCALAPPDATA','TEMP','TMP','HOME','CODEX_HOME')){
        Check (!(Test-Path -LiteralPath (Join-Path $run "unused-$key"))) "No runtime fallback to $key"
    }
    Check (WaitStopped) 'Failed startups leave no active singleton'
    $checks | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $run 'passed.json') -Encoding utf8
    Write-Host "Package validation complete: $($checks.Count) checks; artifacts: $run"
} finally {
    if($running){$result=Command $exe @('--stop');Write-Host "Cleanup: $($result.Code)"}
    $http.Dispose()
}
