"""爬虫链路接口契约（链路1）。

设计要点：
- ``login_flow`` 仅负责产出有效 :class:`Session`，不调用 ``api_fetcher``（上下游关系）。
- ``api_fetcher`` 是配置驱动的通用引擎，不硬编码业务接口。
- ``HdKGccbL`` 取自实时页面 URL（见 :class:`EndpointConfig.hdkgccbl_source`）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class Session:
    """登录后产出的有效会话凭证。"""

    bearer_token: str
    cookies: dict[str, str] = field(default_factory=dict)
    hdkgccbl: str = ""
    page_url: str = ""


@dataclass
class CrawlTask:
    """采集任务输入。"""

    task_id: str
    date: str  # YYYYMMDD
    data_type: str  # 如 "daily_settlement"
    priority: int = 0


@dataclass
class CrawlResult:
    """采集任务输出。"""

    task_id: str
    success: bool
    data: bytes | None = None  # Excel/JSON 二进制
    record_count: int = 0
    error_msg: str | None = None
    duration_ms: int = 0


@dataclass
class EndpointConfig:
    """单个 API 端点的配置（从 yaml/json 任务配置加载，非硬编码）。"""

    name: str
    url: str
    method: str = "POST"
    params: dict[str, str] = field(default_factory=dict)
    hdkgccbl_source: str = "page_url"  # page_url / config / none
    extra_headers: dict[str, str] = field(default_factory=dict)
    content_type: str = "application/x-www-form-urlencoded;charset=UTF-8"
    date_param: str = "pdate"        # 日期参数名，默认 pdate（POST body），GET 端点可设为 "date"
    date_format: str = "YYYYMMDD"    # 日期格式，默认 YYYYMMDD（去掉横线），GET 端点用 "YYYY-MM-DD"


class ICaptchaSolver(Protocol):
    """验证码识别（字母+数字）。"""

    def recognize(self, image_bytes: bytes) -> str: ...


class ISessionProvider(Protocol):
    """会话凭证提供者：实时读取 token/cookie/HdKGccbL，并检测 412 过期。"""

    async def get_session(self) -> Session: ...

    def is_expired(self, status_code: int) -> bool: ...

    async def refresh(self) -> Session: ...


class ILoginFlow(Protocol):
    """登录流程编排：账密 + 验证码 → 提交 → 校验 → 输出有效 session。"""

    async def execute(self) -> Session: ...


class IApiFetcher(Protocol):
    """通用 API 采集引擎：用有效 session + 端点配置取数据。"""

    async def fetch(
        self, session: Session, endpoint: EndpointConfig, task: CrawlTask
    ) -> CrawlResult: ...
