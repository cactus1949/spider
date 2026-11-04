@echo off
REM ------------------------------------------------------------
REM  spider-gd dev launcher (Electron + Python backend)
REM  Python : conda env "spider-gd" (3.11), project .venv inherits it
REM  Frontend: frontend\node_modules (pnpm install)
REM  NOTE: --noSandbox below only affects the Electron dev shell.
REM  The automation Chrome is launched by the Python backend as a
REM  normal local Chrome (default sandbox kept, see
REM  shared_browser._build_launch_args).
REM ------------------------------------------------------------
setlocal
cd /d "%~dp0frontend"

if not exist "node_modules" (
  echo [1/2] installing frontend dependencies...
  call pnpm install || goto :error
)

echo [2/2] starting: renderer http://localhost:5173  backend http://127.0.0.1:8800
call pnpm exec electron-vite dev --noSandbox
goto :eof

:error
echo.
echo Launch failed, see errors above.
pause
