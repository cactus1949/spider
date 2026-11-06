# 界面原型

用浏览器打开同目录的 `index.html`。没有构建步骤，也不请求外网脚本。侧栏切换画面。右上角「说明」列出本屏编号对应的改动。窗口按 1280×800 看。

数据是编的。场站和 Wi-Fi 是假的，证书日期和上传地址也是假的。

## 画面

| 文件 | 画面 | 在草案里看什么 |
|------|------|----------------|
| [01-home.png](screenshots/01-home.png) | 任务配置 | 首页仍是任务卡片。顶栏留着「仍暂停」，Wi-Fi 同时给出解码名和 netsh 原文 |
| [02-run-pause.png](screenshots/02-run-pause.png) | 运行与暂停 | 证书失败停在 `error_pause`，并给出继续和检查登录，以及打开专用 Chrome |
| [03-before-after.png](screenshots/03-before-after.png) | 前后对照 | 左栏是现在的结束后通知，右栏把成功、失败和仍暂停分开 |
| [04-task-editor.png](screenshots/04-task-editor.png) | 任务编辑 | 离屏窗口的文案，自然日怎么数，结束后走落盘、发送还是上传 |
| [05-preflight.png](screenshots/05-preflight.png) | 运行前检查 | 缺登录检查时不能运行 |
| [06-settings.png](screenshots/06-settings.png) | 设置 | 登录与场站和专网分开，发送和上传也分开。专网页含有线网卡、无线网卡和 SSID |
| [07-logs.png](screenshots/07-logs.png) | 执行日志 | 仍暂停和失败可以分开筛，已中断也单独一项 |
| [08-indicators.png](screenshots/08-indicators.png) | 指标与工坊 | 已导入步骤只在指标页改，工坊只负责录新的并导入 |

截图也在 `/opt/cursor/artifacts/`，文件名与上表相同。

## 对应优化项

条目编号来自 [product-and-ux-optimization.md](../product-and-ux-optimization.md)。

| 画面 | 优化项 |
|------|--------|
| 任务配置 | 1 定时暂停要一直看得见。2 顶栏 SSID 与路径里的 `{SSID}` 用同一次解码 |
| 运行与暂停 | 1 证书或登录弹窗失败时暂停，并给出已有的恢复动作 |
| 前后对照 | 1 定时任务的结果不要只在结束后弹一次 |
| 任务编辑 | 3 隐藏运行改成离屏仍渲染。4 发送和上传分开，并预览将上传的文件。6 日期窗口按自然日写清起止 |
| 运行前检查 | 7 运行前用浏览器状态和 `/api/health` 做检查 |
| 设置 | 2 SSID 解码失败时标红。4 设置按要办的事拆开 |
| 执行日志 | 10 先把已中断和失败分开。跨重启按步骤续跑仍不做 |
| 指标与工坊 | 5 两个入口不再都能改同一步骤 |

运行模块那一块对应 `configs/mode_*.yaml` 里的 `ModuleToggle`。现在的 `SettingsModal` 没有这组开关。草案放在设置页底部，只为和模式文件对照。

U-Key 和证书列沿用现有桌面端的叫法，专网、外网和诊断包也一样，专用 Chrome 和补数据同样沿用。原型没有接 `127.0.0.1:8800`。
