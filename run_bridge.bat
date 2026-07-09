@echo off
setlocal
cd /d "%~dp0"
call .venv\Scripts\activate.bat
python -m codex_github_bridge
pause
