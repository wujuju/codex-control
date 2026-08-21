@echo off
chcp 65001 >nul
setlocal

set "PROJECT_ROOT=%~dp0"
set "APP_NAME=wechat-codex-gui"
set "ENTRY=%PROJECT_ROOT%src\wechat_codex_gui.py"
set "BUILD_DIR=%PROJECT_ROOT%build"
set "SPEC_FILE=%PROJECT_ROOT%%APP_NAME%.spec"
set "SPEC_DIR=%PROJECT_ROOT%."
set "OUTPUT_EXE=%PROJECT_ROOT%%APP_NAME%.exe"

if exist "%PROJECT_ROOT%.venv\Scripts\python.exe" (
    set "PYTHON=%PROJECT_ROOT%.venv\Scripts\python.exe"
) else (
    set "PYTHON=python"
)

echo.
echo ========================================
echo   微信 Codex GUI 打包
echo ========================================
echo.

if not exist "%ENTRY%" (
    echo [错误] 找不到打包入口：%ENTRY%
    goto failed
)

if not exist "%PROJECT_ROOT%config.yaml" (
    echo [错误] 找不到配置文件：%PROJECT_ROOT%config.yaml
    goto failed
)

echo [1/3] 安装/检查项目与打包依赖...
"%PYTHON%" -m pip install -e "%PROJECT_ROOT%." -r "%PROJECT_ROOT%requirements-build.txt"
if errorlevel 1 goto failed

echo.
echo [2/3] 生成单文件 EXE...
"%PYTHON%" -m PyInstaller --noconfirm --clean --onefile --windowed --name "%APP_NAME%" --paths "%PROJECT_ROOT%src" --collect-data wechat_codex --collect-all playwright --distpath "%SPEC_DIR%" --workpath "%BUILD_DIR%" --specpath "%SPEC_DIR%" "%ENTRY%"
if errorlevel 1 goto failed

if not exist "%OUTPUT_EXE%" (
    echo [错误] 未生成 %OUTPUT_EXE%
    goto failed
)

echo.
echo [3/3] 检查运行配置与图标...

if exist "%BUILD_DIR%" rmdir /S /Q "%BUILD_DIR%"
if exist "%SPEC_FILE%" del /Q "%SPEC_FILE%"

echo.
echo ========================================
echo [完成] 双击以下文件即可运行：
echo %OUTPUT_EXE%
echo.
echo EXE 会自动读取同目录的 config.yaml，无需传入参数。
echo ========================================
echo.
pause
exit /b 0

:failed
echo.
echo ========================================
echo [失败] 打包未完成，请查看上方错误信息。
echo ========================================
echo.
pause
exit /b 1
