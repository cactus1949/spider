# GPT 协作项目熟悉与优化建议

> 生成时间：2026-06-29  
> 目标：让后续 GPT / 开发者快速接手本项目，并围绕“客户好用、稳定持续运行”形成优先级明确的改进路线。

## 1. 一句话定位

`spider-gd` 是一个面向广东电力交易平台的数据采集桌面系统：Python 后端负责登录、RPA/API 采集、调度、落地、告警、健康检查；Electron + React 前端负责客户配置、手动运行、执行进度、文件管理、录制工坊和设置。

项目当前不是单纯爬虫脚本，而是“采集后端服务 + 桌面管理端 + 工作流录制/执行器 + 运维监控”的组合产品。

## 2. 当前系统地图

### 后端主链路

- `main.py`：本地开发入口，注入 `src` 后委托 `spider_gd.__main__`。
- `backend_entry.py`：PyInstaller 打包入口，负责修复编码、解包配置、设置 Playwright 浏览器路径、加载默认配置。
- `src/spider_gd/__main__.py`：CLI 命令入口，包含 `run / crawl / network / schedule / alert / health / agent`。
- `src/spider_gd/app.py`：依赖注入和常驻运行入口，装配 SQLite、落地、调度、告警、控制服务、Agent、Web UI。
- `src/spider_gd/gateway/api/server.py`：FastAPI 控制面，当前承担 REST、SSE、工作流 CRUD、任务配置、执行日志、设置、网络路由、数据导入导出、录制工坊、共享浏览器等大量职责。

### 核心采集能力

- `src/spider_gd/core/crawler/crawl_orchestrator.py`：API 采集状态机，按 `task_items` 粒度断点续采、部分失败聚合、失败告警。
- `src/spider_gd/core/crawler/api_fetcher.py`：配置驱动 API 采集，支持 412 触发会话续期，支持 `curl_cffi` TLS 指纹伪装。
- `src/spider_gd/core/crawler/session_keeper.py`：会话续期与重登。
- `tools/workflow_runner.py`：YAML 工作流驱动的 Playwright/CDP RPA 执行器。
- `tools/task_runner.py`：任务级执行器，登录一次后共享浏览器顺序执行多个指标工作流。

### 数据、调度、告警、健康

- `src/spider_gd/infrastructure/storage/database.py`：SQLite 状态库，存任务、工作流配置、任务配置、执行日志、应用设置、录制工坊等元数据。
- `src/spider_gd/core/pipeline/local_landing.py`：本地目录分层、Excel 台账、成功任务压缩包。
- `src/spider_gd/core/scheduler/task_scheduler.py`：APScheduler 包装。
- `src/spider_gd/gateway/alert/alert_gateway.py`：多通道告警路由与静默时段。
- `src/spider_gd/infrastructure/monitor/health_checker.py`：磁盘、U-Key、专网、公网健康检查。

### 前端桌面端

- `frontend/src/main/backend.ts`：Electron 主进程启动/监控后端服务，最多重启 3 次。
- `frontend/src/renderer/src/App.tsx`：页面路由与更新提示。
- `frontend/src/renderer/src/layouts/AppLayout.tsx`：主布局、健康状态、设置入口。
- `frontend/src/renderer/src/pages/Home.tsx`：今日采集任务概览。
- `frontend/src/renderer/src/pages/Indicators.tsx`：指标/工作流配置与单步运行。
- `frontend/src/renderer/src/pages/Tasks.tsx`：任务配置与手动执行。
- `frontend/src/renderer/src/pages/ExecutionLogs.tsx`：执行日志。
- `frontend/src/renderer/src/pages/RecordWorkshop.tsx`：录制工坊。
- `frontend/src/renderer/src/services/api.ts`：前端 API 封装和设置缓存。

## 3. 运行与发布路径

### 开发运行

- 后端：`python main.py --config configs/mode_d_offline.yaml run`
- 单次采集 dry-run：`python main.py --config configs/mode_d_offline.yaml crawl --dry-run`
- 前端：`cd frontend && npm run dev` 或 `pnpm dev`

### 打包发布

- `python build_backend.py --with-browsers`：打包后端和 Playwright Chromium。
- `cd frontend && pnpm build:win`：Electron Windows 安装包。
- `release.sh`：构建后上传到更新服务器。

注意：`build_backend.py` 不能跨平台产出 Windows exe。Windows 客户包必须在 Windows 环境构建。

## 4. 本次验证结果

### 通过

- `python -m pytest -m "not slow"`
- 结果：`230 passed, 1 deselected, 1 warning in 62.77s`

说明：Python 后端测试基线较好，核心状态机、API、调度、告警、存储、健康检查、工作流类型等都有覆盖。

### 未通过

- `cd frontend && npm run typecheck`
- 结果：失败在 `frontend/src/main/files.ts:152`
- 原因：`dialog:pick-file` 异常分支返回 `{ path, error }`，但类型要求 `{ path, content, error? }`，缺少 `content` 字段。

建议先补齐该类型错误，再运行一次完整前端 typecheck，因为当前检查会在首个错误处停止。

## 5. 面向客户使用的主要风险

### P0：错误弹窗处理会被误判为完成

项目记忆中已有明确预期：

> 检查是否弹出错误（如数字证书连接失败） - `action: dismiss_modal timeout: 8000 report_error: true` 现在是状态完成，但是期望是点了登录后弹窗失败，直接告警并 pause 暂停整个。

本次阅读确认该问题存在结构性原因：

- `tools/workflow_runner.py` 的 `_do_dismiss_modal()` 发现错误弹窗后只发 `waiting_input`，随后继续执行，不抛错。
- `_execute_steps()` 捕获步骤异常后会发 `step_error` 和 `aborted`，但最后仍会发 `workflow_done`。
- `tools/task_runner.py` 调用 `WorkflowRunner._execute_steps()` 后，不会检查是否发生过 `aborted/step_error`，因此可能继续标记登录或指标为 `done`。
- `gateway/api/server.py` 里的 `/run/continue` 命名是“继续”，但当前实现把 `cont_result` 置为 `False`，与 `WorkflowRunner._do_pause()` 中 `False = 停止/跳过` 的语义冲突。

建议立即建立统一契约：

- `dismiss_modal + report_error=true` 应视为 `error_pause`，不是普通 `pause`。
- 检测到证书/PIN/连接失败弹窗时：发告警、写执行日志、进入 paused/failed 状态，并阻止后续步骤被标记成功。
- `workflow_done` 只能在未发生 `aborted / step_error / task_error` 时发送或被解释为 success。
- `/run/continue` 返回 `True`；`/run/stop` 返回 `False`。按钮语义和后端布尔语义必须一致。

### P0：敏感配置进入代码/默认配置

历史版本曾在 `configs/common.yaml` 和 `frontend/src/renderer/src/components/SettingsModal.tsx` 中暴露 ServerChan `send_key` / openid 等真实配置痕迹。客户交付前应确认这类凭据只存在于 `.env` 或 SQLite 设置，并在仓库中只保留占位示例。

建议：

- 新增 `.env.example` 或完善现有模板。
- `configs/common.yaml` 只保留空通道或示例占位。
- 启动时如果告警未配置，前端显示“未配置告警”，而不是内置个人 key。

### P1：FastAPI 控制面单文件过大

`src/spider_gd/gateway/api/server.py` 当前约 2378 行，包含很多不属于同一边界的职责。后续每次改动都容易互相影响，尤其是 SSE、工作流、文件、设置、网络路由、数据备份等。

建议拆分为：

- `routes/health.py`
- `routes/tasks.py`
- `routes/workflows.py`
- `routes/run_stream.py`
- `routes/settings.py`
- `routes/network.py`
- `routes/files.py`
- `routes/workshop.py`

先只做“移动代码 + 保持接口不变”的机械拆分，降低维护成本。

### P1：前端 API 命名冲突

`frontend/src/renderer/src/services/api.ts` 中存在两个 `listTasks`：

- 一个用于 `/api/tasks?limit=...`，返回采集任务历史。
- 一个用于 `/api/tasks/list`，返回任务 YAML/配置列表。

后者会覆盖前者，导致 `Home.tsx` 的 `api.listTasks(30)` 可能拿到错误结构，首页任务概览不稳定。

建议重命名：

- `listTaskRuns(limit)`：采集执行历史。
- `listTaskTemplates()` 或 `listLegacyTaskFiles()`：任务配置文件列表。

### P1：执行状态模型需要收敛

现在同时存在多套状态：

- API 采集：`running / completed / failed / partial`
- 工作流 SSE：`workflow_done / workflow_error / aborted / waiting_input`
- 任务 SSE：`task_done / task_error / task_step`
- 前端进度：`pending / running / done / error`
- 执行日志：`running / success / failed`

建议统一出一个小型状态机：

- `running`
- `paused`
- `success`
- `failed`
- `aborted`
- `partial`

所有事件先归一化为状态，再写 SQLite、告警和前端显示。

### P1：客户侧可观测性还不够

已有执行日志、健康检查、告警、日志目录入口，但还缺少客户能直接理解的“下一步怎么处理”。

建议：

- 错误弹窗告警中加入“建议动作”：检查 U-Key、证书 PIN、网络、证书客户端是否启动。
- 首页加入“最近一次失败原因”和“重试/打开日志/打开下载目录”。
- 健康检查增加“证书有效期”和“浏览器可启动性”。
- 调度任务失败后自动创建“待处理事件”，避免只沉在日志里。

## 6. 稳定持续运行建议

### 短期：1-3 天

1. 修复 `dismiss_modal(report_error=true)` 的失败传播：错误弹窗必须告警、暂停/失败，不得最终 success。
2. 修复 `/run/continue` 和 `/run/stop` 的布尔语义。
3. 修复 `frontend/src/main/files.ts` typecheck 错误。
4. 重命名前端 `api.listTasks` 冲突。
5. 移除默认配置中的真实推送 key / openid。
6. 为“证书连接失败弹窗”补单元测试或集成测试：断言最终执行日志为 `failed/paused`，且发送告警。

### 中期：1-2 周

1. 拆分 `gateway/api/server.py` 路由模块。
2. 抽出统一 `RunState` / `RunEvent`，让 WorkflowRunner、TaskRunner、SSE、执行日志共用。
3. 前端首页改成客户运维视角：今日任务、异常处理、最近文件、健康状态在一个闭环里。
4. 设置页把凭据存储、告警测试、连接测试打通；敏感字段默认不回显明文。
5. 发布流程加入固定门禁：Python tests、frontend typecheck、后端 dry-run、打包 smoke test。

### 长期：1 个月+

1. 将工作流运行器从 `tools/` 逐步迁入 `src/spider_gd/core/workflow/`，明确产品代码和辅助工具边界。
2. RPA 工作流加入版本号、兼容性检测、选择器健康检查。
3. 采集数据标准化阶段 2.5：基于真实 Excel 样本建立字段映射、空值校验、日期连续性、重复文件校验。
4. 增加现场诊断包：一键导出配置、健康检查、最近日志、执行日志、网络路由、浏览器启动结果。
5. 引入最小权限控制：本地 API 不应长期 `allow_origins=["*"]`，生产版建议绑定本机和随机 token。

## 7. 给后续 GPT 的协作提示

如果让 GPT 继续改本项目，建议每次任务先提供这几个上下文：

1. 目标是“客户桌面采集系统”，不是一次性脚本。
2. 优先保证运行状态准确：失败不能显示完成。
3. 修改工作流运行时，必须同时看：
   - `tools/workflow_runner.py`
   - `tools/task_runner.py`
   - `src/spider_gd/gateway/api/server.py`
   - `frontend/src/renderer/src/components/ProgressModal.tsx`
   - `src/spider_gd/infrastructure/storage/database.py`
4. 修改前端任务/首页时，注意 `api.ts` 的命名和返回结构。
5. 修改配置/告警时，先检查是否有敏感信息硬编码。
6. 修改发布链路时，必须同时验证 `build_backend.py`、`frontend/electron-builder.yml`、`release.sh`。

推荐的后续提示词：

```text
请基于 docs/11_GPT协作项目熟悉与优化建议.md，优先修复 P0：
当工作流登录后出现数字证书/PIN/证书连接失败弹窗时，
dismiss_modal(report_error=true) 必须发送告警、暂停或失败整个任务，
不能把步骤/工作流/任务标记为完成。
请补测试并运行 Python 测试与前端 typecheck。
```

## 8. 当前结论

项目后端核心测试基线扎实，架构也有清晰的 Protocol / 配置 / 离线底座思路。真正影响客户信任的不是“能不能跑一次”，而是异常状态是否准确、告警是否及时、配置是否安全、前端是否把复杂性藏起来。

下一步最值得做的是 P0 修复：把“证书错误弹窗”从普通等待事件升级为明确失败/暂停状态，并让告警、执行日志、前端进度三者一致。
