@echo off
REM ============================================================
REM  Remote Management Console - ONE-CLICK LAUNCHER (LAN direct).
REM  Run this on EACH PC (both must be on the SAME network). It:
REM    * frees stale port 8000,
REM    * starts your console + agent at http://localhost:8000/,
REM    * binds to the LAN so a partner can reach this PC by IP,
REM    * prints THIS PC's IP address + password.
REM  To control a partner: open the console and enter THEIR
REM  IP address + password. Close this window to go offline.
REM ============================================================
setlocal
cd /d "%~dp0"

where python >nul 2>&1
if %errorlevel% neq 0 goto nopython

echo Installing dependencies (first run may take a minute)...
python -m pip install -r "%~dp0requirements.txt" --quiet --disable-pip-version-check

python "%~dp0server\run_all.py"

echo.
echo ============================================================
echo  The app stopped. Read any error message above this line.
echo ============================================================
pause
exit /b

:nopython
echo.
echo [ERROR] Python is not installed, or not on PATH.
echo Install it from https://www.python.org/downloads/
echo and tick "Add python.exe to PATH" during setup, then run this again.
echo.
pause
exit /b
