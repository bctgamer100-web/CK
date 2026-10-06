@echo off
cd /d "%~dp0"

python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python is not installed on this computer.
    echo.
    echo Install it first, then run this file again:
    echo     winget install Python.Python.3.12
    echo or download it from https://www.python.org/downloads/
    echo ^(tick "Add python.exe to PATH" in the installer^)
    echo.
    pause
    exit /b 1
)

python -c "import cv2, numpy" >nul 2>&1
if errorlevel 1 (
    echo Installing required packages...
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] Could not install the required packages.
        pause
        exit /b 1
    )
)

python web.py
pause
