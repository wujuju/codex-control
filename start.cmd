@echo off
setlocal
set "PROJECT_ROOT=%~dp0"
set "PYTHONUTF8=1"

if not exist "%PROJECT_ROOT%.venv\Scripts\wechat-codex.exe" (
  echo Program not installed. Run:
  echo   .\.venv\Scripts\python.exe -m pip install -e .
  exit /b 1
)

if not exist "%PROJECT_ROOT%.venv\Scripts\wechat-codex-gui.exe" (
  echo Program not installed. Run:
  echo   .\.venv\Scripts\python.exe -m pip install -e .
  exit /b 1
)

"%PROJECT_ROOT%.venv\Scripts\wechat-codex.exe" --config "%PROJECT_ROOT%config.yaml" setup --chatgpt-only
if errorlevel 1 (
  echo ChatGPT Plus login check failed; the GUI was not started.
  exit /b 1
)

"%PROJECT_ROOT%.venv\Scripts\wechat-codex-gui.exe" --config "%PROJECT_ROOT%config.yaml" --skip-login-check
