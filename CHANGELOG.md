# Changelog

本项目所有重要变更记录。格式基于 [Keep a Changelog](https://keepachangelog.com/)。

## [Unreleased]
### Added
- 项目地图（`docs/00_项目地图.md`）与变更日志维护文档

## [0.4.0] - 2026-06-23
### Added — 阶段 1：端到端采集骨架
- `crawl_orchestrator.py`：状态机编排器（task_items 粒度断点续采+部分失败聚合）
- `session_keeper.py`：412 自动重登封装（refresh→完整重登→终止）
- `task_lock.py`：跨进程文件锁（O_EXCL 原子创建+6h 过期回收）
- `configs/endpoints.yaml`：单文件端点配置（mock_site 端点）
- CLI `crawl` 子命令真实接入（--task/--date/--resume/--dry-run）
- E2E 集成测试覆盖 412 重登/部分失败/断点续采三分支

### Added — 阶段 2：数据管道与运行机制
- `data_processor.py` 扩充：Excel 解析+校验+原始 DataFrame 落地
- `backfill.py`：自动补漏规划（防雪崩三限制：单次限额+冷却+低优先级）
- `rpa_fetcher.py`：RPA 兜底引擎（API 失败→浏览器仿真导出）
- `retention.py`：保留期自动清理（按日期目录+空月清除）
- 调度接线：每日两次定时+每小时补漏+凌晨保留期清理
- 告警闭环：item 终止失败即时告警+任务收尾按 partial/failed 升级

### Added — 阶段 3：控制面与交互
- `gateway/api/server.py`：FastAPI REST（4 端点）+暗色 Web 仪表盘
- `control_service.py`：REST+Agent 共用统一控制服务
- `handlers.py`：3 个真实 Agent handler（trigger_crawl/health_check/query_result）
- `stream_client.py`：Webhook 回传完整+Stream 占位
- Web UI：暗色控制台（健康/任务/手动触发），web_ui 开关驱动
- 5 个 CLI 子命令（network/schedule/alert/health/agent）真实逻辑
- `docs/08_远程控制外部依赖调研.md`：钉钉 Stream/OpenClaw 排雷

### Added — 阶段 4：全局运行机制
- `ukey_manager.py`：U-Key 插入自登录+有效期记录+续期/过期告警
- `supervisor.py`：双网卡持续监控+专网自动重连+公网失败降级模式 D
- 废弃双机模式 C，assemble 按 ModuleToggle 条件装配

### Fixed — 代码审查修复
- [Critical] 编排器 `_process_item` 异常捕获遗漏：DataQualityError 不再中断整个任务
- [High] `database.py` 线程安全：写操作加 `threading.Lock`
- [High] `database.py` SQL f-string 消除：改为参数化 CASE WHEN
- [High] `supervisor.py` 公网降级阈值不对称：引入 `public_degrade_after=2`
- [Low] finalize 全失败时跳过 zip 压缩
- [Low] `zip_path` 全链路判空防护（Protocol→实现→消费方）

### Changed
- SQLite `check_same_thread=False`（REST/调度多线程访问所需）
- `interfaces/storage.py` `compress_task` 返回类型 `str` → `str | None`

## [0.3.0] - 2026-06-22
### Added — 侦察工具集
- `tools/recon_tool/`：完整 4 步侦察流程（网络+浏览器+分析+打包）
- HAR 录制/回放/分析+doctor 自检+PyInstaller 打包
- `mock_site/`：模拟广东电力交易平台（登录/验证码/多页面/导出 API）

### Added — 核心模块
- Protocol 接口层（7 个 Protocol+14 个 dataclass+3 个 Enum）
- 配置加载（pydantic-settings+多 YAML 继承+.env）
- 爬虫核心引擎（LoginFlow/ApiFetcher/CaptchaSolver/SessionProvider/PlaywrightRunner）
- 网络自动配置（USB 监控/DNS 三级回退/路由配置/连通性验证/诊断）
- 调度器（APScheduler+指数退避重试）
- 本地落地（分层目录+台账 Excel+zip）
- SQLite 状态库（WAL 模式）
- 告警网关（钉钉 HMAC 加签+静默时段）
- Agent 网关（指令解析+三级权限）
- 监控健康（健康检查+告警触发+状态报告）
- 部署脚本（install/NSSM/routes）
- 日志系统（loguru+脱敏+按任务 sink）

### Added — 测试
- 23 个单元测试覆盖全部核心模块
- 1 个集成测试（离线链路冒烟）

## [0.2.0] - 2026-06-19
### Added — 基础设施
- 项目初始化（pyproject.toml+ruff+mypy+pytest 配置）
- 文档 1.0（部署方案/网络工具/离线开发/TDD 规范/模块化架构/侦察工具）
- `参考/` 目录（spider-mengdong/playwright-learn/南方五省参考代码）

## [0.1.0] - 2026-06-19
### Added
- 项目初始提交（Initial commit）
