# 浏览器接管与登录态复用：实施与验收

更新：2026-10-27。本文记录本次实际实现，替代早期方案中尚未实施的设想。

## 可行性结论

可以复用用户手动登录的 Chrome。关键是连接原浏览器的原 Context 和原页面，保留 Cookie、localStorage、sessionStorage 以及页面内存，而不是导出 Cookie 后新建 Context。

不能只凭操作系统窗口或 PID，直接用 Playwright 接管任意未开放调试接口的浏览器。界面只打开持久化专用浏览器。空闲时绑定页跟随该浏览器当前可见标签；任务占用期间不改绑定。多个窗口各有一个可见标签时不猜测。

| 路径 | 使用方法 | 实施状态 |
| --- | --- | --- |
| 已开放 CDP 的 Chrome | 用户自己启动专用 Profile 的 Chrome，手动登录，然后输入本机端点 | 已实现，真实 Chrome 回归通过 |
| 软件打开持久化 Chrome | 软件用固定专用 Profile 启动系统 Chrome，用户手动登录或运行已有登录工作流 | 已实现，关闭重开后存储保留通过 |
| Chrome 当前会话授权 | Chrome 144+ 启用远程调试，提供用户数据根目录并允许连接 | 已实现 DevToolsActivePort 接入；原生授权弹窗及目标机器兼容性待验收 |

Chrome 136 起，传统 remote-debugging-port 对默认用户数据目录有限制，须使用非默认目录。Chrome 的当前会话授权能力与传统启动参数是两种不同入口。依据：[Chrome 远程调试变更](https://developer.chrome.com/blog/remote-debugging-port)、[Chrome 当前会话连接说明](https://developer.chrome.com/docs/devtools/agents/use-cases/auto-connect)、[Playwright CDP 接口](https://playwright.dev/python/docs/api/class-browsertype#browser-type-connect-over-cdp)。

复用真实会话能减少重复登录和上下文切换，但不保证无法被网站识别为自动化。服务器频率、行为、IP、账号风控、证书环境仍然存在；本实现没有关闭 Chrome 沙箱、站点隔离或加入指纹伪装。

## 推荐操作

### 方法一：软件打开专用 Chrome

1. 重启后端与前端，顶部出现全局浏览器控制栏。
2. 点击“打开专用 Chrome”，检查业务站点和登录成功元素。默认站点 `https://pm.gd.csg.cn`，成功元素 `.ant-menu-horizontal, .el-menu`（兼容门户工作台和现货页），并核对 `pfxh-token`；应按实际业务页面调整。
3. 登录方式选择“仅复用已有登录”或“由用户手动登录”，在 Chrome 完成证书、账号及验证码操作。
4. 在专用浏览器里打开要操作的标签。顶部显示“当前标签”，空闲时自动跟随，不必再从下拉框挑选。然后点击“检查登录”。需要防止账号混用时，填写企业/账号元素选择器和预期文字。
5. 运行指标单步、工作流或任务。它们使用此连接和选中页面，不再另建隔离 Context。
6. “断开连接”仅断开自动化连接；“关闭专用浏览器”只结束本次软件启动且仍持有的进程。Profile 不删除。

默认 Profile：Windows `%LOCALAPPDATA%\SpiderGD\browser-profiles\default`；macOS `~/Library/Application Support/SpiderGD/browser-profiles/default`；Linux `~/.local/share/spider-gd/browser-profiles/default`。连接配置和最近运行状态存于对应 SpiderGD 根目录的 `browser-session.json`；可用 `SPIDER_BROWSER_CONFIG` 指定文件。

专用浏览器产生的下载会另存到设置中的下载目录（`downloadDir`），未配置时使用应用的数据目录，界面连接栏显示实际路径。独立使用 SharedBrowser 且未指定目录时才回退到 Profile 下的 `downloads`。Playwright 接管的临时文件使用 UUID，断开时会清理；持久副本使用浏览器提供的建议文件名，日志记录原名与保存位置。工作流显式导出的文件继续按 yml 的 `save_as` 命名。

### 方法二：用户自己启动 Chrome

Windows PowerShell 示例（Chrome 路径按安装位置调整）：

```powershell
& "$env:ProgramFiles\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --remote-debugging-address=127.0.0.1 --user-data-dir="$env:LOCALAPPDATA\SpiderGD\manual-chrome"
```

macOS 示例：

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=9222 --remote-debugging-address=127.0.0.1 \
  --user-data-dir="$HOME/Library/Application Support/SpiderGD/manual-chrome"
```

顶部不再提供“连接已有浏览器”。请使用“打开专用浏览器”。不要让两个 Chrome 同时使用一个 Profile；普通日用 Chrome 的登录态不会自动复制到新 Profile。

若连接时提示 `/json/version` 返回 404，说明该端口有程序监听，却没有提供可用的 Chrome 调试接口。2026-10-27 在当前 macOS 上实测：9222 由普通 Google Chrome 进程占用，但 `/json/version` 和 `/json/list` 均返回 404。可直接改用软件“打开专用浏览器”；若希望继续使用日用 Chrome 中已有的登录态，按下述“当前会话授权连接”操作。仅修改输入框端口号不会让当前普通 Chrome 自动变成可接管实例。

本机端点只接受 loopback 地址，不接受远程主机和 URL 用户密码。调试端口应只对本机开放，应用后端也应保持本机部署；本次未实现完整的远程访问认证体系。

### 方法三：授权接管当前日用 Chrome（目标机试验入口）

1. 使用支持该能力的 Chrome，在 `chrome://inspect/#remote-debugging` 启用远程调试。
2. 在 `chrome://version` 查看 Profile 路径，取其用户数据根目录（去掉末尾 `Default` 或 `Profile N`）。
3. 软件选择“Chrome 144+ 当前会话授权连接”，填写上述根目录。
4. 在 Chrome 原生对话框允许连接，再选择业务页面。

软件读取该目录的 `DevToolsActivePort`，连接其浏览器 WebSocket。此路径依赖具体版本、企业策略与 Chrome 授权行为。超时或拒绝授权时不会偷偷启动替代会话；改用前两种已验证路径。没有实现 Chrome 扩展桥、Node MCP 桥或操作系统窗口注入。

当前 macOS 的例子：Profile 路径若是 `/Users/jayden/Library/Application Support/Google/Chrome/Default`，根目录就是 `/Users/jayden/Library/Application Support/Google/Chrome`。软件现在会自动识别误填的 `Default`、`Profile N` 或 `Guest Profile` 路径，并从上一级寻找端点文件。2026-10-27 已在本机实际连接 Chrome 当前会话，确认能取得原 Context；业务登录状态仍须在界面选择页面后检查。

## 运行、暂停与恢复规则

- 全局会话只允许一个任务执行；暂停也保留占用。并发任务明确失败，不插入当前页面、不静默排队。
- 用户选中的根页面保持绑定；执行前已有页面受到保护。任务中途打开的页面可在步骤结束后清理。
- 执行中禁止切换绑定页、断开或关闭浏览器。浏览器/绑定页丢失时失败，不切换到其他页面继续点击。
- 优先复用已登录状态；仅配置“优先复用，未登录自动登录”且有登录流程时自动登录。未知状态、身份不匹配或错误弹窗要求人工处理。
- `dismiss_modal` 设置 `report_error: true` 时保留错误现场、发出 `error_pause`、告警并等待，不会将该步骤立即标为完成。点击继续后检查弹窗和登录状态；仍失败则再次暂停。
- 其他普通 `pause` 步骤按其原有语义继续。所有继续操作带运行 ID 和暂停版本，拒绝陈旧操作。
- 取消为协作式停止：当前浏览器调用可能需等到返回或超时；不会杀掉用户的外部 Chrome。
- 界面流断开不代表任务取消。顶部控制栏可重新查看、处理和取消任务；后台负责最终日志更新。
- 后端重启后，旧运行标记“已中断”，不自动重放可能有副作用的步骤。重新连接、检查现场后由用户重新执行。尚未实现跨重启的精确步骤续跑。
- 前端执行请求改为 POST JSON，密码不再进入 URL 查询串；兼容旧 GET 接口仍存在，外部调用者应迁移到 POST。

## 页面请求失败、白屏与半登录

下面三块分开写。观测事实只描述当时看到的响应；推测没有被这次修复证明；已验证路径只包括模拟站和单元测试锁住的行为。真实业务站点尚未按同样步骤验收。

### 观测事实

- 现场记录里，「日前节点电价查询」相关请求出现过 HTTP 400，`Content-Type: text/html`，正文大约 6 字节。同一 URL 在另一份新打开的文档里出现过 HTTP 200。
- 白屏现场的 localStorage 有 `pfxh-userInfo`，没有 `pfxh-token`。当时刷新当前文档后页面是白的。
- 这些记录不足以说明所有 4xx/5xx 都是同一种错误，也没有把响应正文当作密钥保存。

### 推测（本次没有验证）

- 有人推测失败与文档级请求参数、前置防护或会话检查有关。把一段请求参数改到另一个接口上也会得到类似的 400，只能说明那种构造会被拒绝，不能证明正常点击日期失败的原因相同。
- 多个同域标签是否互相影响，没有被证明为必然争抢 Cookie。软件只在同一 Context 里还有未处理的同域旧标签时暂停并列出 URL，不自动关闭那些页。

### 已验证的恢复路径

- 模拟站点：同一 `doc` 刷新仍然是 400 + text/html；换了 `doc` 的新标签才 200。执行器只在错误属于上述网关形态，且检查点是 `click` + `expect: new_tab`、绑定页还在、本次新开的非保护标签能关掉、同一 Context 没有未处理的同域旧标签时，关闭那一个新标签并从该点击重放。不刷新失败文档，不关闭保护页。
- 同一检查点第二次出现应恢复的错误、总恢复次数用尽、没有安全恢复路径、错误不是上述网关形态、或状态未知：进入 `error_pause` 并 `wait_continue`。不导出，不发 `workflow_done`。用户取消才结束。继续后仍失败则再次暂停。`page_recovery: off` 只是关掉自动关页，不会把已记录错误当成成功。
- 登录阶段（`type: login`，或步骤配置了 `require_storage_keys`）的行前、暂停后和结束判定使用同一组存储键。有菜单和 `pfxh-userInfo`、没有 `pfxh-token` 时不能判已登录，点继续也不能跳过剩余登录步骤。没有配置这些键的其他页面不机械要求 token。

对应实现：

- `workflow_runner` 对每个页面挂只读观测器（`response`/`console`/`pageerror`，只记录不改写请求）。工作流成功、失败或取消后卸下这些观测器。记录包含页面 URL、资源类型、状态码、Content-Type 和响应体大小，不写响应正文。
- 只有同源 xhr/fetch 的 HTTP 400，且 Content-Type 含 `text/html` 或缺失、正文不超过 64 字节，才可能自动关页重放。`application/json` 的 400，以及 401、403、404、429、5xx，只记录并暂停诊断。
- 每个检查点最多自动重放一次，整轮最多 `max_page_recoveries`（默认 3）次。额度只限制自动恢复动作。登录步骤和 `type: login` 不靠关页重放绕过登录结果。
- 重放前若同一 Context 里除绑定页和本次要关的新标签外还有同域页面，先发 `stale_tabs_warning` 并暂停。不关闭那些页。
- 导出前若仍有未确认的网关形 400，或同源查询请求仍在途，暂停。日期算法和原有 wait 毫秒数不变。不靠“在途数量一段时间没减少”来决定可以导出。
- `navigate` 支持 `skip_if_loaded: true`：当前文档已经在目标 URL 时不重新加载。当前 `workflows/` 未配置这个键。
- `captcha` 在登录阶段要求工作台元素可见，并且 `require_storage_keys`（登录流程默认包括 `pfxh-token`）已落盘。缺键是半登录：不刷新，`error_pause`，继续后用同一规则再查。
- 登录失败提示同时读 `.ant-modal-confirm`、`.el-message-box`、`.ant-message-error`、`.el-message--error`。HTTP 200 的 HTML 或缺 Content-Type 不标成「疑似网关拦截」；只有 4xx/5xx 且为 text/html 或极短正文才这么标。JSON 标为业务响应。
- `reset_site_storage` 只清步骤指定的 `local_storage` / `session_storage` / `cookies`，并保留 `keep_keys`。Cookie 走 CDP；CDP 失败且步骤要求清 cookie 时暂停，不用清空全部存储当兜底。它不是默认自动登录链的一步。
- 诊断文件写入 URL、标题、localStorage 键名、`runDay`、状态码、Content-Type、响应体大小、控制台错误和页面异常，不写响应正文。

回归：

```bash
.venv/bin/pytest -q tests/integration/test_workflow_runner_mocksite_e2e.py tests/unit/test_workflow_login_guard.py
```

`mock_site` 的 `/pfxh/stc/stale`：同一份文档（相同 `doc`）刷新仍然 400 + text/html；只有新开标签换了 `doc` 才 200。用来复现“网关拒绝 → 关掉新标签 → 从 `expect: new_tab` 重放”。

## 实现位置和接口

- `shared_browser.py`：连接、持久化 Profile、页面绑定、业务登录探测、独占运行、暂停版本与生命周期所有权。
- `workflow_runner.py` / `task_runner.py`：复用 Context、保护既有页面、登录失败暂停及同页指标执行。
- `playwright_runner.py`：主采集器也使用共享连接。
- 全局 `BrowserSessionPanel`：打开专用浏览器、显示当前标签、登录检查、暂停恢复、取消及断开。空闲时跟随可见标签。
- API：`GET /api/browser/status`、`GET /api/browser/pages`；`POST /api/browser/connect|select|auth/check|resume|cancel|detach|close`。
- connect 字段：`mode`、`endpoint`、`profile_dir`、`auth_policy`、`target_origin`、`success_selector`、`identity_selector`、`expected_identity`。
- select：`page_id`；resume：`run_id` + `pause_version`；cancel：`run_id`。

## 验收与边界

新增真实 Chrome 集成测试覆盖：原页面内存保留；外部进程不关闭；错误弹窗阻断后续操作及人工修复恢复；原 Context 多指标执行和实际 CSV 下载；HTTP 接口连接/选页/单步执行；持久化 Profile 重开；授权端点文件接入；并发互斥、陈旧控制拒绝、重启不重放。

测试使用临时 Profile 和本机测试页面，不接触个人浏览器会话。授权端点文件测试不等同于验证 Chrome 原生授权 UI。真实广东电力站点、Windows U-Key 驱动/证书窗口、企业 Chrome 策略与账号登录态需要目标机验收。

目标机验收顺序：手动登录 → 检查身份 → 单步无副作用导航 → 单指标下载 → 多指标任务 → 人为制造证书失败确认告警和暂停 → 修复后继续 → 断开软件确认 Chrome 保留。通过后再开启定时任务。若自动登录遇到证书原生窗口，保留人工接管入口，不将原生窗口当作 DOM 元素自动点击。

运行回归：

```bash
.venv/bin/python -m pip install -r mock_site/requirements.txt
.venv/bin/pytest -q tests/integration/test_browser_attachment.py tests/test_shared_browser.py tests/integration/test_control_api.py tests/unit/test_task_runner_delivery.py tests/unit/test_workflow_error_status.py tests/unit/test_task_runner_window.py tests/integration/test_workflow_runner_mocksite_e2e.py
cd frontend
npm run typecheck
```
