@echo off
rem Launcher for the installed app. The first start downloads Python, the speech
rem model runtime and LibreTranslate; later starts work offline.
setlocal
title Live Translate
set "APP=%~dp0"
set "DATA=%LOCALAPPDATA%\LiveTranslate"
set "UV=%APP%bin\uv.exe"
set "UV_PROJECT_ENVIRONMENT=%DATA%\venv"
set "UV_PYTHON_INSTALL_DIR=%DATA%\python"
set "UV_PYTHON_PREFERENCE=only-managed"
set "UV_CACHE_DIR=%DATA%\uv-cache"
set "LIVETRANSLATE_UV=%UV%"

set "VER=dev"
if exist "%APP%VERSION" set /p VER=<"%APP%VERSION"
set "MARKER=%UV_PROJECT_ENVIRONMENT%\.installed-%VER%"

if not exist "%MARKER%" (
    echo.
    echo  Live Translate %VER% is being set up. The first start downloads about 3 GB.
    echo  Please keep this window open...
    echo.
    "%UV%" sync --frozen --no-dev --no-editable --project "%APP%."
    if errorlevel 1 (
        echo.
        echo  Setup failed. Check the internet connection and start Live Translate again.
        pause
        exit /b 1
    )
    echo ok> "%MARKER%"
)

"%UV_PROJECT_ENVIRONMENT%\Scripts\python.exe" -m livetranslate %*
if errorlevel 1 pause
