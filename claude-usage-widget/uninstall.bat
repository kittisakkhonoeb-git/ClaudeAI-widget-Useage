@echo off
setlocal

set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"

echo Stopping the widget if it's running...
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*claude_usage_widget.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

echo Removing startup shortcut...
del /Q "%STARTUP%\Claude Usage Widget.lnk" >nul 2>nul

echo.
echo Done. The widget will no longer start automatically.
echo You can now delete this whole folder if you don't want it anymore.
pause
