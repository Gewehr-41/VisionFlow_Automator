@echo off
rem ---------------------------------------------------------------
rem  One-click packaging entry. Double-click this file.
rem  Builds the app, deploys it, and produces a CLEAN zip for users:
rem  saved presets / steps / snippets / settings / captured images
rem  are NOT included in the zip.
rem  Use --sync-data only for local debugging (it copies your data).
rem ---------------------------------------------------------------
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PYTHONIOENCODING=utf-8"
set "PY=python"
where python >nul 2>nul || set "PY=py -3"

%PY% build_package.py --zip %*
set "CODE=%ERRORLEVEL%"

echo.
echo Exit code: %CODE%   (0=ok  2=preflight failed  3=build failed  4=verify failed)
echo.
pause
exit /b %CODE%
