@echo off
title Sheet music to TAB
if "%~1"=="" (
    echo.
    echo   Drag a PDF, SVG, or scan of sheet music onto this file
    echo   and it will come back with a guitar TAB staff added.
    echo.
    pause
    exit /b 0
)
for %%F in (%*) do (
    echo.
    echo ================ %%~nxF ================
    python "%~dp0sheet2tab.py" "%%~F"
)
echo.
echo All done - the new files are in the same folder as your sheet music.
echo.
pause
