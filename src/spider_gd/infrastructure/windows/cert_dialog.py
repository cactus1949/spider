"""原生证书选择窗口：只在唯一一行对上单位之后才点确定。

Windows 上的点击要到现场用 UI Automation 验收。这里的匹配可以在任何系统上测。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from spider_gd.core.station_profile import org_name_matches


@dataclass(frozen=True)
class CertDialogRow:
    """证书窗口表格里的一行。

    Args:
        issued_to: 颁发给。
        org_name: 单位。窗口可能把末尾截成省略号。
        expires: 截止日期。
        issuer: 颁发者。

    Examples:
        >>> CertDialogRow("用户", "阳江公司", "2027-01-01", "NETCA").org_name
        '阳江公司'
    """

    issued_to: str
    org_name: str
    expires: str = ""
    issuer: str = ""


@dataclass(frozen=True)
class CertDialogResult:
    """一次证书窗口操作的结果。

    Args:
        ok: 已经选中唯一行并点了确定。
        reason: 没点确定的原因。
        row_index: 选中的行，从 0 计。没选中时为 None。
        absent: 这段时间里没有出现证书窗口。
        needs_windows: 匹配逻辑之外的真实点击还没在这台机器上做过。

    Examples:
        >>> CertDialogResult(False, "证书窗口有多行匹配", None).ok
        False
    """

    ok: bool
    reason: str
    row_index: int | None = None
    absent: bool = False
    needs_windows: bool = False


def match_dialog_row(rows: list[CertDialogRow], keyword: str) -> CertDialogResult:
    """在证书窗口里找唯一对得上单位的一行。

    对不上或不只一行时，不返回可以点击的行。

    Args:
        rows: 当前表格行。
        keyword: 场站保存的单位关键字。

    Returns:
        CertDialogResult: 唯一命中时 ``ok`` 仍为 False，调用方点完确定后再标成功。
        ``row_index`` 有值才允许点确定。

    Examples:
        >>> rows = [
        ...     CertDialogRow("甲", "阳江公司", "2027", "NETCA"),
        ...     CertDialogRow("乙", "韶关公司", "2027", "NETCA"),
        ... ]
        >>> match_dialog_row(rows, "阳江").row_index
        0
        >>> match_dialog_row(rows, "公司").row_index is None
        True
    """
    hits = [
        index for index, row in enumerate(rows)
        if org_name_matches(keyword, row.org_name)
    ]
    if len(hits) == 1:
        return CertDialogResult(False, "", hits[0])
    if not hits:
        return CertDialogResult(False, "证书窗口没有匹配的单位")
    return CertDialogResult(False, "证书窗口有多行匹配")


def confirm_certificate(keyword: str, thumbprint: str = "") -> CertDialogResult:
    """在 Windows 上找原生证书窗口并确认唯一行。

    非 Windows 直接返回「需要现场验收」，不去点任何窗口。
    指纹目前只留给调用方做证书库消歧，窗口表格没有指纹列，仍按单位匹配。

    Args:
        keyword: 场站单位关键字。
        thumbprint: 预留。窗口上没有这一列，本函数不拿它盲选。

    Returns:
        CertDialogResult: 没在 Windows 上跑过真实点击时 ``needs_windows`` 为 True。

    Examples:
        >>> result = confirm_certificate("阳江")
        >>> result.needs_windows or result.ok or result.reason
        True
    """
    del thumbprint
    if sys.platform != "win32":
        return CertDialogResult(
            False,
            "原生证书窗口需要在 Windows 现场验收",
            needs_windows=True,
            absent=True,
        )
    return _confirm_on_windows(keyword)


def _confirm_on_windows(keyword: str) -> CertDialogResult:
    """读当前证书窗口。读不到或对不上就不点确定。"""
    try:
        import uiautomation as auto
    except Exception as exc:
        return CertDialogResult(False, f"缺少 UI Automation：{exc}", needs_windows=True, absent=True)
    root = auto.GetRootControl()
    dialog = None
    for window in root.GetChildren():
        name = str(getattr(window, "Name", "") or "")
        if "证书" not in name:
            continue
        dialog = window
        break
    if dialog is None:
        return CertDialogResult(False, "", absent=True, needs_windows=True)
    rows = _read_rows(dialog)
    matched = match_dialog_row(rows, keyword)
    if matched.row_index is None:
        return CertDialogResult(False, matched.reason, needs_windows=True)
    table_rows = _table_rows(dialog)
    if matched.row_index >= len(table_rows):
        return CertDialogResult(False, "证书窗口行对不上", needs_windows=True)
    try:
        table_rows[matched.row_index].GetSelectionItemPattern().Select()
    except Exception as exc:
        return CertDialogResult(False, f"无法选中证书行：{exc}", needs_windows=True)
    ok_button = _find_button(dialog, "确定")
    if ok_button is None:
        return CertDialogResult(False, "证书窗口没有确定按钮", needs_windows=True)
    try:
        ok_button.GetInvokePattern().Invoke()
    except Exception as exc:
        return CertDialogResult(False, f"无法点击确定：{exc}", needs_windows=True)
    return CertDialogResult(True, "", matched.row_index, needs_windows=True)


def _read_rows(dialog: object) -> list[CertDialogRow]:
    """把表格读成单位行。列对不上时返回空，调用方就不会点确定。"""
    headers: list[str] = []
    body: list[list[str]] = []
    for item in _walk(dialog):
        control = str(getattr(item, "ControlTypeName", "") or "")
        name = str(getattr(item, "Name", "") or "").strip()
        if control == "HeaderItemControl" and name:
            headers.append(name)
        elif control == "DataItemControl" and name:
            if not body or len(body[-1]) >= max(len(headers), 4):
                body.append([name])
            else:
                body[-1].append(name)
    if not headers:
        return []
    index = {title: pos for pos, title in enumerate(headers)}
    needed = ("颁发给", "单位", "截止日期", "颁发者")
    if any(title not in index for title in needed):
        return []
    rows: list[CertDialogRow] = []
    for cells in body:
        def cell(title: str) -> str:
            pos = index[title]
            return cells[pos] if pos < len(cells) else ""
        rows.append(CertDialogRow(cell("颁发给"), cell("单位"), cell("截止日期"), cell("颁发者")))
    return rows


def _table_rows(dialog: object) -> list[object]:
    """返回可以选择的数据行控件。"""
    return [
        item for item in _walk(dialog)
        if str(getattr(item, "ControlTypeName", "") or "") in {"DataItemControl", "ListItemControl"}
        and hasattr(item, "GetSelectionItemPattern")
    ]


def _find_button(dialog: object, title: str) -> object | None:
    """只在这个证书窗口里找按钮。"""
    for item in _walk(dialog):
        if str(getattr(item, "ControlTypeName", "") or "") != "ButtonControl":
            continue
        if str(getattr(item, "Name", "") or "").strip() == title:
            return item
    return None


def _walk(control: object) -> list[object]:
    """深度优先读子控件。读失败就停在这一支。"""
    found = [control]
    try:
        children = control.GetChildren()
    except Exception:
        return found
    for child in children:
        found.extend(_walk(child))
    return found
