$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$pidFile = Join-Path $projectRoot ".runtime\bridge.pid"
$stopRequestFile = Join-Path $projectRoot ".runtime\stop.request"
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

Set-Content -LiteralPath $stopRequestFile -Value $bridgePid -Encoding ascii
Write-Output "Graceful stop requested. Waiting for cleanup..."
$exited = $process.WaitForExit(30000)
if (-not $exited) {
    Write-Warning "Graceful stop timed out; terminating the full process tree."
    & taskkill.exe /PID $bridgePid /T /F | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to terminate process tree for PID $bridgePid"
    }
    $process.WaitForExit(5000) | Out-Null
}
Remove-Item -LiteralPath $stopRequestFile -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $pidFile -ErrorAction SilentlyContinue
Write-Output "WeChat Codex bridge stopped"
