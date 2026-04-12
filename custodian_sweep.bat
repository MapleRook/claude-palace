@echo off
REM custodian_sweep.bat — Scheduled palace custodian sweep
REM Runs all custodians on all wings, then exports palace state.
REM Respects active_lock — skips if user is in an interactive session.
REM
REM Set these two variables to your local paths before scheduling.

set VENV_PYTHON=C:\path\to\venv\Scripts\python.exe
set SCRIPTS=C:\path\to\claude-palace
set LOG=%USERPROFILE%\.claude\palace\custodian_scheduled.log

echo [%date% %time%] Custodian sweep starting >> "%LOG%"

REM Check active lock first
%VENV_PYTHON% "%SCRIPTS%\active_lock.py" check >nul 2>&1
if %errorlevel% == 0 (
    echo [%date% %time%] Active session detected - skipping >> "%LOG%"
    exit /b 0
)

REM Bootstrap vs steady-state: --bootstrap raises expander budget during initial population.
set EXTRA_FLAGS=

echo [%date% %time%] Mode: %EXTRA_FLAGS% >> "%LOG%"

REM Run custodian sweep on all wings
%VENV_PYTHON% "%SCRIPTS%\palace_custodians.py" --all-wings --budget 0.15 --verbose --force %EXTRA_FLAGS% >> "%LOG%" 2>&1

REM Export palace to sync dir
%VENV_PYTHON% "%SCRIPTS%\palace_sync.py" export >> "%LOG%" 2>&1

echo [%date% %time%] Custodian sweep complete >> "%LOG%"
