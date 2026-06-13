@echo off
REM Claude Fleet launcher (Windows). First run creates .venv and installs deps.
cd /d "%~dp0"

if not exist .venv (
    echo [claude-fleet] creating venv...
    python -m venv .venv
)
call .venv\Scripts\activate.bat

python -c "import fastapi" 2>nul
if errorlevel 1 (
    echo [claude-fleet] installing deps...
    pip install -q -e .
)

if "%CLAUDE_FLEET_PORT%"=="" set CLAUDE_FLEET_PORT=7878
echo [claude-fleet] listening on http://127.0.0.1:%CLAUDE_FLEET_PORT%
echo [claude-fleet] Note: opening/focusing terminals is macOS-only; on Windows the
echo [claude-fleet] dashboard works and resume/fork show a command to copy + run.
python -m uvicorn app:app --host 127.0.0.1 --port %CLAUDE_FLEET_PORT% --reload
