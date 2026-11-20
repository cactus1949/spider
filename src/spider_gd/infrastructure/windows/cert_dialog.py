"""原生证书选择窗口：只在唯一一行对上单位之后才点确定。

Windows 上的点击要到现场用 UI Automation 验收。这里的匹配可以在任何系统上测。
"""

from __future__ import annotations

import contextlib
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


_COLUMNS = ("颁发给", "单位", "截止日期", "颁发者")


def assemble_certificate_rows(
    headers: list[str],
    row_cells: list[list[str]],
) -> list[CertDialogRow]:
    """按证书窗口的四列表头把单元格收成行。

    缺任何一列表头时返回空列表，调用方就不能点确定。

    Args:
        headers: 表头原文，顺序与窗口一致。
        row_cells: 每一行的单元格文字。

    Returns:
        list[CertDialogRow]: 能对上四列的行。

    Examples:
        >>> rows = assemble_certificate_rows(
        ...     ["颁发给", "单位", "截止日期", "颁发者"],
        ...     [["甲", "单位甲公司", "2031-01-01", "NETCA"]],
        ... )
        >>> rows[0].org_name
        '单位甲公司'
        >>> assemble_certificate_rows(["颁发给"], [["甲"]])
        []
    """
    index = {title: pos for pos, title in enumerate(headers)}
    if any(title not in index for title in _COLUMNS):
        return []
    rows: list[CertDialogRow] = []
    for cells in row_cells:
        def cell(title: str, cells: list[str] = cells) -> str:
            pos = index[title]
            return cells[pos] if pos < len(cells) else ""

        if not any(cell(title).strip() for title in _COLUMNS):
            continue
        rows.append(CertDialogRow(
            cell("颁发给"), cell("单位"), cell("截止日期"), cell("颁发者"),
        ))
    return rows


def apply_dialog_choice(
    rows: list[CertDialogRow],
    keyword: str,
    select,
    selected,
    invoke_ok,
    invoke_cancel,
) -> CertDialogResult:
    """选中唯一一行，确认它仍然选中，再点确定。

    对不上、有多行、或选中没有保持时，点取消把窗口放掉，不点确定。

    Args:
        rows: 已经读出的证书行。
        keyword: 场站单位关键字。
        select: 按行号选中。
        selected: 返回这一行现在是不是仍被选中。
        invoke_ok: 点击确定。
        invoke_cancel: 点击取消。

    Returns:
        CertDialogResult: 只有确定已经被点下时 ``ok`` 为 True。

    Examples:
        >>> rows = [
        ...     CertDialogRow("甲", "单位甲公司", "2031-01-01", "NETCA"),
        ...     CertDialogRow("乙", "单位乙公司", "2031-01-01", "NETCA"),
        ... ]
        >>> chosen = {}
        >>> result = apply_dialog_choice(
        ...     rows, "单位甲",
        ...     lambda index: chosen.__setitem__("index", index),
        ...     lambda index: chosen.get("index") == index,
        ...     lambda: chosen.__setitem__("ok", True),
        ...     lambda: chosen.__setitem__("cancel", True),
        ... )
        >>> result.ok and chosen["ok"] and not chosen.get("cancel")
        True
    """
    matched = match_dialog_row(rows, keyword)

    def cancel() -> None:
        try:
            invoke_cancel()
        except Exception:
            return

    if matched.row_index is None:
        cancel()
        return CertDialogResult(False, matched.reason or "证书窗口无法唯一确认")
    try:
        select(matched.row_index)
    except Exception as exc:
        cancel()
        return CertDialogResult(False, f"无法选中证书行：{exc}")
    try:
        held = bool(selected(matched.row_index))
    except Exception as exc:
        cancel()
        return CertDialogResult(False, f"无法确认证书行已选中：{exc}")
    if not held:
        cancel()
        return CertDialogResult(False, "证书行没有保持选中")
    try:
        invoke_ok()
    except Exception as exc:
        cancel()
        return CertDialogResult(False, f"无法点击确定：{exc}")
    return CertDialogResult(True, "", matched.row_index)


class CertificateWatch:
    """在登录点击的同时看证书窗口。

    点击若被原生窗口挡住，这个监视在另一条线程里选中唯一行并点确定，
    点击才能返回。窗口没出现就结束，不额外空等一整段登录时间。
    """

    def __init__(
        self,
        keyword: str,
        thumbprint: str = "",
        *,
        probe=None,
        interval: float = 0.8,
        sleeper=None,
        clock=None,
        cancelled=None,
        timeout: float = 20.0,
        after_click: float = 2.0,
    ):
        import threading
        import time

        self.keyword = keyword
        self.thumbprint = thumbprint
        self._probe = probe or (lambda: confirm_certificate(keyword, thumbprint))
        self._interval = interval
        self._sleeper = sleeper or time.sleep
        self._clock = clock or time.monotonic
        self._cancelled = cancelled
        self._timeout = timeout
        self._after_click = after_click
        self._click_finished = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.result: CertDialogResult | None = None

    def start(self) -> None:
        """开始监视。必须在等待登录点击之前调用。"""
        import threading

        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="cert-dialog-watch", daemon=True)
        self._thread.start()

    def mark_click_finished(self) -> None:
        """登录点击已经返回。之后只再短暂看一眼窗口。"""
        if not hasattr(self, "_click_seen_at"):
            self._click_seen_at = self._clock()
        self._click_finished.set()

    def finish(self, timeout: float | None = None) -> CertDialogResult:
        """等到监视结束。"""
        if self._thread is not None:
            self._thread.join(self._timeout if timeout is None else timeout)
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        if self.result is not None:
            return self.result
        return CertDialogResult(False, "", absent=True)

    def stop(self) -> None:
        """不再等待。已经点过确定的结果保留。"""
        self._stop.set()
        self.mark_click_finished()

    def _run(self) -> None:
        started = self._clock()
        deadline = started + self._timeout
        while not self._stop.is_set():
            if self._cancelled and self._cancelled():
                self.result = CertDialogResult(False, "已取消")
                return
            try:
                result = self._probe()
            except Exception as exc:
                result = CertDialogResult(False, f"读取证书窗口失败：{exc}", absent=True)
            if result.needs_windows and result.absent:
                self.result = result
                return
            if result.absent:
                click_done = self._click_finished.is_set() and hasattr(self, "_click_seen_at")
                if click_done and self._clock() >= self._click_seen_at + self._after_click:
                    self.result = result
                    return
                if self._clock() >= deadline:
                    self.result = result
                    return
                self._sleeper(self._interval)
                continue
            self.result = result
            return
        if self.result is None:
            self.result = CertDialogResult(False, "", absent=True)


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
    """读当前证书窗口。读不到、对不上或选不中，就点取消，不点确定。"""
    try:
        import uiautomation as auto
    except Exception as exc:
        return CertDialogResult(
            False, f"缺少 UI Automation：{exc}", needs_windows=True, absent=True,
        )
    dialog = _find_dialog(auto.GetRootControl())
    if dialog is None:
        # 窗口还没出现。needs_windows 留给「这台系统看不了窗口」，
        # 不能把「还没弹出来」标成看不了，否则监视会立刻停。
        return CertDialogResult(False, "", absent=True)
    rows = _read_rows(dialog)
    selectable = _selectable_rows(dialog)

    def cancel() -> None:
        button = _find_button(dialog, "取消")
        if button is not None:
            button.GetInvokePattern().Invoke()

    if not rows or len(selectable) != len(rows):
        with contextlib.suppress(Exception):
            cancel()
        reason = "证书窗口没有读到单位列" if not rows else "证书窗口行对不上"
        return CertDialogResult(False, reason, needs_windows=True)

    def select(index: int) -> None:
        selectable[index].GetSelectionItemPattern().Select()

    def selected(index: int) -> bool:
        pattern = selectable[index].GetSelectionItemPattern()
        flag = getattr(pattern, "IsSelected", None)
        if callable(flag):
            flag = flag()
        return bool(flag)

    def ok() -> None:
        button = _find_button(dialog, "确定")
        if button is None:
            raise RuntimeError("证书窗口没有确定按钮")
        button.GetInvokePattern().Invoke()

    result = apply_dialog_choice(rows, keyword, select, selected, ok, cancel)
    return CertDialogResult(
        result.ok, result.reason, result.row_index, needs_windows=True,
    )


def _find_dialog(root: object) -> object | None:
    """优先找标题里带「证书选择」的窗口。"""
    fallback = None
    try:
        children = root.GetChildren()
    except Exception:
        return None
    for window in children:
        name = str(getattr(window, "Name", "") or "")
        if "证书选择" in name:
            return window
        if fallback is None and "证书" in name:
            fallback = window
    return fallback


def _read_rows(dialog: object) -> list[CertDialogRow]:
    """把表格读成单位行。列对不上时返回空，调用方就不会点确定。"""
    headers = [
        str(getattr(item, "Name", "") or "").strip()
        for item in _walk(dialog)
        if str(getattr(item, "ControlTypeName", "") or "") == "HeaderItemControl"
        and str(getattr(item, "Name", "") or "").strip()
    ]
    list_rows = [item for item in _walk(dialog) if _is_list_row(item)]
    if list_rows:
        return assemble_certificate_rows(headers, [_row_texts(item) for item in list_rows])
    cells = [
        str(getattr(item, "Name", "") or "").strip()
        for item in _walk(dialog)
        if str(getattr(item, "ControlTypeName", "") or "") == "DataItemControl"
        and str(getattr(item, "Name", "") or "").strip()
    ]
    width = len(headers)
    if width < 4 or not cells:
        return []
    grouped = [cells[start:start + width] for start in range(0, len(cells), width)]
    return assemble_certificate_rows(headers, grouped)


def _is_list_row(item: object) -> bool:
    """列表行本身可以选择。单元格不是行。"""
    if str(getattr(item, "ControlTypeName", "") or "") != "ListItemControl":
        return False
    try:
        item.GetSelectionItemPattern()
    except Exception:
        return False
    return True


def _row_texts(item: object) -> list[str]:
    """一行里按从左到右收集文字。行自己的名字通常是「颁发给」。"""
    texts: list[str] = []
    own = str(getattr(item, "Name", "") or "").strip()
    if own:
        texts.append(own)
    for child in _walk(item)[1:]:
        kind = str(getattr(child, "ControlTypeName", "") or "")
        if kind in {"ListItemControl", "HeaderControl", "HeaderItemControl"}:
            continue
        name = str(getattr(child, "Name", "") or "").strip()
        if name and kind in {"TextControl", "EditControl", "DataItemControl", "CustomControl"}:
            texts.append(name)
    return texts


def _selectable_rows(dialog: object) -> list[object]:
    """返回与读出的行同一顺序、可以选中的列表行。"""
    rows = [item for item in _walk(dialog) if _is_list_row(item)]
    if rows:
        return rows
    return _table_rows(dialog)


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
