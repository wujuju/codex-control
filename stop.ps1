$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$pidFile = Join-Path $projectRoot ".runtime\bridge.pid"
$expectedExecutable = (Join-Path $projectRoot ".venv\Scripts\wechat-codex.exe")

if (-not (Test-Path -LiteralPath $pidFile)) {
    Write-Output "没有后台运行记录"
    exit 0
}

$bridgePid = [int](Get-Content -LiteralPath $pidFile -Raw)
$process = Get-Process -Id $bridgePid -ErrorAction SilentlyContinue
if (-not $process) {
    Remove-Item -LiteralPath $pidFile
    Write-Output "后台进程已经结束"
    exit 0
}

$actualPath = $process.Path
if ($actualPath -ne $expectedExecutable) {
    throw "PID $bridgePid 对应的不是本项目程序，拒绝终止：$actualPath"
}

Stop-Process -Id $bridgePid
Remove-Item -LiteralPath $pidFile
Write-Output "微信 Codex 助手已停止"

