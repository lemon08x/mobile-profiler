@echo off
setlocal

cd /d "%~dp0"
title Mobile Profiler UI

if exist ".venv\Scripts\python.exe" (
    set "PYTHON_EXE=%CD%\.venv\Scripts\python.exe"
) else (
    where python >nul 2>&1
    if errorlevel 1 (
        echo ERROR: Python was not found.
        echo Install Python 3.10 or newer, or create .venv in this directory.
        echo.
        pause
        exit /b 2
    )
    set "PYTHON_EXE=python"
)

set "PYTHONPATH=%CD%\src;%PYTHONPATH%"

set "IOS_PYTHON_EXE="
if defined IOS_PYTHON if exist "%IOS_PYTHON%" set "IOS_PYTHON_EXE=%IOS_PYTHON%"
if not defined IOS_PYTHON_EXE if exist ".venv-ios\Scripts\python.exe" set "IOS_PYTHON_EXE=%CD%\.venv-ios\Scripts\python.exe"

echo Starting Mobile Profiler UI...
echo Dashboard port: automatically select an available local port.
if defined IOS_PYTHON_EXE echo iOS sidecar runtime: %IOS_PYTHON_EXE%
echo.
if defined IOS_PYTHON_EXE (
    "%PYTHON_EXE%" -m mobile_power_profiler --ios-python "%IOS_PYTHON_EXE%" ui --port 0 %*
) else (
    "%PYTHON_EXE%" -m mobile_power_profiler ui --port 0 %*
)
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
    echo.
    echo UI exited with code %EXIT_CODE%.
    pause
)

exit /b %EXIT_CODE%
