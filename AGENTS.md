# Project memory

## Notes

- 检查是否弹出错误（如数字证书连接失败） - action: dismiss_modal timeout: 8000 report_error: true 现在是状态完成 但是我期望是，点了登陆，然后弹窗失败了，直接告警 pause暂停整个
- 后端以 `PYTHONUTF8=1` 启动（dev-backend.cmd），netsh/powershell/wmic 在中文系统输出 cp936。禁止在 Windows 控制台命令上用 `subprocess.run(..., text=True)`（解码失败会把 stdout 变成 None，报“命令未返回输出”），统一用 `spider_gd.infrastructure.proc.run_capture`；自检脚本 `tools/check_wifi.py`
- WiFi 名字里带中文（任何 >=0x80 的字节）时，`netsh wlan show interfaces/networks` **不输出文本**，而是把 802.11 原始字节打成连续十六进制（`iPhone什么` → `6950686F6E65E4BB80E4B988`）；纯 ASCII 名字才原样输出。所有 SSID 都必须过 `spider_gd.infrastructure.network.decode_ssid_name` 再比较/展示/写路径，否则“当前 SSID == 目标 SSID”永远不成立。
- 沙箱（DSH/Codex 的 Windows 隔离）会把工程目录打上 **Low 完整性标签**；带 Low 标签的 exe（`esbuild.exe`、`electron.exe`…）启动后进程也是 Low，无法删除/写入 %TEMP%、%APPDATA% 里的 Medium 文件。症状：`electron-vite dev/build` 报 `[vite:esbuild-transpile] remove ...\Temp\esbuild-...: Access is denied`，或 Electron 启动即退出。修复：`powershell -ExecutionPolicy Bypass -File tools\fix_integrity_labels.ps1`（每个沙箱会话后可能复发）。
