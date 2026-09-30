@echo off
chcp 65001 >nul 2>&1
setlocal
cd /d "%~dp0"

set PY=.venv\Scripts\python.exe

echo.
echo ============================================================
echo   P1 Audio Capture Self-Check
echo ============================================================
echo.

if not exist "%PY%" (
    echo [ERROR] venv not found. Please run setup.bat first.
    echo         Expected: %PY%
    echo.
    pause
    exit /b 1
)

echo [1/2] Listing audio devices...
echo ------------------------------------------------------------
"%PY%" -m src.tools.list_devices
if errorlevel 1 echo [WARN] Device listing failed, see error above.

echo.
echo [2/2] Monitoring audio level for 20 seconds...
echo        Please PLAY some audio now (music / video / Mumble).
echo ------------------------------------------------------------
"%PY%" -m src.tools.audio_monitor -d 20 -o test_audio.wav

echo.
echo ============================================================
echo   Done. If test_audio.wav was created, play it to verify.
echo ============================================================
echo.
pause
