@echo off
REM === 构建侦察工具 exe ===
REM 在 tools/ 目录执行；要求当前 Python 环境已安装 pyinstaller / playwright / spider_gd 依赖。

setlocal
cd /d "%~dp0.."

echo.
echo === [1/3] PyInstaller 打包 (onedir) ===
pyinstaller tools\recon.spec --noconfirm
if errorlevel 1 (
    echo PyInstaller 打包失败
    pause
    exit /b 1
)

echo.
echo === [2/3] 安装 Playwright Chromium ===
playwright install chromium
if errorlevel 1 (
    echo Playwright chromium 安装失败（可手动重试 ^"playwright install chromium^"）
    pause
    exit /b 1
)

echo.
echo === [3/3] 复制 Chromium 到 dist\recon\playwright-browsers ===
if exist "%LOCALAPPDATA%\ms-playwright" (
    if not exist "dist\recon\playwright-browsers" mkdir "dist\recon\playwright-browsers"
    xcopy /E /I /Y "%LOCALAPPDATA%\ms-playwright\*" "dist\recon\playwright-browsers\" >nul
    echo Chromium 已复制至 dist\recon\playwright-browsers
) else (
    echo 未找到 %%LOCALAPPDATA%%\ms-playwright，跳过复制
    echo 现场可手动放置 chromium 到 dist\recon\playwright-browsers\ 下
    echo 或在 run_recon.bat 中设置 PLAYWRIGHT_BROWSERS_PATH 指向已有路径
)

echo.
echo === 构建完成 ===
echo 产物目录：dist\recon\
echo 入口 exe：dist\recon\recon.exe
echo 现场运行：双击 dist\recon\run_recon.bat
echo.
pause
endlocal
