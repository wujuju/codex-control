$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$runtimeDir = Join-Path $projectRoot ".runtime"
$executable = Join-Path $projectRoot ".venv\Scripts\wechat-codex.exe"
$pidFile = Join-Path $runtimeDir "bridge.pid"
$stdoutLog = Join-Path $runtimeDir "bridge.out.log"
$stderrLog = Join-Path $runtimeDir "bridge.err.log"

if (-not (Test-Path -LiteralPath $executable)) {
    throw "未找到程序，请先运行：.\.venv\Scripts\python.exe -m pip install -e ."
}

New-Item -ItemType Directory -Force $runtimeDir | Out-Null
if (Test-Path -LiteralPath $pidFile) {
    $oldPid = [int](Get-Content -LiteralPath $pidFile -Raw)
    $oldProcess = Get-Process -Id $oldPid -ErrorAction SilentlyContinue
    if ($oldProcess -and $oldProcess.Path -eq $executable) {
        Write-Output "程序已在运行，PID：$oldPid"
        exit 0
    }
}

$env:PYTHONUTF8 = "1"
$process = Start-Process `
    -FilePath $executable `
    -ArgumentList @("--config", (Join-Path $projectRoot "config.yaml"), "start") `
    -WorkingDirectory $projectRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput $stdoutLog `
    -RedirectStandardError $stderrLog `
    -PassThru
Set-Content -LiteralPath $pidFile -Value $process.Id -Encoding ascii
Write-Output "微信 Codex 助手已在后台启动，PID：$($process.Id)"
Write-Output "日志：$stderrLog"
