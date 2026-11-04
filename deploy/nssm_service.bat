@echo off
REM 用 NSSM 把 spider-gd 注册为 Windows 服务（开机自启、崩溃自拉起）
REM 前置：下载 nssm.exe 放到 PATH 或本目录。
setlocal

set SERVICE_NAME=spider-gd
set ROOT=%~dp0..
set PYTHON=%ROOT%\.venv\Scripts\python.exe
set ENTRY=%ROOT%\main.py
set CONFIG=%ROOT%\configs\mode_d_offline.yaml

echo 注册服务 %SERVICE_NAME% ...
nssm install %SERVICE_NAME% "%PYTHON%" "%ENTRY%" --config "%CONFIG%" run
nssm set %SERVICE_NAME% AppDirectory "%ROOT%"
nssm set %SERVICE_NAME% Start SERVICE_AUTO_START
nssm set %SERVICE_NAME% AppStdout "%ROOT%\logs\service_stdout.log"
nssm set %SERVICE_NAME% AppStderr "%ROOT%\logs\service_stderr.log"

echo 启动服务 ...
nssm start %SERVICE_NAME%

echo 完成。管理命令：
echo   nssm stop %SERVICE_NAME%
echo   nssm restart %SERVICE_NAME%
echo   nssm remove %SERVICE_NAME% confirm
endlocal
