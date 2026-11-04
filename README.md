# spider-gd · 广东电力交易数据智能采集系统

通过 U-Key（USB 随身 WiFi）接入电力交易中心专网（`pm.gd.csg.cn`），自动登录、采集、落地数据。
采用 **Protocol 驱动 + 离线核心优先 + 配置装配** 架构：离线模式（D）为不可妥协的底座，
联网能力（告警 / Agent / Web）作为可选附加。

> 设计文档见 [`PROMPT.md`](PROMPT.md)、[`docs/`](docs/)、[`电力交易数据采集系统方案.md`](电力交易数据采集系统方案.md)、架构图。

## 架构总览

```
现场侦察(tools/recon_tool) ──HAR/API清单──▶ 链路1 爬虫核心
        │(复用)                                  │session/数据
        ▼                                        ▼
infrastructure/network(链路2) ── 链路0 接口/配置/日志 ── 链路3 调度+存储+落地
                                       │                       │
                              gateway/alert(链路4) ◀── 链路6 监控 ── gateway/agent(链路5,可选)
```

依赖方向铁律：`core` / `infrastructure` / `gateway` 仅依赖 `interfaces/` 与 `config/`，不跨层 import 实现类。

## 目录结构

```
src/spider_gd/
├── interfaces/        # 链路0：冻结的 Protocol 接口契约
├── config/            # 链路0：settings(多yaml inherits) + loguru 双 sink
├── core/
│   ├── crawler/       # 链路1：captcha/session/api_fetcher/login_flow/playwright_runner
│   ├── pipeline/      # 链路3：local_landing(目录+台账+zip)/data_processor
│   └── scheduler/     # 链路3：task_scheduler/retry_policy
├── infrastructure/
│   ├── network/       # 链路2：usb_monitor/diagnoser/dns_resolver/route_writer/validator
│   ├── storage/       # 链路3：SQLite 状态追踪（仅状态，不存数据）
│   └── monitor/       # 链路6：health_checker/status_reporter/alert_trigger
│   └── gateway/...    # 见 src/spider_gd/gateway
├── gateway/
│   ├── alert/         # 链路4：告警网关 + 钉钉签名 + Null 兜底
│   └── agent/         # 链路5：可拆卸远程 Agent（当前仅 readonly）
└── app.py             # 依赖注入装配 + 离线链路
tools/recon_tool/      # 现场侦察工具（复用 network 核心）
configs/               # common.yaml + mode_{a,b,d}_*.yaml
deploy/                # install.bat / setup_routes.ps1 / nssm_service.bat
tests/                 # unit / integration / fixtures
```

## 安装

```bash
uv venv
uv pip install loguru pydantic pydantic-settings pyyaml httpx openpyxl pandas apscheduler playwright
# 开发依赖
uv pip install pytest pytest-asyncio pytest-cov pytest-mock respx freezegun ruff mypy
# 爬虫/侦察需要浏览器内核
.venv/Scripts/python.exe -m playwright install chromium
# 验证码（可选 extra）
uv pip install ddddocr
```

或直接 `deploy/install.bat`。

## 运行

```bash
# 默认离线模式启动（装配各模块 + 调度常驻）
python main.py --config configs/mode_d_offline.yaml run

# 查看所有子命令
python main.py --help
```

各链路可独立运行：

| 命令 | 链路 | 说明 |
|------|------|------|
| `python main.py crawl --date 20260618 --dry-run` | 1 | 采集（HAR 离线回放） |
| `python main.py network --validate` | 2 | 网络连通性验证 |
| `python main.py schedule --list` | 3 | 列出调度任务 |
| `python main.py alert --test "消息"` | 4 | 测试告警 |
| `python main.py health --check` | 6 | 单次健康检查 |
| `python -m recon_tool.cli start` | 侦察 | 现场一键侦察（网络+HAR+分析+打包） |

> Windows 上若包未安装，临时设 `PYTHONPATH=src;tools` 后用 `python -m spider_gd ...`。

## 测试

```bash
# CI 集（确定性，毫秒级）
python -m pytest -m "not slow"

# 全量（含集成、需更多依赖）
python -m pytest -m "unit or integration"

# 覆盖率
python -m pytest -m "not slow" --cov=spider_gd --cov-branch --cov-report=term-missing
```

- 单元测试全部 Mock 外部副作用（subprocess/WMI/httpx/文件/时间）。
- 验证码识别率类测试标 `@pytest.mark.slow`，CI 默认跳过（需现场样本）。
- Lint/Format：`ruff check src tools tests` / `ruff format`。

## 部署模式

| 模式 | 配置 | 启用模块 |
|------|------|---------|
| D 离线（默认底座） | `mode_d_offline.yaml` | 核心 + 网络（告警/Agent 关闭） |
| A 单机有线 | `mode_a_wired.yaml` | 核心 + 告警 + Web |
| B 单机无线/4G | `mode_b_wireless.yaml` | 同 A，网卡不同 |

切换模式只改 `--config`，业务代码零改动。现场路由配置见 `deploy/setup_routes.ps1`。

## 现场侦察工作流（无数据先行）

1. 带 `recon` 工具到现场，`python -m recon_tool.cli start`。
2. 插 U-Key → 自动网络侦察 → 手动登录浏览 → 录制 HAR + 截图。
3. 产出 `session.har` / `api_endpoints.json` / `summary_report.md` / zip。
4. 回办公室用 HAR 离线回放开发链路1，确认验证码类型与 API 形态。
