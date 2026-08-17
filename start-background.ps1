$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$runtimeDir = Join-Path $projectRoot ".runtime"
$executable = Join-Path $projectRoot ".venv\Scripts\wechat-codex.exe"
$pidFile = Join-Path $runtimeDir "bridge.pid"
$stopRequestFile = Join-Path $runtimeDir "stop.request"
$stdoutLog = Join-Path $runtimeDir "bridge.out.log"
$stderrLog = Join-Path $runtimeDir "bridge.err.log"
$appLog = Join-Path $runtimeDir "bridge.log"
$configFile = Join-Path $projectRoot "config.yaml"
$quotedConfigFile = '"' + $configFile + '"'
$quotedAppLog = '"' + $appLog + '"'

if (-not (Test-Path -LiteralPath $executable)) {
    throw "Executable not found. Run: .\.venv\Scripts\python.exe -m pip install -e ."
}

New-Item -ItemType Directory -Force $runtimeDir | Out-Null
if (Test-Path -LiteralPath $pidFile) {
    $oldPid = [int](Get-Content -LiteralPath $pidFile -Raw)
    $oldProcess = Get-Process -Id $oldPid -ErrorAction SilentlyContinue
    if ($oldProcess -and $oldProcess.Path -eq $executable) {
        Write-Output "Already running. PID: $oldPid"
        exit 0
    }
}

$env:PYTHONUTF8 = "1"
& $executable --config $configFile setup
if ($LASTEXITCODE -ne 0) {
    throw "Login check failed; the background service was not started."
}

Remove-Item -LiteralPath $stopRequestFile -ErrorAction SilentlyContinue

$process = Start-Process `
    -FilePath $executable `
    -ArgumentList @(
        "--config", $quotedConfigFile,
        "--log-file", $quotedAppLog,
        "start", "--skip-login-check"
    ) `
    -WorkingDirectory $projectRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput $stdoutLog `
    -RedirectStandardError $stderrLog `
    -PassThru
Start-Sleep -Milliseconds 500
if ($process.HasExited) {
    throw "Background process exited during startup. Check: $stderrLog"
}
Set-Content -LiteralPath $pidFile -Value $process.Id -Encoding ascii
Write-Output "WeChat Codex bridge started. PID: $($process.Id)"
Write-Output "Log: $appLog"
