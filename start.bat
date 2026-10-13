@echo off
setlocal
py "%~dp0app.py"
if errorlevel 1 python "%~dp0app.py"
endlocal
