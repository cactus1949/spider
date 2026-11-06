# 产品与体验优化方向

只写当前界面和代码已经暴露出来的问题。排序看的是现场影响，以及要动的模块面，不是排期。可点击的界面草案在 [prototype/README.md](prototype/README.md)，用浏览器打开 `docs/prototype/index.html`。

`docs/15_客户交付闭环与优化清单.md` 里有一批已经落地的能力（登录熔断和版本归档，诊断包和离屏 Chrome，以及上传重试）。那份清单也写了「首页日历驾驶舱」。当前 `frontend/src/renderer/src/App.tsx` 的首页是任务卡片，没有日历视图；顶栏日历图标打开的是 `CatchupModal`（补数据）。以路由为准，不要按 15 号文档向客户演示一个不存在的首页。

## 怎么排

影响大、改动集中在展示和状态，放前面。要重做执行引擎或跨重启续跑的，放后面，并写明为什么贵。

## P0 — 现场一次失败就该让人看见

### 1. 定时任务的暂停不能只靠事后弹窗

手动跑有 `ProgressModal`，`error_pause` 会停在「需要处理」。定时跑不打开这个弹窗。`App.tsx` 只轮询触发方式为 `scheduled_auto` 的执行日志，结束后用 `ExecutionResultModal` 通知。

证书弹窗和数字证书连接失败，以及验证码失败，在 `WorkflowRunner` 里已经是暂停并告警，不是步骤成功。若这次运行迟迟不写「结束」，或者窗口没人看着，操作员会以为夜里跑完了。

建议如下。

- 顶栏在存在未处理的 `error_pause` 时给固定状态，不依赖某次 Modal 还开着。`BrowserSessionPanel` 已经能查看占用中的运行，把它和「定时任务正在等你」合成同一条，而不是两个入口各说各的。
- Electron 托盘（`frontend/src/main/tray.ts`）在暂停和专网断开时出系统通知。轮询日志适合「跑完了」，不适合「卡在证书」。
- 结果弹窗区分三种终态：成功、失败、仍暂停。暂停不要用完成态的绿。

改动面：`App.tsx` 和 `ExecutionResultModal.tsx`，以及 `BrowserSessionPanel.tsx` 和托盘。后端事件已经有了。

### 2. 顶栏 WiFi 名必须等于路径里的 `{SSID}`

`AppLayout` 每 15 秒读 `/api/network/wifi/current`。路径模板用解码后的 SSID。`decode_ssid_name` 对不像十六进制、或解出来不像中文的字符串会原样返回。

一旦界面展示的是 hex、目录却是中文（或反过来），「今晚文件去哪了」就对不上。设置里的上传 WiFi、双网卡目标 SSID 也要走同一解码，否则自动切网永远判不相等（`AGENTS.md`）。

建议：网络测试和顶栏同时给出「解码后名称」和「netsh 原文」。解码失败时标红，并禁止用这个值写入新的路径模板。任务卡片上预览真实目录（已有 `preview-path` / `preview-structure`）时，用同一次 current SSID，不要各算各的。

改动面：`AppLayout.tsx`，`SettingsModal` 的网络区，以及 `wifi_connector.py` 的返回字段（若接口今天只回一个字符串）。

### 3. 「隐藏运行」文案和真实窗口不一致

`_resolve_browser_visibility` 收到 `headless=true` 后仍启动可见的离屏窗口，用来保住 UKey 和瑞数定时器。界面如果仍写「无头 / headless」，现场会去找一个不存在的后台浏览器，或把离屏窗口当成泄漏关掉。

建议：任务和指标上的开关改成「窗口移出屏幕，仍保持渲染」。不要出现 headless 这个词。环境变量 `SPIDER_BROWSER_HEADLESS` 留在开发说明里，不要进客户设置。

改动面：调用 `ProgressModal` 的页面文案。行为可以不动。

## P1 — 日常配置成本

### 4. 设置模态承担了四份工作

`SettingsModal.tsx` 同时放站点凭据和 WiFi/双网卡/路由，也放 delivery，还有场站和数据导入导出。上传配置又在另一块 `UploadProfilesPanel`。delivery（企微/Server酱/PushPlus/本机微信）和「采集后的 HTTP 上传」是两条链路，都可以在任务结束时动文件。

操作员分不清「发到群里」和「POST 到客户接口」。任务上的 `uploadProfileId` / `autoUpload`（`TaskUploadFields.tsx`）只覆盖第二条。

建议如下。

- 设置按任务拆开：登录与场站、专网、发送、上传。每块只保留自己的测试按钮（现有 `/api/delivery/test`、`/api/upload-profiles/test-wifi`、`test-upload`）。
- 任务编辑器里写明本任务结束时会触发哪几条。两条都关时明示「只落盘」。
- 上传前列出将要 POST 的文件名。多项会打成一个 zip，版本清单不进包。这两句现在藏在 `data_uploader.py` 的模块说明里，界面上应该出现在确认步骤。

改动面以前端信息架构为主，API 可复用。

### 5. 指标有两个编辑入口

侧栏同时有「指标配置」和「录制工坊」。运行时以 SQLite `workflow_configs` 为准，仓库 `workflows/` 只是只读来源。工坊产出 zip，再导入/提交。两边都能改步骤时，后写的覆盖先写的，现场对不上「上次录制的那份」。

建议：工坊只负责产生新 YAML 并导入；导入后的修改只在指标页。指标页标明来源（导入时间、工坊 export id，`workshop_zip.read_workshop_zip_meta` 已有元数据）。不要在两个页面各做一个步骤编辑器。

### 6. 补数据和工作流日期窗口的规则要写在控件上

任务有向前/向后天数，也有周窗口和月窗口，指标还能单独改某个导出的窗口（`DateWindowSlider.tsx`，`core/dates.py`）。默认语义在代码里是自然日，不是交易日。`docs/15` 把「要不要改成交易日」留成客户确认项，界面却已经能配得很细。

建议：滑块旁用一句人话写清「从哪一天跑到哪一天」，并注明按自然日。交易日历步骤（`extract_calendar` 等）不要被理解成已经自动跳过节假日。在确认项落地前，不要再加第三套偏移。

`docs/15` 还留着：同哈希文件是否仍上传、文件名要不要带版本号。文件中心已能看 SHA256 和 `_versions`，没有一键恢复。恢复会覆盖客户正在用的表，应做成显式动作并先复制，而不是自动回滚。

### 7. 空状态把「下一步」说具体

任务列表空态已经指向「新增任务」。更常卡住的是：没有专用 Chrome，没检查登录，专网健康是红的，上传配置没测通。这四件事分散在顶栏和设置里。

建议：点「运行」前做一次本地检查清单（浏览器已连接，登录检查通过，健康接口里的专网，若勾了自动上传则 profile 有 URL）。缺哪项就停在那一项，不要等到 SSE 里才出现 `site_unreachable`。健康数据已由 `HealthIndicators` 轮询 `/api/health`，浏览器状态已有 `/api/browser/status`。

## P2 — 结构债，改完现场才不容易回退

### 8. 控制面和步骤执行器过大

`gateway/api/server.py` 把健康和工作流放在一个 `create_app` 里，任务流和网络也在，上传、工坊和浏览器会话同样在，文件超过四千行。`tools/workflow_runner.py` 同样超过四千行，`app.py` 把装配和双网卡放在一起，delivery 和定时任务也在其中，超过三千行。`pages/Tasks.tsx` 超过一千行，编辑器三步和卡片列表耦在同一文件。

登录暂停和上传打包，以及离屏窗口的规则已经分叉在 server、runner 和 Modal 三处，下一次只改一处就会出现「界面说暂停了、日志说成功」。

建议按现有 URL 前缀拆路由模块（`/api/browser` 和 `/api/network`，`/api/workflows` 和 `/api/tasks`，以及上传），步骤 `action` 拆成单独处理器，由 `exec_step` 派发。行为测试已覆盖 mock site 与工作流（`tests/integration/test_workflow_runner_mocksite_e2e.py`、`test_workflow_data_only_input.py`），拆分时以这些测试为网。

### 9. 端点编排器不要在桌面模式里静默开跑

`CrawlOrchestrator` 适合「已知 HTTP、用 mock 或 HAR 回放」的开发链路。`endpoints.yaml` 仍是 localhost mock。`register_jobs` 只要配置了 `crawl_cron`，就会对全部端点跑昨天。

桌面客户的任务来自 `task_configs`。两套调度同时存在时，日志里会出现操作员没在任务页配过的任务名。

建议：`mode_dev` / 交付配置默认不启用 `crawl_cron`，除非端点 URL 明确不是 mock。任务页不必暴露这条链路。保留 CLI `python main.py crawl` 给定时 HTTP 采集。

### 10. 跨重启续跑先别做

`BROWSER_SESSION.md` 写明：后端重启后旧运行标已中断，不重放可能重复点击、重复导出的步骤。Scrapling 一类框架的 `crawldir` 续的是 URL 队列，对不上「第 N 步已经提交了导出」。

若要做，只续「尚未发出导出/提交」的步骤，并且续跑前强制重新检查登录。这是产品决策，不是把暂停点存进 SQLite 就结束。在此之前，执行日志把中断和失败分成两个筛选（`ExecutionLogs.tsx` 已有触发方式筛选，可加结果状态）即可。

### 11. 文档地图收束

`docs/00`、`docs/05` 的目录和模式 C 会把新人和实施带偏。本目录 `architecture.md` 描述的是现在的进程和接口。后续改路由或步骤类型时改那一篇，不要再平行维护一份「目标目录」。

## 不建议现在做的

- 用通用爬虫框架换掉 `WorkflowRunner`。步骤语义（证书弹窗、交易日历周、瑞数令牌、UKey 本机端口）是这个产品本身。见 [scrapling-research.md](scrapling-research.md)。
- 把真 headless Chromium 当成客户默认。控制接口已经为了 UKey 放弃这条路。
- 再加一条告警通道配置。`docs/15` 已把运行时通知收成 delivery 出口，并保留 outbox。新通道应进同一出口，而不是恢复旧的 `alertChannelsJson`。
