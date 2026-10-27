@echo off
rem Launcher for the installed app. The first start downloads Python, the speech
rem model runtime and LibreTranslate; later starts work offline.
setlocal
title Live Translate
set "APP=%~dp0"

rem Never run elevated (e.g. started from the installer): an elevated uv refuses
rem the Python folder links it made as the normal user ("untrusted mount point",
rem os error 448). fltmc only succeeds as admin; explorer restarts us unelevated.
fltmc >nul 2>&1
if not errorlevel 1 (
    explorer.exe "%APP%LiveTranslate.vbs"
    exit /b 0
)

set "DATA=%LOCALAPPDATA%\LiveTranslate"
set "UV=%APP%bin\uv.exe"
set "UV_PROJECT_ENVIRONMENT=%DATA%\venv"
set "UV_PYTHON_INSTALL_DIR=%DATA%\python"
set "UV_PYTHON_PREFERENCE=only-managed"
set "UV_CACHE_DIR=%DATA%\uv-cache"
rem The CUDA/torch wheels are large; uv's default 30 s timeout is too short on slow links.
set "UV_HTTP_TIMEOUT=300"
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
        echo  Download interrupted, trying once more...
        echo.
        "%UV%" sync --frozen --no-dev --no-editable --project "%APP%."
    )
    if errorlevel 1 (
        echo.
        echo  Setup failed. Check the internet connection and start Live Translate again.
        pause
        exit /b 1
    )
    echo ok> "%MARKER%"
)

rem pythonw: no console window; the app shows itself as a tray icon and logs to
rem %LOCALAPPDATA%\LiveTranslate\livetranslate.log
start "" "%UV_PROJECT_ENVIRONMENT%\Scripts\pythonw.exe" -m livetranslate %*
