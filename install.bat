@echo off
setlocal
cd /d "%~dp0"

echo ===============================================
echo   kalikot - installing Python dependencies
echo ===============================================
echo.

REM --- find a Python launcher ---------------------------------------------
set "PY="
where py >nul 2>&1 && set "PY=py"
if not defined PY (
    where python >nul 2>&1 && set "PY=python"
)
if not defined PY (
    echo [ERROR] Python was not found on this PC.
    echo.
    echo   1. Install Python 3 from https://www.python.org/downloads/
    echo   2. During setup, TICK "Add Python to PATH"
    echo   3. Run this install.bat again
    echo.
    echo   (Or skip all this and just run the standalone  dist\kalikot.exe )
    echo.
    pause
    exit /b 1
)

echo Using Python launcher: %PY%
%PY% --version
echo.

REM --- install -----------------------------------------------------------
echo Upgrading pip...
%PY% -m pip install --upgrade pip
echo.
echo Installing requirements...
%PY% -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo [ERROR] Installation failed - see the messages above.
    pause
    exit /b 1
)

REM --- verify ------------------------------------------------------------
echo.
echo Verifying...
%PY% -c "import pynput, customtkinter; print('OK - pynput and customtkinter are installed')"
if errorlevel 1 (
    echo [ERROR] Verification failed.
    pause
    exit /b 1
)

echo.
echo ===============================================
echo   Done! Start the app with:  gui.bat
echo   (or just use the standalone  dist\kalikot.exe )
echo ===============================================
echo.
pause
