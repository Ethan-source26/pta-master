@echo off
setlocal
cd /d "%~dp0"
title PTA Theory Question Scraper

rem ---------------------------------------------------------------
rem  This launcher is intentionally ASCII-only.
rem  All Chinese messages are printed by pta_scraper.py instead,
rem  because cmd.exe mis-parses UTF-8 batch files.
rem ---------------------------------------------------------------

set "PY="
python --version >nul 2>nul
if not errorlevel 1 set "PY=python"
if not defined PY (
  py -3 --version >nul 2>nul
  if not errorlevel 1 set "PY=py -3"
)
if not defined PY goto NOPYTHON

%PY% "%~dp0pta_scraper.py" %*
if errorlevel 1 goto RUNTIMEERR
goto END

:NOPYTHON
echo.
echo   [ERROR] Python 3 was not found on this computer.
echo.
echo   Please install it first:
echo     1. Open   https://www.python.org/downloads/
echo     2. Download and run the installer
echo     3. IMPORTANT: tick the box  "Add python.exe to PATH"
echo     4. Then double-click this file again
echo.
echo   (If you only see the Microsoft Store opening, Python is
echo    not really installed yet - please install it from the
echo    link above.)
echo.
pause
goto END

:RUNTIMEERR
echo.
echo   [ERROR] The script stopped with an error.
echo   Please take a screenshot of the lines above.
echo.
pause
goto END

:END
endlocal
