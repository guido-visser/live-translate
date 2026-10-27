@echo off
rem Start Live Translate from a source checkout on Windows (needs uv on PATH).
cd /d "%~dp0"
uv sync --frozen --no-dev || (pause & exit /b 1)
uv run --frozen --no-dev python -m livetranslate %*
if errorlevel 1 pause
