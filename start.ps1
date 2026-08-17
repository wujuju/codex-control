$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$consoleExecutable = Join-Path $projectRoot ".venv\Scripts\wechat-codex.exe"
$guiExecutable = Join-Path $projectRoot ".venv\Scripts\wechat-codex-gui.exe"
$configFile = Join-Path $projectRoot "config.yaml"

if (-not (Test-Path -LiteralPath $consoleExecutable) -or
    -not (Test-Path -LiteralPath $guiExecutable)) {
    throw "Executable not found. Run: .\.venv\Scripts\python.exe -m pip install -e ."
}

$env:PYTHONUTF8 = "1"
& $consoleExecutable --config $configFile setup
if ($LASTEXITCODE -ne 0) {
    throw "Login check failed; the GUI was not started."
}

& $guiExecutable --config $configFile --skip-login-check
