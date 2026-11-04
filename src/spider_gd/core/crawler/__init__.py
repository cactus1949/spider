"""爬虫核心引擎（链路1）。"""

from __future__ import annotations

from spider_gd.core.crawler.api_fetcher import ApiFetcher
from spider_gd.core.crawler.captcha_solver import CaptchaSolver
from spider_gd.core.crawler.crawl_orchestrator import CrawlOrchestrator
from spider_gd.core.crawler.exceptions import AuthFailedError, TokenExpiredError
from spider_gd.core.crawler.login_flow import LoginFlow
from spider_gd.core.crawler.playwright_runner import PlaywrightRunner
from spider_gd.core.crawler.rpa_fetcher import RpaFetcher
from spider_gd.core.crawler.session_keeper import ReloginExhaustedError, SessionKeeper
from spider_gd.core.crawler.session_provider import BrowserSessionProvider
from spider_gd.core.crawler.task_lock import TaskLock, TaskLockedError

__all__ = [
    "ApiFetcher",
    "AuthFailedError",
    "BrowserSessionProvider",
    "CaptchaSolver",
    "CrawlOrchestrator",
    "LoginFlow",
    "PlaywrightRunner",
    "ReloginExhaustedError",
    "RpaFetcher",
    "SessionKeeper",
    "TaskLock",
    "TaskLockedError",
    "TokenExpiredError",
]
