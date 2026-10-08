@echo off
rem ---------------------------------------------------------------
rem  One-click packaging entry. Double-click this file.
rem  All messages (Chinese) come from build_package.py, which is
rem  UTF-8; chcp 65001 + PYTHONIOENCODING keep them readable.
rem ---------------------------------------------------------------
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PYTHONIOENCODING=utf-8"
set "PY=python"
where python >nul 2>nul || set "PY=py -3"

%PY% build_package.py %*
set "CODE=%ERRORLEVEL%"

echo.
echo Exit code: %CODE%   (0=ok  2=preflight failed  3=build failed  4=verify failed)
echo.
pause
exit /b %CODE%
