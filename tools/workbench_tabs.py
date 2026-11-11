"""门户工作台和业务页的地址分类。

任务开始前只留一个 ``portal/#/powerEnterprise``。业务系统页（现货、交易、结算）
是每次执行打开的，做完要关掉。其它地址视为用户自己的页面，不主动关。
"""

from __future__ import annotations

from urllib.parse import urlsplit

__all__ = [
    "BUSINESS_ROOTS",
    "choose_workbench",
    "is_business_url",
    "is_workbench_url",
    "page_role",
]

BUSINESS_ROOTS = ("/pfjy/", "/pfxh/", "/pfjs/")


def page_role(url: str) -> str:
    """判断这个地址在任务里扮演什么角色。

    Args:
        url: 标签页地址。

    Returns:
        str: ``workbench`` 是门户工作台，``business`` 是本系统打开的业务页，
        其余（含用户自己打开的页面）是 ``other``。

    Examples:
        >>> page_role("https://pm.gd.csg.cn/portal/#/powerEnterprise")
        'workbench'
        >>> page_role("https://pm.gd.csg.cn/pfxh/stc/index")
        'business'
        >>> page_role("https://pm.gd.csg.cn/portal/#/home")
        'other'
    """
    try:
        parts = urlsplit(str(url or ""))
    except ValueError:
        return "other"
    host = parts.netloc.lower()
    if "pm.gd.csg.cn" not in host:
        return "other"
    if "powerenterprise" in parts.fragment.lower():
        return "workbench"
    path = parts.path.lower()
    if any(path.startswith(root) or path == root.rstrip("/") for root in BUSINESS_ROOTS):
        return "business"
    return "other"


def is_workbench_url(url: str) -> bool:
    """是不是门户工作台地址。

    Args:
        url: 标签页地址。

    Returns:
        bool: ``portal/#/powerEnterprise`` 时为 True。

    Examples:
        >>> is_workbench_url("https://pm.gd.csg.cn/portal#/powerEnterprise")
        True
    """
    return page_role(url) == "workbench"


def is_business_url(url: str) -> bool:
    """是不是任务会打开的业务系统页。

    Args:
        url: 标签页地址。

    Returns:
        bool: 现货、交易、结算这些路径为 True。

    Examples:
        >>> is_business_url("https://pm.gd.csg.cn/pfjy/stc/index")
        True
    """
    return page_role(url) == "business"


def choose_workbench(scored: list[tuple[object, int]]) -> object | None:
    """在多个工作台里留下分数最高的一个。

    分数由调用方计算：有菜单且已登录最高。分数为 0 的空壳不算可用工作台。
    同分时保留先出现的那个，一般是更早打开的原工作台。

    Args:
        scored: ``(页面, 分数)`` 列表。

    Returns:
        object | None: 选中的页面。都不可用时为 None。

    Examples:
        >>> choose_workbench([("empty", 0), ("menu", 2), ("both", 3)])
        'both'
        >>> choose_workbench([("empty", 0)]) is None
        True
    """
    usable = [(page, score) for page, score in scored if score > 0]
    if not usable:
        return None
    return max(usable, key=lambda item: item[1])[0]
