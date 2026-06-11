@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

if not exist "venv\Scripts\python.exe" (
    echo [init] 未找到 venv，正在初始化...
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0init-venv.ps1"
    if errorlevel 1 (
        echo.
        echo 环境初始化失败。
        pause
        exit /b 1
    )
)

echo [gui] 启动 mm-qbank-gui ...
"%~dp0venv\Scripts\python.exe" -m mm_qbank.gui_app
set "exit_code=%ERRORLEVEL%"
if not "%exit_code%"=="0" (
    echo.
    echo GUI 退出，代码: %exit_code%
    pause
)
exit /b %exit_code%
