"""爬虫核心异常。"""

from __future__ import annotations


class CrawlerError(Exception):
    """爬虫基类异常。"""


class TokenExpiredError(CrawlerError):
    """Token / HdKGccbL 过期（HTTP 412），需重新读取会话。"""


class AuthFailedError(CrawlerError):
    """认证失败（HTTP 401/403）。"""


class CaptchaError(CrawlerError):
    """验证码识别失败。"""


class SiteUnreachableError(CrawlerError):
    """目标站点不可达（连接被拒绝/重置/DNS 失败等），需告警暂停。

    与一般的网络瞬时抖动不同：此错误表示重试已耗尽且目标主机无法连接，
    通常意味着站点维护/关停/U-Key 网络异常，继续重试无意义。
    """


def is_playwright_goto_error(exc: BaseException) -> bool:
    """判断 Playwright page.goto() 异常是否为连接不可达错误。

    检测 Chrome 网络层错误码（net::ERR_*），覆盖：
    - ERR_CONNECTION_CLOSED / REFUSED / RESET / FAILED
    - ERR_NAME_NOT_RESOLVED（DNS 失败）
    - ERR_INTERNET_DISCONNECTED
    - ERR_TIMED_OUT / ERR_CONNECTION_TIMED_OUT
    """
    msg = str(exc)
    markers = (
        "ERR_CONNECTION_CLOSED", "ERR_CONNECTION_REFUSED",
        "ERR_CONNECTION_RESET", "ERR_CONNECTION_FAILED",
        "ERR_NAME_NOT_RESOLVED", "ERR_INTERNET_DISCONNECTED",
        "ERR_TIMED_OUT", "ERR_CONNECTION_TIMED_OUT",
        "ERR_PROXY_CONNECTION_FAILED", "ERR_ADDRESS_UNREACHABLE",
    )
    return any(m in msg for m in markers)
