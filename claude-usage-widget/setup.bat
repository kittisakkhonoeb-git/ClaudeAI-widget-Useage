@echo off
setlocal

echo ============================================
echo  Claude Usage Widget - Setup
echo ============================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found on this PC.
    echo Please install Python 3 from https://www.python.org/downloads/
    echo During install, check "Add python.exe to PATH", then run this file again.
    pause
    exit /b 1
)

python -c "import tkinter" >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python is installed but the "tkinter" module is missing.
    echo Reinstall Python from python.org with default options ^(tkinter is included^).
    pause
    exit /b 1
)

if not exist "%USERPROFILE%\.claude\.credentials.json" (
    echo [WARNING] Claude Code login not found at:
    echo   %USERPROFILE%\.claude\.credentials.json
    echo.
    echo Please install Claude Code on this PC and run "claude" once to log in,
    echo then run this setup again. Continuing anyway...
    echo.
)

set "SCRIPT_DIR=%~dp0"
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"

echo Creating startup shortcut...
powershell -NoProfile -Command "$s = New-Object -ComObject WScript.Shell; $sc = $s.CreateShortcut('%STARTUP%\Claude Usage Widget.lnk'); $sc.TargetPath = '%SCRIPT_DIR%Start Claude Usage Widget.vbs'; $sc.WorkingDirectory = '%SCRIPT_DIR%'; $sc.Description = 'Claude Usage Widget'; $sc.Save()"

if errorlevel 1 (
    echo [ERROR] Could not create the startup shortcut.
    pause
    exit /b 1
)

echo.
echo Setup complete! The widget will start automatically every time you log in.
echo Starting it now...
start "" wscript "%SCRIPT_DIR%Start Claude Usage Widget.vbs"

echo.
echo Done. You can close this window.
pause
