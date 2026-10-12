@echo off
REM ---------------------------------------------------------------
REM  Autonomous Trading System -- dashboard launcher
REM  Removes itself from view when started from the desktop shortcut
REM ---------------------------------------------------------------
cd /d "C:\Users\Administrateur\Documents\Default Project\tradingsys"

if not exist "state" mkdir "state"

echo ============================================
echo   AUTONOMOUS TRADING SYSTEM
echo   dashboard starting...
echo ============================================

"C:\Users\Administrateur\AppData\Local\Python\pythoncore-3.14-64\python.exe" run.py dashboard --port 8642

echo.
echo Dashboard stopped. Press any key to close.
pause >nul
