@echo off
chcp 65001 >nul 2>&1
setlocal
cd /d "%~dp0"

set PY=C:\Users\yyyhf\AppData\Roaming\uv\python\cpython-3.12.14-windows-x86_64-none\python.exe
set VENV=%~dp0.venv

echo.
echo ============================================================
echo   Setup - Real-time Audio Translator
echo ============================================================
echo.

echo [1/3] Checking Python 3.12...
if not exist "%PY%" (
    echo [ERROR] Python 3.12 not found at:
    echo         %PY%
    echo.
    pause
    exit /b 1
)
"%PY%" --version

echo.
echo [2/3] Creating virtual environment...
if exist "%VENV%\Scripts\python.exe" (
    echo        Already exists, skipping.
) else (
    "%PY%" -m venv "%VENV%"
    if errorlevel 1 (
        echo [ERROR] Failed to create venv.
        pause
        exit /b 1
    )
    echo        Created.
)

echo.
echo [3/3] Installing dependencies...
"%VENV%\Scripts\python.exe" -m pip install --upgrade pip -q
"%VENV%\Scripts\python.exe" -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 (
    echo [ERROR] Dependency installation failed.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   Setup complete!
echo.
echo   Next steps:
echo     1. List audio devices:
echo        .venv\Scripts\python.exe -m src.tools.list_devices
echo     2. Test capture (play audio, watch the level bar):
echo        .venv\Scripts\python.exe -m src.tools.audio_monitor -d 20
echo ============================================================
echo.
pause
