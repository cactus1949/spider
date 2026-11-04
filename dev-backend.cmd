@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Missing .venv\Scripts\python.exe. Restore the project Python environment first.
  exit /b 1
)
REM UTF-8 mode: keeps console/emoji logging readable. Console tools (netsh /
REM powershell / wmic) still emit cp936 on zh-CN, so do NOT call
REM subprocess.run(text=True) directly -- use spider_gd.infrastructure.proc.run_capture.
set "PYTHONUTF8=1"
".venv\Scripts\python.exe" main.py --config configs/mode_dev.yaml run
