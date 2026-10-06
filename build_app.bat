@echo off
rem Builds the app into build\app\CookieRunBot (CookieRunBot.exe, runs without Python)
rem and, when Inno Setup 6 is installed, the installer dist\CookieRunBot-Setup.exe
cd /d "%~dp0"

python -m pip install -r requirements.txt pyinstaller
if errorlevel 1 goto :error

python -m PyInstaller --noconfirm --clean --onedir --windowed --name CookieRunBot --icon app.ico --hidden-import main --hidden-import treasure --collect-all av --distpath build\app web.py
if errorlevel 1 goto :error

rem Files the app reads from its own folder
xcopy /e /i /y templates build\app\CookieRunBot\templates >nul
xcopy /e /i /y tool-img build\app\CookieRunBot\tool-img >nul
copy /y index.html build\app\CookieRunBot\ >nul
copy /y logo.png build\app\CookieRunBot\ >nul
copy /y box?.png build\app\CookieRunBot\ >nul

set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if exist "%ISCC%" (
    "%ISCC%" /Q installer.iss
    if errorlevel 1 goto :error
) else (
    echo Inno Setup 6 not found: skipped the installer. Install it with: winget install JRSoftware.InnoSetup
)

echo.
echo Done. The installer is dist\CookieRunBot-Setup.exe
pause
exit /b 0

:error
echo.
echo [ERROR] Build failed.
pause
exit /b 1
