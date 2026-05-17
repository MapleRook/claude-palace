@echo off
REM custodian_sweep.bat — Scheduled palace custodian sweep (cheap mode)
REM
REM Cost-minimal by construction:
REM   --lean        only auditor+verifier (Haiku) — the arbiter loop.
REM                 No Sonnet expander/structurer, no linker.
REM   --wings-file  a curated allowlist (one wing per line, # comments
REM                 ok). Do NOT sweep all 61 wings nightly — that is the
REM                 ~$1.5k/mo ceiling. Rotate this file or use --max-wings.
REM   --max-wings   hard cap on wings per run (rotation slice).
REM Respects active_lock — skips if user is in an interactive session.
REM
REM Set these paths before scheduling.

set VENV_PYTHON=C:\path\to\venv\Scripts\python.exe
set SCRIPTS=C:\path\to\claude-palace
set WINGS=%USERPROFILE%\.claude\palace\custodian_wings.txt
set LOG=%USERPROFILE%\.claude\palace\custodian_scheduled.log

echo [%date% %time%] Custodian sweep starting (lean) >> "%LOG%"

REM Check active lock first
%VENV_PYTHON% "%SCRIPTS%\active_lock.py" check >nul 2>&1
if %errorlevel% == 0 (
    echo [%date% %time%] Active session detected - skipping >> "%LOG%"
    exit /b 0
)

REM Lean arbiter sweep over the allowlist, capped. --force is safe here:
REM the active-lock check above already gated us.
%VENV_PYTHON% "%SCRIPTS%\palace_custodians.py" --lean --wings-file "%WINGS%" --max-wings 8 --budget 0.15 --verbose --force >> "%LOG%" 2>&1

REM Export palace to sync dir
%VENV_PYTHON% "%SCRIPTS%\palace_sync.py" export >> "%LOG%" 2>&1

echo [%date% %time%] Custodian sweep complete >> "%LOG%"
