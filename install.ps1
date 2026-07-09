Set-Location $PSScriptRoot
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\pip.exe install -r requirements.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
Write-Host "Install done. Edit .env, then run run_bridge.ps1"
