@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion

REM ==================================================
REM 配置：相对于当前 BAT 文件的项目目录
REM
REM BAT 同目录：
REM set "PY_DIR=%~dp0"
REM
REM BAT 下的 SteamTool 子目录：
set "PY_DIR=%~dp0Src/wechat_codex"
REM ==================================================

echo.
echo ========================================
echo   Python EXE 打包工具
echo ========================================
echo.
echo 项目目录：
echo %PY_DIR%
echo.

REM ==================================================
REM 检查项目目录
REM ==================================================
if not exist "%PY_DIR%" (
    echo ❌ 目录不存在：
    echo %PY_DIR%
    echo.
    pause
    exit /b 1
)

REM 切换到目标项目目录
pushd "%PY_DIR%"
if errorlevel 1 (
    echo ❌ 无法进入目录：
    echo %PY_DIR%
    pause
    exit /b 1
)

REM ==================================================
REM 搜索当前目录下所有 .py 文件
REM ==================================================
set "COUNT=0"

echo 可打包的 Python 文件：
echo.

for %%F in (*.py) do (
    set /a COUNT+=1
    set "PYFILE_!COUNT!=%%F"
    echo   [!COUNT!] %%F
)

REM ==================================================
REM 未找到 Python 文件
REM ==================================================
if !COUNT! EQU 0 (
    echo.
    echo ❌ 当前目录没有找到 .py 文件
    popd
    pause
    exit /b 1
)

echo.
echo ========================================

REM ==================================================
REM 选择要打包的 Python 文件
REM ==================================================
set "CHOICE="
set /p "CHOICE=请选择要打包的文件编号："

REM 检查是否输入
if not defined CHOICE (
    echo.
    echo ❌ 没有输入编号
    popd
    pause
    exit /b 1
)

REM 检查编号是否有效
if not defined PYFILE_%CHOICE% (
    echo.
    echo ❌ 无效的选择：%CHOICE%
    popd
    pause
    exit /b 1
)

REM ==================================================
REM 获取 Python 文件名和应用名
REM ==================================================
for %%F in ("!PYFILE_%CHOICE%!") do (
    set "PYFILE=%%~nxF"
    set "APPNAME=%%~nF"
)

set "DIST_EXE=dist\%APPNAME%.exe"

echo.
echo ========================================
echo 🔨 正在打包
echo.
echo Python：%PYFILE%
echo EXE：   %APPNAME%.exe
echo ========================================
echo.

REM ==================================================
REM 安装 / 更新打包依赖
REM ==================================================
if exist "requirements-build.txt" (
    echo 📚 正在安装打包依赖...
    echo.

    python -m pip install -r requirements-build.txt

    if errorlevel 1 goto build_failed

    echo.
) else (
    echo ⚠️ 未找到 requirements-build.txt
    echo    跳过依赖安装
    echo.
)

REM ==================================================
REM 构建 PyInstaller 参数
REM ==================================================
set "EXTRA_ARGS="

REM templates 存在才加入
if exist "templates" (
    set "EXTRA_ARGS=!EXTRA_ARGS! --add-data "templates;templates""
)

REM 图标资源存在才加入
if exist "static\no_icon.png" (
    set "EXTRA_ARGS=!EXTRA_ARGS! --add-data "static\no_icon.png;static""
)

REM ==================================================
REM PyInstaller 打包
REM ==================================================
echo 📦 正在执行 PyInstaller...
echo.

python -m PyInstaller ^
    --onefile ^
    !EXTRA_ARGS! ^
    "%PYFILE%"

if errorlevel 1 goto build_failed

REM ==================================================
REM 检查 EXE
REM ==================================================
if not exist "%DIST_EXE%" (
    echo.
    echo ❌ 打包失败
    echo    没有找到：
    echo    %DIST_EXE%
    echo.
    goto build_failed_no_command
)

REM ==================================================
REM 复制 EXE 到项目根目录
REM ==================================================
echo.
echo ✅ 打包成功：
echo %DIST_EXE%
echo.
echo 📦 正在复制到项目根目录...

copy "%DIST_EXE%" "%APPNAME%.exe" /Y >nul

if errorlevel 1 (
    echo.
    echo ❌ EXE 复制失败
    goto build_failed_no_command
)

REM ==================================================
REM 清理打包临时文件
REM ==================================================
echo 🧹 正在清理临时文件...

if exist "dist" (
    rmdir /s /q "dist"
)

if exist "build" (
    rmdir /s /q "build"
)

if exist "%APPNAME%.spec" (
    del /q "%APPNAME%.spec"
)

REM ==================================================
REM 完成
REM ==================================================
echo.
echo ========================================
echo 🎉 打包完成
echo.
echo 输出文件：
echo %PY_DIR%\%APPNAME%.exe
echo ========================================
echo.

popd
pause
exit /b 0


REM ==================================================
REM PyInstaller / pip 执行失败
REM ==================================================
:build_failed

echo.
echo ========================================
echo ❌ 依赖安装或打包命令执行失败
echo ========================================
echo.

popd
pause
exit /b 1


REM ==================================================
REM 其他构建失败
REM ==================================================
:build_failed_no_command

echo.
echo ========================================
echo ❌ 打包失败
echo ========================================
echo.

popd
pause
exit /b 1