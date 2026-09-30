@echo off
chcp 65001 >nul 2>&1
setlocal

rem ============================================================
rem  Build the single-file exe (PyInstaller).
rem
rem  Output : dist\EVE-Voice-Translator.exe
rem  Secrets: the .env file (API key) is NEVER bundled.
rem           Users paste the key into the settings window;
rem           it is written to .env NEXT TO THE EXE.
rem
rem  Double-click this file, or run it from a terminal.
rem ============================================================

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Virtual env not found: .venv\Scripts\python.exe
    echo         Run setup.bat first.
    pause
    exit /b 1
)

echo [1/4] Checking PyInstaller...
".venv\Scripts\python.exe" -c "import PyInstaller" 2>nul
if errorlevel 1 (
    echo       Not installed. Installing now...
    ".venv\Scripts\python.exe" -m pip install pyinstaller
    if errorlevel 1 (
        echo [ERROR] pip install pyinstaller failed.
        pause
        exit /b 1
    )
)

echo [2/4] Generating app icon...
".venv\Scripts\python.exe" -m src.tools.make_app_icon

echo [3/4] Building single-file exe (this takes a few minutes)...
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean trans.spec
if errorlevel 1 (
    echo [ERROR] Build failed. See the log above.
    pause
    exit /b 1
)

echo [4/4] Verifying bundle: no secrets, size, resources...
".venv\Scripts\python.exe" -m src.tools.verify_bundle "dist\EVE-Voice-Translator.exe"
if errorlevel 1 (
    echo.
    echo [ERROR] Verification FAILED. Do not ship this build.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo  DONE
echo.
echo  File: dist\EVE-Voice-Translator.exe
echo.
echo  How to use:
echo    1. Copy the exe anywhere you like (Desktop is fine).
echo    2. Double-click it. A settings window opens automatically.
echo    3. Paste your DASHSCOPE API key, click "Save and Apply",
echo       then let it restart.
echo.
echo  The key is stored in .env NEXT TO THE EXE - never inside it.
echo ============================================================
pause
