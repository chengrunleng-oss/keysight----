@echo off
setlocal
set "APP_DIR=%~dp0"
set "PYTHON_EXE=%USERPROFILE%\.conda\envs\py311\python.exe"
if exist "%PYTHON_EXE%" goto run
set "PYTHON_EXE=python"
:run
cd /d "%APP_DIR%"
"%PYTHON_EXE%" main.py
if errorlevel 1 pause
