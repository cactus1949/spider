@echo off
REM 一键安装：创建虚拟环境并安装依赖（离线核心）
setlocal

echo [1/3] 创建虚拟环境 .venv ...
uv venv || (echo uv 不可用，回退到 python -m venv & python -m venv .venv)

echo [2/3] 安装依赖 ...
uv pip install loguru pydantic pydantic-settings pyyaml httpx openpyxl pandas apscheduler playwright

echo [3/3] 安装浏览器内核（爬虫/侦察需要）...
.venv\Scripts\python.exe -m playwright install chromium

echo.
echo 安装完成。运行：
echo   .venv\Scripts\python.exe main.py --config configs\mode_d_offline.yaml run
endlocal
