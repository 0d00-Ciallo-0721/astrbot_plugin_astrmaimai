@echo off
setlocal
title AstrBot Starter
color 0a

cd /d "%~dp0"
echo ======================================================
echo           AstrBot starter (system Python supported)
echo ======================================================

set "PYTHON_CMD=venv\Scripts\python.exe"
if not exist "%PYTHON_CMD%" (
    where python >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] Neither venv\Scripts\python.exe nor system python was found.
        pause
        exit /b 1
    )
    set "PYTHON_CMD=python"
    echo [INFO] venv not found; using system Python.
)

echo [1/2] Loading secrets and starting AstrBot...
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
%PYTHON_CMD% -X utf8 "scripts\astrbot_live_bootstrap.py"
set "EXIT_CODE=%ERRORLEVEL%"

echo ------------------------------------------------------
echo AstrBot exited with code %EXIT_CODE%.
pause
exit /b %EXIT_CODE%
