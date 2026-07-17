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
set "IOS_PYTHON_REJECTED="
if defined IOS_PYTHON if exist "%IOS_PYTHON%" call :try_ios_python "%IOS_PYTHON%"
if not defined IOS_PYTHON_EXE if exist ".venv-ios313\Scripts\python.exe" call :try_ios_python "%CD%\.venv-ios313\Scripts\python.exe"
if not defined IOS_PYTHON_EXE if exist "%LOCALAPPDATA%\mobile-profiler\ios-python313\Scripts\python.exe" call :try_ios_python "%LOCALAPPDATA%\mobile-profiler\ios-python313\Scripts\python.exe"
if not defined IOS_PYTHON_EXE if exist ".venv-ios\Scripts\python.exe" call :try_ios_python "%CD%\.venv-ios\Scripts\python.exe"

if not defined IOS_PYTHON_EXE if defined IOS_PYTHON_REJECTED (
    echo WARNING: Ignoring incompatible iOS sidecar: %IOS_PYTHON_REJECTED%
    echo iOS 18.2 or newer requires official CPython 3.13+, pymobiledevice3 9.34.0,
    echo and the compatible pmd-pytcp 0.0.6 API. Android and HarmonyOS remain available.
    echo.
)

echo Starting Mobile Profiler UI...
echo Dashboard port: automatically select an available local port.
if defined IOS_PYTHON_EXE echo iOS sidecar runtime: %IOS_PYTHON_EXE%
echo.
if defined IOS_PYTHON_EXE (
    "%PYTHON_EXE%" -m mobile_profiler --ios-python "%IOS_PYTHON_EXE%" ui --port 0 %*
) else (
    "%PYTHON_EXE%" -m mobile_profiler ui --port 0 %*
)
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
    echo.
    echo UI exited with code %EXIT_CODE%.
    pause
)

exit /b %EXIT_CODE%

:try_ios_python
"%~1" -c "import inspect, ssl; from pymobiledevice3.remote import userspace_tunnel; raise SystemExit(0 if callable(getattr(ssl.SSLContext, 'set_psk_client_callback', None)) and not inspect.iscoroutinefunction(userspace_tunnel.stack.start) else 1)" >nul 2>&1
if errorlevel 1 (
    set "IOS_PYTHON_REJECTED=%~1"
) else (
    set "IOS_PYTHON_EXE=%~1"
)
exit /b 0
