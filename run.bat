@echo off
chcp 65001 >nul 2>&1
setlocal

rem ============================================================
rem  Real-time audio translation overlay
rem  Double-click to launch. Logs go to logs\app.log
rem ============================================================

cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
    echo [ERROR] Virtual env not found: .venv\Scripts\pythonw.exe
    echo         Run setup.bat first.
    pause
    exit /b 1
)

if not exist ".env" (
    echo [WARN] .env not found. Copy .env.example to .env
    echo        and fill in DASHSCOPE_API_KEY.
    echo.
)

rem Use pythonw.exe so no console window stays open.
rem Output still goes to logs\app.log.
start "" ".venv\Scripts\pythonw.exe" -m src.app

echo Started. Hotkeys:
echo   Ctrl+Alt+H  show / hide overlay
echo   Ctrl+Alt+M  toggle click-through
echo   Ctrl+Alt+Up / Down  opacity
echo   Ctrl+Alt+R  recenter overlay
echo   Ctrl+Alt+Q  quit
echo.
echo This window will close in 5 seconds.
timeout /t 5 >nul
