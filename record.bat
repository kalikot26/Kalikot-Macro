@echo off
REM Record a macro. Usage: record.bat [name]
set NAME=%1
if "%NAME%"=="" set NAME=macro
python "%~dp0macro.py" record %NAME%
echo.
pause
