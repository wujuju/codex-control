$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$pidFile = Join-Path $projectRoot ".runtime\bridge.pid"
$expectedExecutable = (Join-Path $projectRoot ".venv\Scripts\wechat-codex.exe")

if (-not (Test-Path -LiteralPath $pidFile)) {
    Write-Output "No background PID file"
    exit 0
}

$bridgePid = [int](Get-Content -LiteralPath $pidFile -Raw)
$process = Get-Process -Id $bridgePid -ErrorAction SilentlyContinue
if (-not $process) {
    Remove-Item -LiteralPath $pidFile
    Write-Output "Background process already exited"
    exit 0
}

$actualPath = $process.Path
if ($actualPath -ne $expectedExecutable) {
    throw "PID $bridgePid is not this project executable: $actualPath"
}

Stop-Process -Id $bridgePid
Remove-Item -LiteralPath $pidFile
Write-Output "WeCom intelligent bot bridge stopped"
