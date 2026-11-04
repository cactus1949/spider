"""调度链路接口契约（链路3）。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class JobSpec:
    job_id: str
    cron: str  # 如 "0 9,15 * * *"
    handler_name: str
    payload: dict[str, object] = field(default_factory=dict)
    enabled: bool = True


JobHandler = Callable[[dict[str, object]], Awaitable[None]]


class IScheduler(Protocol):
    async def add_job(self, spec: JobSpec, handler: JobHandler) -> str: ...

    async def remove_job(self, job_id: str) -> bool: ...

    async def list_jobs(self) -> list[JobSpec]: ...

    async def trigger_now(self, job_id: str) -> bool: ...

    async def start(self) -> None: ...

    async def shutdown(self) -> None: ...
