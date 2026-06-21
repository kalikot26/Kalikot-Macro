@echo off
REM Replay a macro. Usage: play.bat [name] [extra options]
REM Example: play.bat mymacro --loops 5 --speed 2
set NAME=%1
if "%NAME%"=="" set NAME=macro
shift
set REST=
:collect
if "%1"=="" goto run
set REST=%REST% %1
shift
goto collect
:run
python "%~dp0macro.py" play %NAME% %REST%
echo.
pause
