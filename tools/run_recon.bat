@echo off
REM === 现场运行侦察工具 ===
REM 双击运行；与 recon.exe 放在同一目录（dist\recon\ 或独立分发包根）。
REM 若已将 chromium 复制到本目录下的 playwright-browsers/，下面的环境变量会让
REM Playwright 直接使用本地副本，无需联网安装。

setlocal
set "PLAYWRIGHT_BROWSERS_PATH=%~dp0playwright-browsers"

REM 默认目标 URL，可按需修改
set "TARGET_URL=https://pm.gd.csg.cn/"

echo === 侦察工具启动 ===
echo Chromium 路径: %PLAYWRIGHT_BROWSERS_PATH%
echo 目标 URL    : %TARGET_URL%
echo.

"%~dp0recon.exe" start --url %TARGET_URL%

echo.
echo === 任务完成，按任意键关闭窗口 ===
pause
endlocal
