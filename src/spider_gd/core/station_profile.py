"""场站绑定：一把 UKey 对应一个 WiFi 和一个证书密码。"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass

_KEEP_SECRETS = {"", "******", "***"}


@dataclass(frozen=True)
class StationProfile:
    """一把 UKey 对应的场站、WiFi 和证书密码。

    Args:
        id: 场站编号，保存后保持不变。
        name: 场站名称，选择窗口里显示这一列。
        wifi_ssid: 该场站专用 WiFi 名称。
        wifi_password: WiFi 密码；空字符串表示尚未设置。
        cert_password: 证书密码；空字符串表示尚未设置。
        device_id: 绑定时选中的 USB 设备编号；空字符串表示手填场站名。
        org_name: 单位名称关键字，用来模糊匹配证书窗口「单位」列。

    Examples:
        >>> profile = StationProfile("a", "阳江", "YJ-UKEY", "wifi", "pin")
        >>> profile.wifi_ssid
        'YJ-UKEY'
    """

    id: str
    name: str
    wifi_ssid: str
    wifi_password: str = ""
    cert_password: str = ""
    device_id: str = ""
    org_name: str = ""


def parse_station_profiles(raw: str) -> list[StationProfile]:
    """把设置里的 JSON 文本解析成场站列表。

    Args:
        raw: ``stationProfiles`` 的 JSON 文本；空字符串表示还没有场站。

    Returns:
        list[StationProfile]: 按原顺序排列的场站。名称为空的行会被丢掉。

    Raises:
        ValueError: JSON 不合法、根节点不是数组，或名称、编号重复。

    Examples:
        >>> parse_station_profiles("")
        []
        >>> parse_station_profiles('[{"id":"a","name":"阳江","wifiSsid":"YJ"}]')[0].name
        '阳江'
    """
    text = (raw or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("场站列表不是合法 JSON") from exc
    if not isinstance(data, list):
        raise ValueError("场站列表必须是数组")
    profiles: list[StationProfile] = []
    used_ids: set[str] = set()
    used_names: set[str] = set()
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError("场站项必须是对象")
        profile = _profile_from_mapping(item, index, used_ids)
        if not profile.name:
            continue
        if profile.name in used_names:
            raise ValueError(f"场站名称重复：{profile.name}")
        used_names.add(profile.name)
        profiles.append(profile)
    return profiles


def dump_station_profiles(profiles: list[StationProfile]) -> str:
    """把场站列表写成可放进设置表的 JSON。

    Args:
        profiles: 要保存的场站。

    Returns:
        str: UTF-8 JSON 文本，中文不转义。

    Examples:
        >>> "阳江" in dump_station_profiles([StationProfile("a", "阳江", "YJ")])
        True
    """
    rows = [
        {
            "id": profile.id,
            "name": profile.name,
            "wifiSsid": profile.wifi_ssid,
            "wifiPassword": profile.wifi_password,
            "certPassword": profile.cert_password,
            "deviceId": profile.device_id,
            "orgName": profile.org_name,
        }
        for profile in profiles
    ]
    return json.dumps(rows, ensure_ascii=False, separators=(",", ":"))


def mask_station_profiles(raw: str) -> str:
    """把场站 JSON 里的 WiFi 密码和证书密码换成占位符。

    Args:
        raw: 已保存的场站 JSON。非法文本会变成空数组，避免把原文退回给界面。

    Returns:
        str: 密码已遮盖的 JSON。原本为空的密码仍然为空。

    Examples:
        >>> raw = '[{"id":"a","name":"阳江","wifiPassword":"secret","certPassword":"pin"}]'
        >>> masked = mask_station_profiles(raw)
        >>> "secret" not in masked and "pin" not in masked
        True
    """
    try:
        profiles = parse_station_profiles(raw)
    except ValueError:
        return "[]"
    masked = [
        StationProfile(
            id=profile.id,
            name=profile.name,
            wifi_ssid=profile.wifi_ssid,
            wifi_password=_mask_secret(profile.wifi_password),
            cert_password=_mask_secret(profile.cert_password),
            device_id=profile.device_id,
            org_name=profile.org_name,
        )
        for profile in profiles
    ]
    return dump_station_profiles(masked)


def merge_station_profiles(incoming_raw: str, existing_raw: str) -> str:
    """用新列表覆盖场站，但保留界面没有重填的密码。

    Args:
        incoming_raw: 界面提交的 JSON。密码为空白或 ``******`` 时表示沿用旧值。
        existing_raw: 数据库里现有的 JSON。

    Returns:
        str: 合并后的 JSON。传入列表里没有的场站会被删除。

    Raises:
        ValueError: 任一端 JSON 无法解析。

    Examples:
        >>> saved = dump_station_profiles([StationProfile("a", "阳江", "YJ", "wifi", "pin")])
        >>> edited = (
        ...     '[{"id":"a","name":"阳江","wifiSsid":"YJ-2",'
        ...     '"wifiPassword":"******","certPassword":""}]'
        ... )
        >>> merged = parse_station_profiles(merge_station_profiles(edited, saved))[0]
        >>> (merged.wifi_ssid, merged.wifi_password, merged.cert_password)
        ('YJ-2', 'wifi', 'pin')
    """
    incoming = parse_station_profiles(incoming_raw)
    existing = {profile.id: profile for profile in parse_station_profiles(existing_raw)}
    merged: list[StationProfile] = []
    for profile in incoming:
        old = existing.get(profile.id)
        merged.append(
            StationProfile(
                id=profile.id,
                name=profile.name,
                wifi_ssid=profile.wifi_ssid,
                wifi_password=_keep_secret(profile.wifi_password, old.wifi_password if old else ""),
                cert_password=_keep_secret(profile.cert_password, old.cert_password if old else ""),
                device_id=profile.device_id,
                org_name=profile.org_name,
            )
        )
    return dump_station_profiles(merged)


def demo_station_profiles() -> list[StationProfile]:
    """返回两把示例 UKey，供选择窗口演练。

    示例密码只存在于本机演练，不会写进真实登录流程。

    Returns:
        list[StationProfile]: 阳江、阳西两个场站。

    Examples:
        >>> [item.name for item in demo_station_profiles()]
        ['阳江明阳海上风电', '阳西海上风电']
    """
    return [
        StationProfile(
            id="demo-yangjiang",
            name="阳江明阳海上风电",
            wifi_ssid="YJ-UKEY",
            wifi_password="demo-yj-wifi",
            cert_password="demo-yj-pin",
        ),
        StationProfile(
            id="demo-yangxi",
            name="阳西海上风电",
            wifi_ssid="YX-UKEY",
            wifi_password="demo-yx-wifi",
            cert_password="demo-yx-pin",
        ),
    ]


def profiles_for_window(
    profiles: list[StationProfile],
    *,
    force_window: bool,
) -> tuple[list[StationProfile], bool]:
    """不足两个场站却要打开窗口时，改用示例场站。

    Args:
        profiles: 用户已经配置的场站。
        force_window: 为 True 时表示这次一定要弹出选择窗口。

    Returns:
        tuple[list[StationProfile], bool]: 实际展示的场站，以及是否换成了示例数据。

    Examples:
        >>> shown, used_demo = profiles_for_window([], force_window=True)
        >>> used_demo and len(shown) == 2
        True
    """
    if force_window and len(profiles) < 2:
        return demo_station_profiles(), True
    return list(profiles), False


def same_ukey(saved_code: str, plugged_code: str) -> bool:
    """判断设置里的特征码和当前插入的设备是不是同一把。

    Args:
        saved_code: 设置里保存的特征码。
        plugged_code: 当前插入设备的实例编号。

    Returns:
        bool: 完全相同，或其中较长的一段包含另一段时为 True。短于 4 位的片段不做包含匹配。

    Examples:
        >>> same_ukey("SN1-PORT", "USB\\\\VID_096E\\\\SN1-PORT")
        True
        >>> same_ukey("A", "USB\\\\VID_096E\\\\OTHER")
        False
    """
    saved = saved_code.strip()
    plugged = plugged_code.strip()
    if not saved or not plugged:
        return False
    return saved == plugged or (
        len(saved) >= 4 and (saved in plugged or plugged in saved)
    )


def rows_for_inserted_ukeys(
    profiles: list[StationProfile],
    plugged: list[tuple[str, str]],
) -> tuple[list[StationProfile], bool]:
    """按当前插入的 UKey 生成任务前要确认的场站行。

    特征码能对上设置时，行里用设置中的场站名称。对不上时保留设备自己的名字，
    方便先看见这把钥匙，再到设置里补场站名。

    Args:
        profiles: 设置里的场站。
        plugged: 当前 UKey，每项是 ``(特征码, 设备显示名)``。

    Returns:
        tuple[list[StationProfile], bool]: 场站行，以及是否要弹出多选窗口。
        插入两把及以上才弹窗。

    Examples:
        >>> bound = StationProfile("a", "阳江明阳", "YJ", device_id="SN-YJ")
        >>> rows, popup = rows_for_inserted_ukeys(
        ...     [bound], [("SN-YJ", "USB 输入设备"), ("SN-YX", "USB 输入设备 · 2")]
        ... )
        >>> popup and rows[0].name == "阳江明阳" and rows[1].name.startswith("USB")
        True
    """
    rows: list[StationProfile] = []
    used: set[str] = set()
    for device_id, label in plugged:
        code = device_id.strip()
        if not code:
            continue
        matched = next(
            (profile for profile in profiles if same_ukey(profile.device_id, code)),
            None,
        )
        if matched is not None and matched.id not in used:
            used.add(matched.id)
            rows.append(matched)
            continue
        rows.append(
            StationProfile(
                id=f"usb:{code}",
                name=label.strip() or code,
                wifi_ssid="",
                device_id=code,
            )
        )
    return rows, len(rows) >= 2


def station_run_plan(profile: StationProfile, fallback_cert_password: str = "") -> dict[str, str]:
    """整理一个场站执行时要用的名称、WiFi 和证书密码。

    Args:
        profile: 选中的场站。
        fallback_cert_password: 场站没单独填证书密码时沿用的全局证书密码。

    Returns:
        dict[str, str]: 含 ``name``、``wifi_ssid``、``wifi_password``、``cert_password``。

    Examples:
        >>> plan = station_run_plan(StationProfile("a", "阳江", "YJ", "", "pin"), "global")
        >>> plan["cert_password"]
        'pin'
        >>> station_run_plan(StationProfile("a", "阳江", "YJ"), "global")["cert_password"]
        'global'
    """
    cert_password = profile.cert_password.strip() or fallback_cert_password.strip()
    return {
        "id": profile.id,
        "name": profile.name,
        "wifi_ssid": profile.wifi_ssid,
        "wifi_password": profile.wifi_password,
        "cert_password": cert_password,
        "device_id": profile.device_id,
        "org_name": profile.org_name,
    }


def normalize_org_name(text: str) -> str:
    """把单位名称收成用于比较的文本。

    去掉空白和括号，并把证书窗口末尾的省略号去掉。

    Args:
        text: 配置的单位关键字，或证书窗口「单位」列的原文。

    Returns:
        str: 折叠后的文本。

    Examples:
        >>> normalize_org_name("三峡新能源发电（韶关浈江）有...")
        '三峡新能源发电韶关浈江有'
    """
    value = unicodedata.normalize("NFKC", text or "")
    value = value.replace("（", "").replace("）", "").replace("(", "").replace(")", "")
    value = re.sub(r"\s+", "", value)
    value = re.sub(r"(?:\.{3}|…)+$", "", value)
    return value.strip(".")


def org_name_matches(keyword: str, unit_text: str) -> bool:
    """判断配置的单位关键字能否对上证书窗口里的单位。

    关键字至少两个字。完整单位、或被省略号截断的单位，只要一边包含另一边就算匹配。

    Args:
        keyword: 场站配置的单位名称，例如 ``阳江``、``韶关浈江``。
        unit_text: 证书窗口「单位」列的文字，可以带省略号。

    Returns:
        bool: 能对上时为 True。

    Examples:
        >>> org_name_matches("阳江", "三峡新能源阳江发电有限公司")
        True
        >>> org_name_matches("韶关浈江", "三峡新能源发电（韶关浈江）有...")
        True
        >>> org_name_matches("浈江", "三峡新能源发电（韶关浈江）有...")
        True
        >>> org_name_matches("阳江", "三峡新能源发电（韶关浈江）有...")
        False
    """
    key = normalize_org_name(keyword)
    unit = normalize_org_name(unit_text)
    if len(key) < 2 or len(unit) < 2:
        return False
    return key in unit or unit in key


def pick_certificate_row(
    keyword: str,
    rows: list[dict[str, str]],
) -> dict[str, str] | None:
    """在证书表里按单位名称选出最吻合的一行。

    表头对应证书窗口的「颁发给、单位、截止日期、颁发者」。多行都能对上时，
    取得分更高的一行；得分相同则保留表里更靠前的一行。

    Args:
        keyword: 场站配置的单位名称。
        rows: 证书行。单位文字放在 ``单位`` 或 ``org``。

    Returns:
        dict[str, str] | None: 命中的那一行。没有命中时为 None。

    Examples:
        >>> rows = [
        ...     {"颁发给": "甲", "单位": "三峡新能源阳江发电有限公司"},
        ...     {"颁发给": "乙", "单位": "三峡新能源发电（韶关浈江）有..."},
        ... ]
        >>> pick_certificate_row("浈江", rows)["颁发给"]
        '乙'
    """
    best: dict[str, str] | None = None
    best_score = 0
    key = normalize_org_name(keyword)
    for row in rows:
        unit_text = str(row.get("单位") or row.get("org") or "")
        if not org_name_matches(keyword, unit_text):
            continue
        unit = normalize_org_name(unit_text)
        score = len(key) if key in unit else len(unit)
        if best is None or score > best_score:
            best = row
            best_score = score
    return best


def find_station(profiles: list[StationProfile], choice: str) -> StationProfile | None:
    """按编号或名称找到场站。

    Args:
        profiles: 候选场站。
        choice: 场站编号或完整名称。

    Returns:
        StationProfile | None: 命中的第一项；没有匹配时为 None。

    Examples:
        >>> found = find_station(demo_station_profiles(), "阳西海上风电")
        >>> found is not None and found.wifi_ssid
        'YX-UKEY'
    """
    key = choice.strip()
    if not key:
        return None
    for profile in profiles:
        if profile.id == key or profile.name == key:
            return profile
    return None


def public_station(profile: StationProfile) -> dict[str, str]:
    """提取可以回给界面的场站信息。

    Args:
        profile: 已选中的场站。

    Returns:
        dict[str, str]: 编号、名称和 WiFi 名称，不含任何密码。

    Examples:
        >>> row = public_station(StationProfile("a", "阳江", "YJ", "secret", "pin"))
        >>> sorted(row)
        ['id', 'name', 'wifiSsid']
    """
    return {"id": profile.id, "name": profile.name, "wifiSsid": profile.wifi_ssid}


def _mask_secret(value: str) -> str:
    """把非空密码显示成占位符。

    Args:
        value: 原始密码。

    Returns:
        str: 空密码保持空，其余一律为 ``******``。

    Examples:
        >>> _mask_secret("pin")
        '******'
        >>> _mask_secret("")
        ''
    """
    if not value.strip():
        return ""
    return "******"


def _keep_secret(incoming: str, existing: str) -> str:
    """空白或占位符沿用已保存密码。

    Args:
        incoming: 界面这次提交的密码。
        existing: 数据库里的密码。

    Returns:
        str: 应写入数据库的密码。

    Examples:
        >>> _keep_secret("******", "pin")
        'pin'
        >>> _keep_secret("new", "pin")
        'new'
    """
    if incoming.strip() in _KEEP_SECRETS:
        return existing
    return incoming


def _as_text(value: object) -> str:
    """把 JSON 值收成去掉首尾空白的文本。

    Args:
        value: 任意 JSON 值。

    Returns:
        str: ``None`` 视为空字符串。

    Examples:
        >>> _as_text(" 阳江 ")
        '阳江'
    """
    if value is None:
        return ""
    return str(value).strip()


def _raw_secret(value: object) -> str:
    """读取密码原文，不去掉中间空格。

    Args:
        value: JSON 中的密码字段。

    Returns:
        str: 字符串形式的密码。

    Examples:
        >>> _raw_secret(None)
        ''
    """
    if value is None:
        return ""
    return str(value)


def _mapping_value(item: dict[str, object], *keys: str) -> object:
    """按候选键读取映射中的第一个已有值。

    Args:
        item: 一条场站 JSON。
        *keys: 按优先级排列的字段名。

    Returns:
        object: 命中的值；都不存在时为空字符串。

    Examples:
        >>> _mapping_value({"wifi_ssid": "YJ"}, "wifiSsid", "wifi_ssid")
        'YJ'
    """
    for key in keys:
        if key in item and item[key] is not None:
            return item[key]
    return ""


def _unique_id(preferred: str, used: set[str], index: int) -> str:
    """得到一个尚未使用的场站编号。

    Args:
        preferred: 调用方给出的编号；空则按序号生成。
        used: 已经占用的编号，函数会把新编号加进去。
        index: 当前行号，从 0 开始。

    Returns:
        str: 可用编号。

    Raises:
        ValueError: 调用方主动提交了重复编号。

    Examples:
        >>> _unique_id("", set(), 0)
        'st-1'
    """
    if preferred and preferred in used:
        raise ValueError(f"场站编号重复：{preferred}")
    base = preferred or f"st-{index + 1}"
    candidate = base
    suffix = 2
    while candidate in used:
        candidate = f"{base}-{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def _profile_from_mapping(
    item: dict[str, object],
    index: int,
    used_ids: set[str],
) -> StationProfile:
    """从一条 JSON 对象构造场站；名称为空时不分配编号。

    Args:
        item: 一条场站 JSON。
        index: 行号，从 0 开始。
        used_ids: 已占用编号。

    Returns:
        StationProfile: 构造结果。名称为空时编号为空，调用方应丢弃该行。

    Raises:
        ValueError: 编号重复。

    Examples:
        >>> row = _profile_from_mapping({"name": "阳江", "wifiSsid": "YJ"}, 0, set())
        >>> row.id
        'st-1'
    """
    name = _as_text(_mapping_value(item, "name"))
    if not name:
        return StationProfile(id="", name="", wifi_ssid="")
    profile_id = _unique_id(_as_text(_mapping_value(item, "id")), used_ids, index)
    return StationProfile(
        id=profile_id,
        name=name,
        wifi_ssid=_as_text(_mapping_value(item, "wifiSsid", "wifi_ssid")),
        wifi_password=_raw_secret(_mapping_value(item, "wifiPassword", "wifi_password")),
        cert_password=_raw_secret(_mapping_value(item, "certPassword", "cert_password")),
        device_id=_as_text(_mapping_value(item, "deviceId", "device_id")),
        org_name=_as_text(_mapping_value(item, "orgName", "org_name", "单位")),
    )
