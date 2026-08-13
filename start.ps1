$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$executable = Join-Path $projectRoot ".venv\Scripts\wechat-codex.exe"

if (-not (Test-Path -LiteralPath $executable)) {
    throw "Executable not found. Run: .\.venv\Scripts\python.exe -m pip install -e ."
}

$env:PYTHONUTF8 = "1"
& $executable --config (Join-Path $projectRoot "config.yaml") start
