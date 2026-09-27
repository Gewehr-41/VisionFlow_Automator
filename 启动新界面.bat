@echo off
if "%~1"=="__h__" goto :run
powershell -NoProfile -WindowStyle Hidden -Command "Start-Process -FilePath '%~f0' -ArgumentList '__h__' -WindowStyle Hidden"
exit /b

:run
cd /d "%~dp0"
python gui_pyside6.py
