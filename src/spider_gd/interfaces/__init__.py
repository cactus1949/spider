"""跨链路 Protocol 接口契约（链路0 冻结）。

依赖方向铁律：业务模块仅依赖本包定义的 Protocol，不跨层 import 实现类。
"""

from __future__ import annotations

from spider_gd.interfaces.agent import AgentCommand, AgentResponse, IAgentAdapter
from spider_gd.interfaces.alert import AlertLevel, AlertResult, IAlertGateway
from spider_gd.interfaces.crawler import (
    CrawlResult,
    CrawlTask,
    EndpointConfig,
    IApiFetcher,
    ICaptchaSolver,
    ILoginFlow,
    ISessionProvider,
    Session,
)
from spider_gd.interfaces.monitor import HealthLevel, HealthReport, IHealthChecker
from spider_gd.interfaces.network import (
    DeviceInfo,
    DiagnoseReport,
    INetworkValidator,
    IRouteManager,
    IUsbMonitor,
    ValidateReport,
)
from spider_gd.interfaces.scheduler import IScheduler, JobHandler, JobSpec
from spider_gd.interfaces.storage import (
    ILocalLanding,
    ITaskStateStore,
    TaskItemRecord,
    TaskRecord,
)

__all__ = [
    # agent
    "AgentCommand",
    "AgentResponse",
    "IAgentAdapter",
    # alert
    "AlertLevel",
    "AlertResult",
    "IAlertGateway",
    # crawler
    "CrawlResult",
    "CrawlTask",
    "EndpointConfig",
    "IApiFetcher",
    "ICaptchaSolver",
    "ILoginFlow",
    "ISessionProvider",
    "Session",
    # monitor
    "HealthLevel",
    "HealthReport",
    "IHealthChecker",
    # network
    "DeviceInfo",
    "DiagnoseReport",
    "INetworkValidator",
    "IRouteManager",
    "IUsbMonitor",
    "ValidateReport",
    # scheduler
    "IScheduler",
    "JobHandler",
    "JobSpec",
    # storage
    "ILocalLanding",
    "ITaskStateStore",
    "TaskItemRecord",
    "TaskRecord",
]
