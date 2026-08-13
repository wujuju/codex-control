$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $python)) {
    throw "未找到 .venv，请先运行：python -m venv .venv；.\.venv\Scripts\python.exe -m pip install -e ."
}

$env:PYTHONUTF8 = "1"
& $python -m wechat_codex --config (Join-Path $projectRoot "config.yaml") start
