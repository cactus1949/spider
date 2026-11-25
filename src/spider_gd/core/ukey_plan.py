"""决定一次任务要用哪些已保存场站。

编辑任务时的候选是场站设置里的完整列表，跟钥匙插没插无关。
这里只在执行时判断当前能不能跑。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from spider_gd.core.station_profile import (
    StationProfile,
    normalize_org_name,
    org_name_matches,
    same_ukey,
    station_run_plan,
)


@dataclass(frozen=True)
class KnownCert:
    """证书库里的一张证书，以及现在能不能用它的私钥。

    Args:
        thumbprint: 证书指纹。续期后会变，只用来消歧，不拿来合并两把钥匙。
        org_name: 单位列原文。
        available: 当前能在不弹 PIN 的前提下用到私钥。拔掉后只剩缓存时为 False。

    Examples:
        >>> KnownCert("aa", "阳江公司", True).available
        True
    """

    thumbprint: str
    org_name: str
    available: bool


@dataclass
class StationDecision:
    """一个场站在这次执行里的去向。"""

    id: str
    name: str
    status: str
    reason: str = ""
    plan: dict[str, str] | None = None


@dataclass
class TaskUKeyPlan:
    """执行前的场站计划。

    Args:
        started: 为 False 时不要开始采集。skipped 里是完整原因。
        runnable: 按顺序要跑的场站。
        skipped: 没跑的场站和原因。
        cancelled: 用户关掉了本次选择窗口。
    """

    started: bool
    runnable: list[dict[str, str]] = field(default_factory=list)
    skipped: list[StationDecision] = field(default_factory=list)
    cancelled: bool = False


DialogOpener = Callable[[list[StationProfile], float], list[str] | None]


def editor_ukey_options(stations: Sequence[StationProfile]) -> list[dict[str, str]]:
    """任务编辑下拉里的场站。已保存的都在，不看当前插了没有。

    Args:
        stations: 场站设置里的完整列表。

    Returns:
        list[dict[str, str]]: ``id``、``name``、``orgName``。没有单位时 ``orgName`` 为空。

    Examples:
        >>> rows = editor_ukey_options([
        ...     StationProfile("a", "蕴阳", "W", org_name="三峡"),
        ... ])
        >>> rows[0]["orgName"]
        '三峡'
    """
    options: list[dict[str, str]] = []
    for station in stations:
        if not station.name.strip() or not station.id.strip():
            continue
        options.append({
            "id": station.id,
            "name": station.name.strip(),
            "orgName": station.org_name.strip(),
        })
    return options


def kept_task_ukeys(
    saved_ids: Sequence[str],
    stations: Sequence[StationProfile],
) -> list[str]:
    """编辑保存时保留仍在场站设置里的选择。

    不因为钥匙没插就删掉。设置里已经删掉的场站不再保留。

    Args:
        saved_ids: 任务上已选的场站编号。
        stations: 场站设置里的完整列表。

    Returns:
        list[str]: 仍能对上设置的编号，顺序不变。

    Examples:
        >>> stations = [StationProfile("a", "蕴阳", "W")]
        >>> kept_task_ukeys(["a", "missing"], stations)
        ['a']
    """
    known = {station.id for station in stations if station.id}
    return [item for item in saved_ids if item in known]


def select_available_cert(
    certs: Sequence[KnownCert],
    keyword: str,
    thumbprint: str = "",
) -> tuple[KnownCert | None, str]:
    """在当前可用证书里为场站找唯一一张。

    缓存但私钥不可用的证书不算可用。同一单位在证书库里出现两张可用证书时，
    仍视为这一站可以跑，证书窗口再按单位确认唯一一行。关键字同时对上多个不同单位时不选。

    Args:
        certs: 证书库读到的证书。
        keyword: 场站上的单位关键字。
        thumbprint: 场站指定的指纹。空表示只按单位。

    Returns:
        tuple[KnownCert | None, str]: 找到时原因为空。找不到时说明原因。

    Examples:
        >>> cert, reason = select_available_cert(
        ...     [KnownCert("aa", "阳江公司", False)], "阳江",
        ... )
        >>> cert is None and "不在当前钥匙" in reason
        True
        >>> picked, why = select_available_cert(
        ...     [KnownCert("aa", "阳江公司", True), KnownCert("bb", "阳江公司", True)],
        ...     "阳江",
        ... )
        >>> picked is not None and why == ""
        True
    """
    wanted = thumbprint.strip().lower()
    available = [item for item in certs if item.available]
    if wanted:
        live = [item for item in available if item.thumbprint.lower() == wanted]
        if len(live) == 1:
            return live[0], ""
        cached = [
            item for item in certs
            if not item.available and item.thumbprint.lower() == wanted
        ]
        if cached:
            return None, "证书不在当前钥匙上"
        return None, "没有匹配的可用证书"
    hits = [item for item in available if org_name_matches(keyword, item.org_name)]
    distinct = {
        normalize_org_name(item.org_name)
        for item in hits
        if normalize_org_name(item.org_name)
    }
    if len(distinct) > 1:
        return None, "单位关键字匹配了多个不同单位"
    if len(hits) >= 1:
        return hits[0], ""
    cached_org = [
        item for item in certs
        if not item.available and org_name_matches(keyword, item.org_name)
    ]
    if cached_org:
        return None, "证书不在当前钥匙上"
    return None, "没有匹配的可用证书"


def station_block_reason(
    station: StationProfile,
    present_device_ids: Sequence[str],
    certs: Sequence[KnownCert],
    *,
    certs_checked: bool = True,
) -> str:
    """这个场站现在为什么不能跑。能跑时返回空字符串。

    配了单位就只认当前可用证书，不用另一张证书顶上。
    没配单位时，只认绑定的设备还在不在。

    Args:
        station: 场站设置里的一行。
        present_device_ids: 当前实体钥匙的实例编号。
        certs: 证书库里的证书，含是否可用。

    Returns:
        str: 不能跑的原因。能跑时为 ``""``。

    Examples:
        >>> station = StationProfile("a", "蕴阳", "W", device_id="SN-A", org_name="蕴阳")
        >>> station_block_reason(station, [], [KnownCert("aa", "其他公司", True)])
        '没有匹配的可用证书'
    """
    device_here = bool(station.device_id) and any(
        same_ukey(station.device_id, item) for item in present_device_ids
    )
    if station.org_name.strip() and certs_checked:
        cert, reason = select_available_cert(
            certs, station.org_name, getattr(station, "cert_thumbprint", "") or "",
        )
        if cert is None:
            return reason
        return ""
    if station.device_id:
        return "" if device_here else "当前未插入"
    if station.org_name.strip():
        return "未能确认当前可用证书"
    return "未绑定单位或钥匙"


def plan_task_execution(
    stations: Sequence[StationProfile],
    *,
    selected_ids: Sequence[str] | None,
    present_device_ids: Sequence[str],
    certs: Sequence[KnownCert],
    mode: str,
    fallback_cert_password: str = "",
    opener: DialogOpener | None = None,
    timeout_sec: float = 180,
    certs_checked: bool = True,
) -> TaskUKeyPlan:
    """按任务是否已选场站、以及手动还是定时，排出本次要跑的场站。

    已选的场站没插上就跳过，不用别的证书代替。一个都对不上就不开始采集。
    没选时，手动遇到多把可跑场站才弹窗，而且选择不写回任务；定时把当前可跑的都按顺序跑，不弹窗。

    Args:
        stations: 场站设置里的完整列表。
        selected_ids: 任务保存的场站编号。空序列表示任务没选。
        present_device_ids: 当前实体钥匙。
        certs: 当前证书，含是否可用。
        mode: ``manual`` 或 ``scheduled``。
        fallback_cert_password: 场站没填证书密码时沿用的全局密码。
        opener: 手动多选窗口。定时不会调用。
        timeout_sec: 窗口等待秒数。

    Returns:
        TaskUKeyPlan: 要跑的、跳过的，以及要不要开始采集。

    Examples:
        >>> stations = [
        ...     StationProfile("a", "蕴阳", "W", device_id="SN-A", org_name="蕴阳"),
        ...     StationProfile("b", "阳江", "Y", device_id="SN-B", org_name="阳江"),
        ... ]
        >>> certs = [KnownCert("aa", "蕴阳公司", True)]
        >>> plan = plan_task_execution(
        ...     stations, selected_ids=["a", "b"], present_device_ids=["SN-A"],
        ...     certs=certs, mode="scheduled",
        ... )
        >>> [item["name"] for item in plan.runnable]
        ['蕴阳']
        >>> plan.skipped[0].reason
        '没有匹配的可用证书'
    """
    by_id = {station.id: station for station in stations if station.id}
    chosen = [item.strip() for item in (selected_ids or []) if str(item).strip()]
    if chosen:
        return _plan_selected(
            chosen, by_id, present_device_ids, certs, fallback_cert_password,
            certs_checked=certs_checked,
        )
    if not stations:
        return TaskUKeyPlan(started=True)
    ready = [
        station for station in stations
        if station_block_reason(
            station, present_device_ids, certs, certs_checked=certs_checked,
        ) == ""
    ]
    if not ready:
        return TaskUKeyPlan(started=True, skipped=[
            StationDecision(station.id, station.name, "skipped", station_block_reason(
                station, present_device_ids, certs, certs_checked=certs_checked,
            ))
            for station in stations
        ])
    if mode == "scheduled":
        return TaskUKeyPlan(
            started=True,
            runnable=[station_run_plan(item, fallback_cert_password) for item in ready],
        )
    if len(ready) == 1:
        return TaskUKeyPlan(
            started=True,
            runnable=[station_run_plan(ready[0], fallback_cert_password)],
        )
    if opener is None:
        return TaskUKeyPlan(
            started=False,
            skipped=[StationDecision("", "", "skipped", "需要选择本次场站")],
        )
    picked = opener(ready, timeout_sec)
    if picked is None:
        return TaskUKeyPlan(started=False, cancelled=True)
    runnable: list[dict[str, str]] = []
    for station_id in picked:
        station = by_id.get(station_id)
        if station is None:
            return TaskUKeyPlan(started=False, cancelled=False, skipped=[
                StationDecision(station_id, station_id, "skipped", "选择了未知场站"),
            ])
        runnable.append(station_run_plan(station, fallback_cert_password))
    if not runnable:
        return TaskUKeyPlan(
            started=False,
            skipped=[StationDecision("", "", "skipped", "本次没有选择场站")],
        )
    return TaskUKeyPlan(started=True, runnable=runnable)


def _plan_selected(
    chosen: Sequence[str],
    by_id: dict[str, StationProfile],
    present_device_ids: Sequence[str],
    certs: Sequence[KnownCert],
    fallback_cert_password: str,
    *,
    certs_checked: bool,
) -> TaskUKeyPlan:
    """按任务里保存的场站编号过滤当前能跑的。"""
    runnable: list[dict[str, str]] = []
    skipped: list[StationDecision] = []
    for station_id in chosen:
        station = by_id.get(station_id)
        if station is None:
            skipped.append(StationDecision(station_id, station_id, "skipped", "场站已不在设置中"))
            continue
        reason = station_block_reason(
            station, present_device_ids, certs, certs_checked=certs_checked,
        )
        if reason:
            skipped.append(StationDecision(station.id, station.name, "skipped", reason))
            continue
        runnable.append(station_run_plan(station, fallback_cert_password))
    return TaskUKeyPlan(started=bool(runnable), runnable=runnable, skipped=skipped)


def format_station_report(rows: Sequence[StationDecision | Mapping[str, object]]) -> str:
    """把场站结果收成执行日志里的几行。

    Args:
        rows: ``StationDecision`` 或含 ``name``、``status``、``reason`` 的字典。

    Returns:
        str: 多行文本。没有场站时为空字符串。

    Examples:
        >>> format_station_report([StationDecision("a", "蕴阳", "skipped", "当前未插入")])
        '场站 蕴阳：未执行（当前未插入）'
    """
    lines: list[str] = []
    for item in rows:
        if isinstance(item, StationDecision):
            name, status, reason = item.name, item.status, item.reason
        else:
            name = str(item.get("name") or "")
            status = str(item.get("status") or "")
            reason = str(item.get("reason") or "")
        if status == "ran":
            lines.append(f"场站 {name}：已执行")
        elif status == "failed":
            lines.append(f"场站 {name or '未命名'}：失败（{reason or '执行失败'}）")
        elif status == "cancelled":
            lines.append(f"场站 {name or '未命名'}：已中断（{reason or '用户取消，已完成项保留'}）")
        elif name or reason:
            lines.append(f"场站 {name or '未命名'}：未执行（{reason or '未执行'}）")
    return "\n".join(lines)


def configured_ukey_ids(cfg: dict | None) -> list[str]:
    """读出任务配置里保存的场站编号。

    Args:
        cfg: 任务配置。没有这个字段时返回空列表。

    Returns:
        list[str]: 去重后仍保持原来的顺序。

    Examples:
        >>> configured_ukey_ids({"ukey_station_ids": ["a", "a", " b "]})
        ['a', 'b']
        >>> configured_ukey_ids(None)
        []
    """
    if not isinstance(cfg, dict):
        return []
    raw = cfg.get("ukey_station_ids") or []
    if isinstance(raw, str):
        import json

        text = raw.strip()
        if not text:
            return []
        try:
            raw = json.loads(text)
        except json.JSONDecodeError:
            return []
    if not isinstance(raw, list):
        return []
    found: list[str] = []
    for item in raw:
        text = str(item).strip()
        if text and text not in found:
            found.append(text)
    return found


def should_reuse_login(*, station_count: int, auth_state: str) -> bool:
    """一次要跑多个场站时，不把当前页面当成已经登录。

    Args:
        station_count: 这次要执行的场站数。没有场站队列时为 1。
        auth_state: ``check_auth`` 返回的状态。

    Returns:
        bool: 可以跳过登录流程时为 True。

    Examples:
        >>> should_reuse_login(station_count=2, auth_state="authenticated")
        False
        >>> should_reuse_login(station_count=1, auth_state="authenticated")
        True
    """
    if station_count > 1:
        return False
    return auth_state == "authenticated"


def login_page_can_be_skipped(force_login: bool, auth_state: str) -> bool:
    """切场站时，上一站的工作台还开着也不能当成这一站已登录。

    Args:
        force_login: 这场站必须重新走登录。
        auth_state: 当前页面看起来的登录状态。

    Returns:
        bool: 可以跳过登录表单时为 True。

    Examples:
        >>> login_page_can_be_skipped(True, "authenticated")
        False
        >>> login_page_can_be_skipped(False, "authenticated")
        True
    """
    if force_login:
        return False
    return auth_state == "authenticated"


def station_followup(
    *,
    user_cancelled: bool,
    station_failed: bool,
    more_stations: bool,
) -> str:
    """决定一个场站结束后，后面的场站还跑不跑。

    用户取消才停下。某一站登录失败或出错，后面还有站就继续。

    Args:
        user_cancelled: 用户点了停止，或等待时取消。
        station_failed: 这一站没有跑完。
        more_stations: 队列里还有没开始的场站。

    Returns:
        str: ``stop`` 不再跑后面的站；``continue`` 这一站失败但继续；
        ``next`` 这一站成功并且后面还有站；``finish`` 到此结束。

    Examples:
        >>> station_followup(user_cancelled=False, station_failed=True, more_stations=True)
        'continue'
        >>> station_followup(user_cancelled=True, station_failed=True, more_stations=True)
        'stop'
    """
    if user_cancelled:
        return "stop"
    if station_failed and more_stations:
        return "continue"
    if more_stations:
        return "next"
    return "finish"


def collect_runtime_plan(
    stations: Sequence[StationProfile],
    *,
    selected_ids: Sequence[str] | None,
    mode: str,
    fallback_cert_password: str = "",
    opener: DialogOpener | None = None,
) -> TaskUKeyPlan:
    """读取当前钥匙和证书，再排本次要跑的场站。

    证书可用性探测失败时，不把证书库里的缓存当成已插入。

    Args:
        stations: 场站设置。
        selected_ids: 任务保存的场站。空表示没选。
        mode: ``manual`` 或 ``scheduled``。
        fallback_cert_password: 全局证书密码。
        opener: 手动多选窗口。定时不使用。

    Returns:
        TaskUKeyPlan: 执行计划。
    """
    present: list[str] = []
    try:
        from spider_gd.infrastructure.network.usb_devices import list_usb_devices

        present = [
            device_id
            for item in list_usb_devices() if item.ukey_candidate
            for device_id in (item.device_id, *item.member_ids)
        ]
    except Exception:
        present = []
    certs: list[KnownCert] = []
    checked = False
    try:
        from spider_gd.infrastructure.network.usb_devices import _list_certificates
        from spider_gd.infrastructure.windows.cert_availability import silent_thumbprints

        live = silent_thumbprints()
        checked = live is not None
        live_set = {item.lower() for item in (live or set())}
        for row in _list_certificates():
            thumb = str(row.get("thumbprint") or "")
            certs.append(KnownCert(
                thumb,
                str(row.get("organization") or ""),
                checked and thumb.lower() in live_set,
            ))
    except Exception:
        certs = []
        checked = False
    return plan_task_execution(
        stations,
        selected_ids=selected_ids,
        present_device_ids=present,
        certs=certs,
        mode=mode,
        fallback_cert_password=fallback_cert_password,
        opener=opener,
        certs_checked=checked,
    )
