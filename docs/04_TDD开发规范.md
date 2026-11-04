# 04 - TDD 开发规范

> 适用项目：广东电力交易数据智能采集系统
> 技术栈：Python 3.11+ / pytest / loguru / pydantic-settings / Playwright
> 强制约束：**所有关键逻辑必须采用 TDD 开发，必须有测试覆盖与日志输出**

---

## 1. TDD 总体原则

### 1.1 Red → Green → Refactor 循环

TDD（Test-Driven Development）遵循三步循环：

```
┌──────────────┐      ┌──────────────┐      ┌──────────────┐
│  Red         │ ──▶  │  Green       │ ──▶  │  Refactor    │
│  写一个失败  │      │  最小实现让  │      │  在测试保护  │
│  的测试      │      │  测试通过    │      │  下重构优化  │
└──────────────┘      └──────────────┘      └──────────────┘
        ▲                                            │
        └────────────────────────────────────────────┘
```

- **Red（红）**：先写一个新的、运行后失败的测试。失败原因可以是「函数尚未实现」或「断言不通过」。
- **Green（绿）**：用**最少**的代码让测试通过。允许写得很丑、很粗暴，目标只是变绿。
- **Refactor（重构）**：测试已经变绿后，在测试保护下重构代码（命名、抽函数、消除重复），每一步重构后都要重跑测试确保仍然变绿。

### 1.2 「先写测试，再写实现」工作流

每写一个新功能，遵循以下七步：

1. **拆需求**：将需求拆成可独立验证的最小行为（一个 if 分支即一个测试）。
2. **写测试**：在 `tests/unit/test_xxx.py` 中写第一个失败测试。
3. **跑测试看红**：`pytest tests/unit/test_xxx.py::test_yyy -v`，确认是「正确的失败原因」（不要因 import 错误就误以为变红）。
4. **写最小实现**：在 `src/xxx.py` 中写恰好让测试通过的代码。
5. **跑测试看绿**：再次运行，确认变绿。
6. **重构**：消除坏味道、补全类型注解、抽出魔法数字。
7. **回到第 2 步**：写下一个测试，循环推进。

### 1.3 为什么 TDD 适合本项目

| 项目特性 | TDD 收益 |
|---------|---------|
| 爬虫逻辑分支多（验证码识别、Token 过期、412 重试） | 用测试穷举分支，避免遗漏 |
| 网络环境不稳定（专网、WiFi、4G 切换） | 用 mock 在本地复现极端场景 |
| 涉及硬件交互（U-Key、网络路由） | 用 mock 隔离硬件依赖，单元测试可在 CI 跑 |
| 部署在客户内网，**线上调试成本极高** | 测试是上线前的最后一道防线 |
| 长期运行，对**可靠性**要求高 | 高覆盖率 + 回归测试，防止改 A 坏 B |

### 1.4 覆盖率目标

| 模块类型 | 覆盖率下限 |
|---------|----------|
| 核心逻辑模块（captcha_solver / session_manager / api_fetcher / data_processor / local_landing / task_scheduler / alert_gateway / command_executor） | **≥ 90%** |
| 辅助模块（route_manager / usb_monitor / net_diagnoser / webhook_sender / health_checker / storage_manager） | **≥ 70%** |
| 工具脚本（一次性脚本、配置加载） | 不强制 |

> 覆盖率不是目的，而是**测试是否充分**的体检指标。100% 行覆盖也可能漏分支，必须配合分支覆盖率（`--cov-branch`）一起看。

---

## 2. 必须有测试的模块清单（无一例外）

| 模块 | 必测场景 | 测试类型 | Mock 策略 |
|------|---------|---------|----------|
| **captcha_solver** | 4 位 / 5 位 / 6 位数字验证码识别准确率；模糊图片处理；空图片异常 | 单元 | 预存验证码图片 fixtures（`tests/fixtures/captcha_images/`） |
| **session_manager** | Token 正常获取；Token 过期检测（HTTP 412）；自动刷新；Cookie 持久化保存/恢复；HdKGccbL 参数提取 | 单元 + 集成 | `httpx` 响应 mock |
| **api_fetcher**（通用引擎） | 正常 200 响应解析、412 触发重登录信号（不自己重登，通知上游）、网络超时重试（3 次指数退避）、响应格式异常、不同 API 配置的适配 | 单元 | `respx` / `httpx` mock |
| **route_manager** | 路由添加成功/失败；WiFi 网关自动发现；路由验证通过/失败；默认路由删除；权限不足处理 | 单元 | `subprocess` mock |
| **usb_monitor** | U-Key 插入事件检测；U-Key 拔出事件检测；VID/PID 匹配；无设备时返回空 | 单元 | `WMI` mock |
| **net_diagnoser** | 各网络环境识别（专网/WiFi/4G）、连通性探测、网关自适应发现、诊断结果聚合 | 单元 | `subprocess` / `socket` mock |
| **task_scheduler** | cron 定时触发；手动触发；失败后指数退避重试（1s/2s/4s/8s）；最大重试次数限制；并发任务限制 | 单元 + 集成 | 时间 mock（`freezegun`） |
| **data_processor** | 数据清洗规则应用；基于主键去重；时间序列缺口检测（连续日期缺失）；边界日期处理 | 单元 | 预存 sample 数据 |
| **local_landing** | 目录结构自动创建（年月/年月日/任务名）、台账 Excel 生成（红绿状态/饼图）、zip 自动压缩、文件完整性校验 | 单元 + 集成 | 临时目录 fixtures（`tmp_path`） |
| **login_flow** | 完整登录成功 → 输出有效 session、验证码识别失败重试、密码错误处理、会话超时检测、session 序列化/反序列化 | 集成 | Playwright mock page |
| **alert_gateway** | 消息路由到正确通道、多通道配置加载、静默时段过滤、发送失败重试 | 单元 | `httpx` mock webhook |
| **webhook_sender** | 钉钉签名计算正确、HTTP POST 成功/失败/超时、消息格式化（Markdown） | 单元 | `respx` mock |
| **command_executor** | 指令解析（自然语言 → 操作）、权限校验（三级）、只读操作执行、禁止越权操作 | 单元 | mock 各模块接口 |
| **health_checker** | 各检查项正常/异常判定、整体状态聚合（healthy/degraded/critical）、告警触发阈值 | 单元 | mock 各链路状态 |

> **铁律**：上表中任何模块若 PR 提交时未携带对应测试，CI 将拒绝合并。

---

## 3. 测试目录结构

```
tests/
├── unit/                          # 单元测试（快速，无外部依赖，CI 必跑）
│   ├── test_captcha_solver.py
│   ├── test_session_manager.py
│   ├── test_api_fetcher.py
│   ├── test_login_flow.py
│   ├── test_route_manager.py
│   ├── test_usb_monitor.py
│   ├── test_net_diagnoser.py      # 新增：网络诊断
│   ├── test_task_scheduler.py
│   ├── test_data_processor.py
│   ├── test_local_landing.py      # 替代 test_offline_cache
│   ├── test_alert_gateway.py      # 新增
│   ├── test_webhook_sender.py     # 新增
│   ├── test_command_executor.py   # 新增
│   ├── test_permission_guard.py   # 新增
│   └── test_health_checker.py     # 新增
├── integration/                   # 集成测试（需数据库/网络，可选运行）
│   ├── test_login_flow.py
│   ├── test_api_fetcher_live.py
│   ├── test_alert_send.py         # 替代 test_dingtalk_bot
│   ├── test_storage_manager.py
│   └── test_local_landing_fs.py   # 新增：文件系统实际操作
├── e2e/                           # 端到端测试（需完整环境）
│   └── test_full_crawl_flow.py
├── fixtures/                      # 测试数据
│   ├── captcha_images/            # 验证码测试图片
│   │   ├── 4digit_clear.png
│   │   ├── 5digit_blur.png
│   │   └── ...
│   ├── mock_responses/            # 模拟 HTTP 响应
│   │   ├── api_success.xlsx
│   │   ├── api_412.json
│   │   └── login_page.html
│   ├── sample_data/               # 样本数据
│   │   ├── daily_settlement.xlsx
│   │   └── expected_output.json
│   └── configs/                   # 新增：测试用配置文件
│       ├── test_mode_d.yaml
│       └── test_common.yaml
└── conftest.py                    # 共享 fixtures 和配置
```

### 命名约定

- 测试文件：`test_<被测模块名>.py`，与 `src/<模块名>.py` 一一对应。
- 测试函数：`test_<被测函数>_<场景>_<期望>`，如 `test_recognize_captcha_4digit_returns_correct_string`。
- 参数化测试：`test_<被测函数>_parametrized`，参数 id 用 `pytest.param(..., id="empty_image")` 命名。

---

## 4. pytest 配置规范

### 4.1 `pyproject.toml` 中 pytest 配置

```toml
[tool.pytest.ini_options]
minversion = "8.0"
testpaths = ["tests"]
pythonpath = ["src"]
addopts = [
    "-ra",                          # 显示所有非通过用例的简要原因
    "--strict-markers",             # 未注册的 marker 直接报错
    "--strict-config",
    "--showlocals",                 # 失败时打印局部变量
    "--cov=src",
    "--cov-branch",
    "--cov-report=term-missing",
    "--cov-report=html:htmlcov",
    "--cov-fail-under=85",          # 总覆盖率低于 85% 失败
]
markers = [
    "unit: 纯单元测试，无外部依赖，必须毫秒级",
    "integration: 集成测试，需数据库/网络，可在本地手动运行",
    "e2e: 端到端测试，需完整环境（U-Key、专网）",
    "slow: 慢速测试（> 1 秒），CI 默认跳过",
]
filterwarnings = [
    "error",
    "ignore::DeprecationWarning:playwright.*",
]
log_cli = true
log_cli_level = "INFO"
log_cli_format = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
```

### 4.2 markers 使用

```python
import pytest

@pytest.mark.unit
def test_pure_logic():
    ...

@pytest.mark.integration
def test_with_real_db():
    ...

@pytest.mark.slow
@pytest.mark.integration
def test_long_running_crawl():
    ...
```

CI 中只跑 unit：`pytest -m unit`
本地手动跑全部：`pytest -m "unit or integration"`

### 4.3 fixtures 编写规范

- 共享 fixture 放在 `tests/conftest.py`；模块内独有的放在该测试文件顶部。
- 默认 `scope="function"`，资源昂贵时才升级为 `module` / `session`。
- fixture 名称使用名词，如 `mock_session`，避免动词如 `setup_session`。
- 必须有 docstring 说明 fixture 提供什么。

### 4.4 参数化测试示例

```python
import pytest
from src.captcha_solver import recognize

@pytest.mark.unit
@pytest.mark.parametrize(
    ("image_name", "expected"),
    [
        pytest.param("4digit_clear.png", "1234", id="4digit-clear"),
        pytest.param("5digit_clear.png", "12345", id="5digit-clear"),
        pytest.param("6digit_clear.png", "123456", id="6digit-clear"),
        pytest.param("4digit_blur.png", "5678", id="4digit-blur"),
    ],
)
def test_recognize_captcha_returns_correct_string(image_name, expected, captcha_dir):
    image_bytes = (captcha_dir / image_name).read_bytes()
    assert recognize(image_bytes) == expected
```

### 4.5 conftest.py 共享 fixture 示例

```python
# tests/conftest.py
"""共享 pytest fixtures。"""
from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest
from loguru import logger


FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    """返回测试 fixtures 根目录。"""
    return FIXTURES_DIR


@pytest.fixture(scope="session")
def captcha_dir(fixtures_dir: Path) -> Path:
    """返回验证码图片目录。"""
    return fixtures_dir / "captcha_images"


@pytest.fixture(scope="session")
def mock_responses_dir(fixtures_dir: Path) -> Path:
    """返回模拟 HTTP 响应目录。"""
    return fixtures_dir / "mock_responses"


@pytest.fixture(autouse=True)
def _quiet_loguru() -> Iterator[None]:
    """测试期间将 loguru 写入内存 sink，避免污染 CI 日志。"""
    logger.remove()
    sink_id = logger.add(lambda msg: None, level="DEBUG")
    yield
    logger.remove(sink_id)


@pytest.fixture
def frozen_time():
    """提供时间冻结能力。"""
    from freezegun import freeze_time
    with freeze_time("2026-06-19 09:00:00") as frozen:
        yield frozen
```

---

## 5. Mock / Stub 使用规范

### 5.1 什么时候 Mock

**必须 Mock**（外部副作用、不确定性、慢）：

- 网络请求：`httpx` / `requests` / `aiohttp`
- 文件系统：写盘、删盘（用 `tmp_path` fixture 替代）
- 子进程：`subprocess.run`（路由命令、PowerShell）
- 硬件：USB（WMI）、串口
- 时间：`datetime.now()`、`time.sleep()`（用 `freezegun`）
- 第三方 SaaS：钉钉 API、邮件 SMTP

### 5.2 什么时候不 Mock

**禁止 Mock**（会让测试失去价值）：

- 纯逻辑函数（数据清洗、去重、缺口检测）
- 数据转换（DataFrame 处理）
- 你正在测的「被测对象」本身（mock 自己等于不测）
- 标准库基础容器（list/dict/set）

### 5.3 推荐 Mock 库

| 库 | 用途 |
|---|------|
| `unittest.mock` | 标准库，通用打桩 |
| `pytest-mock` | 提供 `mocker` fixture，自动 teardown |
| `respx` | 专为 httpx 设计的 HTTP mock |
| `freezegun` | 冻结/前进系统时间 |
| `responses` | 兼容 requests 库的 HTTP mock |

`requirements-dev.txt` 必装：

```
pytest>=8.0
pytest-cov>=5.0
pytest-mock>=3.12
pytest-asyncio>=0.23
respx>=0.21
freezegun>=1.5
```

### 5.4 Mock 示例代码

#### 示例 1：用 `respx` mock httpx 的 412 响应

```python
import httpx
import pytest
import respx

from src.api_fetcher import fetch_daily_settlement


@pytest.mark.unit
@respx.mock
async def test_fetch_returns_412_triggers_relogin():
    route = respx.get("https://pm.gd.csg.cn/api/daily").mock(
        return_value=httpx.Response(412, json={"msg": "Token expired"})
    )

    with pytest.raises(TokenExpiredError):
        await fetch_daily_settlement(date="2026-06-18")

    assert route.called
    assert route.call_count == 1
```

#### 示例 2：用 `pytest-mock` mock subprocess

```python
import pytest

from src.route_manager import add_route


@pytest.mark.unit
def test_add_route_calls_correct_command(mocker):
    mock_run = mocker.patch("src.route_manager.subprocess.run")
    mock_run.return_value.returncode = 0

    add_route(destination="10.0.0.0/24", gateway="192.168.1.1")

    mock_run.assert_called_once()
    args = mock_run.call_args[0][0]
    assert "route" in args
    assert "10.0.0.0" in args
    assert "192.168.1.1" in args


@pytest.mark.unit
def test_add_route_permission_denied_raises(mocker):
    mock_run = mocker.patch("src.route_manager.subprocess.run")
    mock_run.return_value.returncode = 1
    mock_run.return_value.stderr = "需要管理员权限"

    with pytest.raises(PermissionError, match="管理员"):
        add_route("10.0.0.0/24", "192.168.1.1")
```

#### 示例 3：用 `freezegun` mock 时间 + 指数退避

```python
import pytest
from freezegun import freeze_time

from src.task_scheduler import retry_with_backoff


@pytest.mark.unit
def test_exponential_backoff_intervals(mocker):
    sleep_mock = mocker.patch("src.task_scheduler.time.sleep")
    func = mocker.MagicMock(side_effect=[Exception, Exception, Exception, "ok"])

    with freeze_time("2026-06-19 09:00:00"):
        result = retry_with_backoff(func, max_attempts=4, base_delay=1)

    assert result == "ok"
    # 验证退避序列：1s -> 2s -> 4s
    assert [c.args[0] for c in sleep_mock.call_args_list] == [1, 2, 4]
```

#### 示例 4：测试告警网关的路由逻辑与静默时段

```python
import pytest
from unittest.mock import AsyncMock, patch

from src.alert_gateway import AlertGateway


@pytest.fixture
def alert_config():
    return {
        "channels": [
            {"type": "dingtalk_webhook", "url": "https://oapi.dingtalk.com/robot/send?access_token=test", "events": ["error"]},
            {"type": "email", "to": ["admin@test.com"], "events": ["error", "task_failed"]},
        ],
        "silence_window": "23:00-07:00",
    }


@pytest.mark.unit
async def test_alert_routes_to_correct_channel(alert_config):
    """告警应路由到配置了对应事件的通道。"""
    gateway = AlertGateway(config=alert_config)
    with patch.object(gateway, "_send_webhook", new=AsyncMock(return_value=True)) as mock_webhook:
        result = await gateway.send_alert(level="error", message="Token expired")
        mock_webhook.assert_called_once()  # error 事件应触发钉钉
        assert result.suppressed is False


@pytest.mark.unit
async def test_silence_window_blocks_alert(alert_config, freezer):
    """静默时段内不发送告警。"""
    freezer.move_to("2026-01-15 23:30:00")
    gateway = AlertGateway(config=alert_config)
    result = await gateway.send_alert(level="error", message="Test")
    assert result.suppressed is True
```

---

## 6. 测试编写模板

新建任何测试文件，**必须**复用以下模板：

```python
"""
test_<module_name>.py

测试 <模块功能> 的 <核心行为>。

Owner: <负责人>
Created: 2026-06-19
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.<module_name> import <SystemUnderTest>


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture
def sut() -> <SystemUnderTest>:
    """被测对象（System Under Test）。"""
    return <SystemUnderTest>(...)


# --------------------------------------------------------------------------- #
# 正常路径（Happy Path）
# --------------------------------------------------------------------------- #

@pytest.mark.unit
def test_xxx_normal_input_returns_expected(sut):
    """正常输入应返回预期结果。"""
    result = sut.do_something("normal input")
    assert result == "expected output"


# --------------------------------------------------------------------------- #
# 异常路径
# --------------------------------------------------------------------------- #

@pytest.mark.unit
def test_xxx_invalid_input_raises_value_error(sut):
    """非法输入应抛出 ValueError。"""
    with pytest.raises(ValueError, match="不能为空"):
        sut.do_something("")


@pytest.mark.unit
def test_xxx_network_failure_retries_three_times(sut, mocker):
    """网络失败应重试 3 次后放弃。"""
    mock_call = mocker.patch.object(sut, "_remote_call", side_effect=ConnectionError)
    with pytest.raises(ConnectionError):
        sut.do_something("input")
    assert mock_call.call_count == 3


# --------------------------------------------------------------------------- #
# 边界条件
# --------------------------------------------------------------------------- #

@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param(0, "zero", id="zero"),
        pytest.param(-1, "negative", id="negative"),
        pytest.param(2**31 - 1, "max-int32", id="max-int32"),
    ],
)
def test_xxx_boundary_values(sut, value, expected):
    """边界值应正确处理。"""
    assert sut.classify(value) == expected
```

---

## 7. 日志规范（loguru）

### 7.1 日志输出分层

本项目要求同时产出两种文件日志：

1. **滚动日志**：`logs/app.log`，按天轮转，保留30天，保障运维查阅全局运行轨迹。
2. **按执行次数独立存储**：每次任务调度独立一个文件，命名规则：
   ```
   logs/{年月日}/{任务名}_{执行时间}.log
   # 示例
   logs/2026-01-15/日清算采集_20260115_083000.log
   ```

### 7.2 配置示例

```yaml
# common.yaml
log:
  persist_to_file: true        # 是否将日志落盘
  file_dir: "./logs"           # 日志根目录
  console_level: "INFO"        # 控制台输出级别
  file_level: "DEBUG"          # 文件输出级别
  retention_days: 30           # 滚动日志保留天数
```

### 7.3 loguru 初始化代码

```python
# src/logging_setup.py
"""全局 loguru 配置，进程入口处调用一次；每次任务调度可调用 attach_task_sink 追加任务独立 sink。"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

from loguru import logger

LOG_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | "
    "{level:<8} | "
    "{module}:{function}:{line} | "
    "{extra[task]: <16} | "
    "{message}"
)


def setup_logging(
    log_dir: Path,
    *,
    persist_to_file: bool = True,
    console_level: str = "INFO",
    file_level: str = "DEBUG",
    retention_days: int = 30,
) -> None:
    """初始化全局日志：
      1. 控制台 sink（彩色，console_level）
      2. 滚动文件 sink：logs/app.log，按天轮转、保留 retention_days
    任务独立 sink 由 attach_task_sink() 在任务启动时追加。
    """
    logger.remove()
    logger.configure(extra={"task": "-"})

    # 1) 控制台 sink
    logger.add(
        sys.stderr,
        level=console_level,
        format=LOG_FORMAT,
        colorize=True,
        enqueue=True,
    )

    if not persist_to_file:
        return

    # 2) 滚动文件 sink（全局）
    log_dir.mkdir(parents=True, exist_ok=True)
    logger.add(
        log_dir / "app.log",
        level=file_level,
        format=LOG_FORMAT,
        rotation="00:00",                  # 每日 0 点切分
        retention=f"{retention_days} days",  # 保留 N 天
        compression="zip",
        encoding="utf-8",
        enqueue=True,
        backtrace=True,
        diagnose=True,
    )


def attach_task_sink(log_dir: Path, task_name: str, file_level: str = "DEBUG") -> int:
    """为本次任务执行追加一个独立文件 sink：
      logs/{YYYY-MM-DD}/{task_name}_{YYYYMMDD_HHMMSS}.log
    返回 sink_id，任务结束后调用 logger.remove(sink_id) 释放。
    """
    now = datetime.now()
    day_dir = log_dir / now.strftime("%Y-%m-%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    file_path = day_dir / f"{task_name}_{now.strftime('%Y%m%d_%H%M%S')}.log"

    sink_id = logger.add(
        file_path,
        level=file_level,
        format=LOG_FORMAT,
        encoding="utf-8",
        enqueue=True,
        backtrace=True,
        diagnose=True,
        filter=lambda record: record["extra"].get("task") == task_name,
    )
    return sink_id
```

> **告警推送**不再作为 loguru sink 直接接入，改由 `alert_gateway` 统一接收上游事件后路由到多通道（钉钉 / 企业微信 / 邮件等）。

### 7.4 关键操作必须记录（硬性要求）

| 操作 | 日志级别 | 必记字段 |
|------|---------|---------|
| U-Key 插入/拔出 | INFO | 时间、设备 ID（VID:PID）|
| WiFi 连接/断开 | INFO | 时间、SSID、信号强度（dBm）|
| 路由变更 | INFO | 变更前后路由表 diff |
| 登录尝试 | INFO | 时间、结果（成功/失败）、耗时 |
| Token 刷新 | INFO | 时间、旧 Token 前 8 位、新 Token 前 8 位 |
| 数据采集 | INFO | 时间、数据项、记录数、文件大小 |
| 采集失败 | ERROR | 时间、错误类型、HTTP 状态码、重试次数 |
| 定时任务触发 | INFO | 任务名、计划时间、实际时间 |
| 异常 | ERROR | 完整堆栈、上下文变量 |
| 告警发送 | INFO | 通道类型、目标、消息摘要、发送结果 |
| Agent 指令接收 | INFO | 指令原文、解析结果、权限级别 |
| Agent 指令执行 | INFO/WARN | 操作类型、执行结果、耗时 |
| 台账生成 | INFO | 任务 ID、文件路径、成功/失败项数 |
| zip 压缩 | INFO | 源目录、目标文件、大小 |

### 7.5 日志编写规范

#### ✅ 不要用 f-string，用 loguru lazy formatting

```python
# ❌ BAD：即使日志级别被过滤，f-string 也会先格式化
logger.debug(f"Fetched {count} records, sample={records[:3]}")

# ✅ GOOD：lazy formatting，DEBUG 被过滤时不会序列化 records
logger.debug("Fetched {} records, sample={}", count, records[:3])
```

#### ✅ 结构化上下文（bind）

```python
task_logger = logger.bind(task_id=task.id, task_name=task.name)
task_logger.info("Task started")
task_logger.info("Task finished, rows={}", row_count)
```

#### ✅ 异常记录用 logger.exception

```python
try:
    fetch()
except Exception:
    logger.exception("Failed to fetch data")  # 自动记录堆栈
```

#### ✅ 敏感信息脱敏

```python
def mask_token(token: str) -> str:
    """Token 仅保留前 8 位，其余以 * 替代。"""
    return token[:8] + "*" * 8 if token else ""

logger.info("Token refreshed: {} -> {}", mask_token(old), mask_token(new))

# ❌ 永远不要记录密码
logger.info("Login with password: {}", password)  # 严禁
```

---

## 8. CI/CD 集成

### 8.1 GitHub Actions 示例

```yaml
# .github/workflows/ci.yml
name: CI

on:
  push:
    branches: [main, develop]
  pull_request:

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
          cache: "pip"

      - name: Install dependencies
        run: |
          pip install -U pip
          pip install -e ".[dev]"

      - name: Lint (ruff)
        run: ruff check src tests

      - name: Format check (ruff)
        run: ruff format --check src tests

      - name: Type check (mypy)
        run: mypy src

      - name: Unit tests
        run: pytest -m unit --cov=src --cov-fail-under=85

      - name: Upload coverage
        uses: codecov/codecov-action@v4
        with:
          files: ./coverage.xml
```

### 8.2 GitLab CI 示例

```yaml
# .gitlab-ci.yml
stages: [lint, test]

variables:
  PIP_CACHE_DIR: "$CI_PROJECT_DIR/.cache/pip"

lint:
  stage: lint
  image: python:3.11
  script:
    - pip install ruff mypy
    - ruff check src tests
    - mypy src

unit-tests:
  stage: test
  image: python:3.11
  script:
    - pip install -e ".[dev]"
    - pytest -m unit --cov=src --cov-fail-under=85 --cov-report=term --cov-report=xml
  coverage: '/TOTAL.*\s+(\d+%)$/'
  artifacts:
    reports:
      coverage_report:
        coverage_format: cobertura
        path: coverage.xml
```

### 8.3 pre-commit hooks

```yaml
# .pre-commit-config.yaml
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.6.0
    hooks:
      - id: ruff
        args: [--fix]
      - id: ruff-format

  - repo: https://github.com/pre-commit/mirrors-mypy
    rev: v1.11.0
    hooks:
      - id: mypy
        additional_dependencies: [pydantic, types-requests]

  - repo: https://github.com/pre-commit/pre-commit-hooks
    rev: v4.6.0
    hooks:
      - id: trailing-whitespace
      - id: end-of-file-fixer
      - id: check-yaml
      - id: check-added-large-files
        args: [--maxkb=500]

  - repo: local
    hooks:
      - id: pytest-unit
        name: pytest unit tests
        entry: pytest -m unit -x
        language: system
        pass_filenames: false
        types: [python]
```

安装：

```bash
pip install pre-commit
pre-commit install
```

---

## 9. 代码质量工具

### 9.1 ruff 配置（pyproject.toml）

```toml
[tool.ruff]
target-version = "py311"
line-length = 100
src = ["src", "tests"]

[tool.ruff.lint]
select = [
    "E", "F", "W",   # pycodestyle / pyflakes
    "I",             # isort
    "N",             # pep8-naming
    "UP",            # pyupgrade
    "B",             # bugbear
    "C4",            # comprehensions
    "SIM",           # simplify
    "PT",            # pytest-style
    "RUF",           # ruff-specific
    "ANN",           # annotations
    "TID",           # tidy-imports
]
ignore = ["ANN101", "ANN102"]   # 无需注解 self / cls

[tool.ruff.lint.per-file-ignores]
"tests/**/*.py" = ["ANN", "S101"]   # 测试文件允许 assert，不强制注解

[tool.ruff.format]
quote-style = "double"
indent-style = "space"
```

### 9.2 mypy 配置（pyproject.toml）

```toml
[tool.mypy]
python_version = "3.11"
strict = true
warn_unused_ignores = true
warn_redundant_casts = true
disallow_any_generics = true
disallow_untyped_defs = true
no_implicit_optional = true
plugins = ["pydantic.mypy"]

[[tool.mypy.overrides]]
module = ["wmi.*", "playwright.*"]
ignore_missing_imports = true

[[tool.mypy.overrides]]
module = "tests.*"
disallow_untyped_defs = false
```

### 9.3 pre-commit 已在 8.3 节给出

---

## 10. TDD 实战示例（以 captcha_solver 为例）

需求：实现一个验证码识别器 `recognize(image_bytes: bytes) -> str`，识别 4-6 位纯数字验证码。

### 第一轮：识别 4 位数字

#### Step 1：Red — 先写测试

```python
# tests/unit/test_captcha_solver.py
"""测试验证码识别。"""
import pytest

from src.captcha_solver import recognize


@pytest.mark.unit
def test_recognize_4digit_clear_image_returns_digits(captcha_dir):
    image_bytes = (captcha_dir / "4digit_clear.png").read_bytes()
    result = recognize(image_bytes)
    assert result == "1234"
```

运行：

```bash
$ pytest tests/unit/test_captcha_solver.py -v
ImportError: cannot import name 'recognize' from 'src.captcha_solver'
```

✅ 红得正确（因为函数尚未实现）。

#### Step 2：Green — 最小实现

```python
# src/captcha_solver.py
"""验证码识别。"""
import ddddocr

_ocr = ddddocr.DdddOcr(show_ad=False)


def recognize(image_bytes: bytes) -> str:
    """识别纯数字验证码，返回识别结果。"""
    return _ocr.classification(image_bytes)
```

运行：

```bash
$ pytest tests/unit/test_captcha_solver.py -v
PASSED  test_recognize_4digit_clear_image_returns_digits
```

✅ 变绿。

#### Step 3：Refactor — 暂时无可重构，进入下一轮

---

### 第二轮：扩展到 5 位、6 位（参数化）

#### Step 1：Red — 加测试

```python
@pytest.mark.unit
@pytest.mark.parametrize(
    ("image_name", "expected"),
    [
        pytest.param("4digit_clear.png", "1234", id="4digit"),
        pytest.param("5digit_clear.png", "12345", id="5digit"),
        pytest.param("6digit_clear.png", "123456", id="6digit"),
    ],
)
def test_recognize_various_lengths(captcha_dir, image_name, expected):
    image_bytes = (captcha_dir / image_name).read_bytes()
    assert recognize(image_bytes) == expected
```

运行：假设 6 位识别失败 — 红。

#### Step 2：Green — 改进实现

```python
def recognize(image_bytes: bytes) -> str:
    """识别纯数字验证码（4-6 位），返回识别结果。"""
    raw = _ocr.classification(image_bytes)
    # 仅保留数字
    digits = "".join(c for c in raw if c.isdigit())
    if not 4 <= len(digits) <= 6:
        raise ValueError(f"识别结果长度异常: {raw!r}")
    return digits
```

变绿 ✅。

---

### 第三轮：异常处理（空图片）

#### Step 1：Red

```python
@pytest.mark.unit
def test_recognize_empty_bytes_raises_value_error():
    with pytest.raises(ValueError, match="图片为空"):
        recognize(b"")
```

#### Step 2：Green

```python
def recognize(image_bytes: bytes) -> str:
    if not image_bytes:
        raise ValueError("图片为空")
    raw = _ocr.classification(image_bytes)
    digits = "".join(c for c in raw if c.isdigit())
    if not 4 <= len(digits) <= 6:
        raise ValueError(f"识别结果长度异常: {raw!r}")
    return digits
```

变绿 ✅。

---

### 第四轮：Refactor — 加入日志、类型注解、提取常量

```python
# src/captcha_solver.py
"""验证码识别。

策略：使用 ddddocr 进行 OCR，过滤非数字字符，校验长度 4-6。
"""
from __future__ import annotations

from typing import Final

import ddddocr
from loguru import logger

_MIN_LEN: Final = 4
_MAX_LEN: Final = 6
_ocr: Final = ddddocr.DdddOcr(show_ad=False)


def recognize(image_bytes: bytes) -> str:
    """识别 4-6 位纯数字验证码。

    Args:
        image_bytes: 验证码图片的二进制数据（PNG/JPG）。

    Returns:
        识别出的数字字符串。

    Raises:
        ValueError: 当图片为空，或识别结果长度不在 [4,6] 区间。
    """
    if not image_bytes:
        logger.warning("Captcha recognize called with empty bytes")
        raise ValueError("图片为空")

    raw = _ocr.classification(image_bytes)
    digits = "".join(c for c in raw if c.isdigit())

    if not _MIN_LEN <= len(digits) <= _MAX_LEN:
        logger.error(
            "Captcha length invalid: raw={!r}, digits={!r}, len={}",
            raw, digits, len(digits),
        )
        raise ValueError(f"识别结果长度异常: {raw!r}")

    logger.debug("Captcha recognized: {} (raw={!r})", digits, raw)
    return digits
```

#### Step 3：所有测试仍绿

```bash
$ pytest tests/unit/test_captcha_solver.py -v
PASSED test_recognize_4digit_clear_image_returns_digits
PASSED test_recognize_various_lengths[4digit]
PASSED test_recognize_various_lengths[5digit]
PASSED test_recognize_various_lengths[6digit]
PASSED test_recognize_empty_bytes_raises_value_error

5 passed in 0.21s
Coverage: 100%
```

✅ 全部通过，覆盖率 100%。

---

## 附录 A：常用 pytest 命令速查

| 场景 | 命令 |
|------|------|
| 跑全部单元测试 | `pytest -m unit` |
| 跑某文件 | `pytest tests/unit/test_captcha_solver.py` |
| 跑某用例 | `pytest tests/unit/test_captcha_solver.py::test_recognize_4digit_clear_image_returns_digits` |
| 失败立即停止 | `pytest -x` |
| 跑上次失败的 | `pytest --lf` |
| 显示最慢的 10 个 | `pytest --durations=10` |
| 实时输出 print/log | `pytest -s` |
| 生成 HTML 覆盖率报告 | `pytest --cov=src --cov-report=html` 然后打开 `htmlcov/index.html` |
| 排除慢测试 | `pytest -m "not slow"` |
| 并行运行（pytest-xdist） | `pytest -n auto` |

---

## 附录 B：违规示例（PR 必拒）

```python
# ❌ 没测试
def fetch_data(url):
    return httpx.get(url).json()

# ❌ 测试但没断言
def test_fetch_data():
    fetch_data("http://...")

# ❌ Mock 了被测对象本身
def test_fetch_data(mocker):
    mocker.patch("src.api.fetch_data", return_value={"ok": True})
    assert fetch_data("...") == {"ok": True}   # 等于啥也没测

# ❌ 用 print 代替 logger
def fetch_data(url):
    print(f"fetching {url}")
    return httpx.get(url).json()

# ❌ Token 明文打印
logger.info("Got token: {}", token)
```

---

**文档版本**：v1.1（与 01/02/03 同步：按执行次数独立日志、告警网关、组件隔离）
**生效日期**：2026-06-19
**复审周期**：每季度一次
