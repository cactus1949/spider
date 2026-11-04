@echo off
setlocal
cd /d "%~dp0frontend"
if not exist "node_modules" (
  echo Run pnpm install in frontend first.
  pause
  exit /b 1
)
REM Keep the window open on failure: when launched by double-click the console
REM otherwise disappears instantly ("闪退") and the real error is lost.
call pnpm exec electron-vite dev
if errorlevel 1 (
  echo.
  echo electron-vite dev exited with an error ^(see the log above^).
  echo If it says "remove ...\Temp\esbuild-...: Access is denied", run:
  echo   powershell -ExecutionPolicy Bypass -File "%~dp0tools\fix_integrity_labels.ps1"
  echo.
  pause
)
