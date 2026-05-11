@echo off
setlocal

cd /d "%~dp0"

echo [AstrMai WebUI] Checking Python installation...
python --version >nul 2>&1
if errorlevel 1 (
    echo Python is not installed or not in PATH.
    pause
    exit /b 1
)

if not exist venv (
    echo [AstrMai WebUI] Creating virtual environment...
    python -m venv venv
)

echo [AstrMai WebUI] Activating virtual environment...
call venv\Scripts\activate

echo [AstrMai WebUI] Installing dependencies...
pip install -r requirements.txt -q

REM Load environment variables from .env if exists
if exist .env (
    for /f "tokens=1,* delims==" %%A in (.env) do (
        set "%%A=%%B"
    )
)

echo [AstrMai WebUI] Starting AstrMai WebUI server...
uvicorn backend.server:app --host 0.0.0.0 --port 8765 --workers 1

pause
