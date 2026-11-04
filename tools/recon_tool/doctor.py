"""侦察工具自检：Playwright/Chromium 就绪、管理员权限。"""

from __future__ import annotations


def check_playwright() -> tuple[bool, str]:
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False, "playwright 未安装（pip install playwright && playwright install chromium）"
    return True, "playwright 已安装"


def check_admin() -> tuple[bool, str]:
    try:
        import ctypes

        ok = ctypes.windll.shell32.IsUserAnAdmin() != 0  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return False, "非 Windows 或无法判断管理员权限"
    return bool(ok), "管理员权限就绪" if ok else "未以管理员身份运行（路由读取可能失败）"


def run_doctor() -> dict[str, tuple[bool, str]]:
    return {
        "playwright": check_playwright(),
        "admin": check_admin(),
    }
