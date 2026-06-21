@echo off
REM Launch kalikot. Prefer the standalone .exe (no Python needed); fall back to
REM running the Python source only if the exe isn't present.
cd /d "%~dp0"
if exist "kalikot.exe" (
    start "" "kalikot.exe"
) else if exist "dist\kalikot.exe" (
    start "" "dist\kalikot.exe"
) else (
    start "" pythonw "macro_gui.py"
)
