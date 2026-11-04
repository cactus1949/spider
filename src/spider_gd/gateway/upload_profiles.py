"""数据上传配置的读写。

配置整包存在应用设置 ``dataUploadProfiles``。token 和 WiFi 密码只以密文
出现在 JSON 里；接口返回给界面时这两项留空，并用 ``tokenSaved`` /
``wifiPasswordSaved`` 表示已经存过。留空或 ``******`` 表示沿用旧密文。

录制工坊现有上传把 token 放在 ``Authorization: Bearer <token>`` 头里
（见 ``workshop_uploader._normalize_bearer_token``）。这里把它收成可配置的
两种传法：请求头，或查询/表单参数。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from spider_gd.gateway.secret_box import SecretBox, SecretBoxError, box_for_store

SETTINGS_KEY = "dataUploadProfiles"
MASK = "******"
DEFAULT_HEADER_NAME = "Authorization"
DEFAULT_PREFIX = "Bearer "
DEFAULT_PARAM_NAME = "token"


@dataclass
class UploadProfile:
    """一条上传配置。内存里的 token 和 WiFi 密码是明文。

    Args:
        id: 稳定标识，任务配置引用它。
        name: 显示名称。
        url: 上传接口的完整 URL。
        token: 可选凭证。
        token_mode: ``header`` 或 ``param``。
        token_header_name: 头名称，默认 ``Authorization``。
        token_value_prefix: 头的值前缀，默认 ``Bearer ``。参数模式不使用。
        token_param_name: 参数名。
        token_param_in: ``query`` 或 ``form``。
        wifi_ssid: 上传前要连接的 WiFi，可空。
        wifi_password: WiFi 密码，可空。
        wifi_interface: 无线网卡名，可空。

    Examples:
        >>> UploadProfile("up_1", "生产", "https://example.com/upload").token_mode
        'header'
    """

    id: str
    name: str
    url: str
    token: str = ""
    token_mode: str = "header"
    token_header_name: str = DEFAULT_HEADER_NAME
    token_value_prefix: str = DEFAULT_PREFIX
    token_param_name: str = DEFAULT_PARAM_NAME
    token_param_in: str = "query"
    wifi_ssid: str = ""
    wifi_password: str = ""
    wifi_interface: str = ""


def new_profile_id() -> str:
    """生成一条上传配置的 id。

    Returns:
        str: ``up_`` 加 12 位十六进制。

    Examples:
        >>> new_profile_id().startswith("up_")
        True
    """
    return f"up_{uuid.uuid4().hex[:12]}"


def apply_token_prefix(prefix: str, token: str) -> str:
    """按配置拼接头的值；token 已经带同样前缀时不重复加。

    Args:
        prefix: 例如 ``Bearer ``，可空。
        token: 原始 token。

    Returns:
        str: 放进请求头的完整值。空 token 返回空串。

    Examples:
        >>> apply_token_prefix("Bearer ", "abc")
        'Bearer abc'
        >>> apply_token_prefix("Bearer ", "Bearer abc")
        'Bearer abc'
    """
    text = (token or "").strip()
    if not text:
        return ""
    lead = prefix or ""
    if lead and text.lower().startswith(lead.lower()):
        return text
    return f"{lead}{text}"


def load_profiles(store: object, box: SecretBox | None = None) -> list[UploadProfile]:
    """读出全部上传配置，秘密已解密。

    Args:
        store: 带 ``get_setting`` 的状态库。
        box: 加密盒；缺省时按库路径打开。

    Returns:
        list[UploadProfile]: 解密后的配置。坏掉的密文变成空秘密，避免整页打不开。

    Examples:
        >>> class Store:
        ...     def get_setting(self, key):
        ...         return ""
        >>> load_profiles(Store(), SecretBox(b"k" * 32))
        []
    """
    cipher = box or box_for_store(store)
    raw = _read_raw(store)
    profiles: list[UploadProfile] = []
    for item in _parse_raw(raw):
        profiles.append(_from_stored(item, cipher))
    return profiles


def public_profiles(profiles: list[UploadProfile]) -> list[dict[str, Any]]:
    """变成给界面的列表，秘密不出现。

    Args:
        profiles: 已解密的配置。

    Returns:
        list[dict[str, Any]]: 可直接 JSON 返回的字典。

    Examples:
        >>> row = public_profiles([UploadProfile("up_1", "生产", "https://a", token="s")])[0]
        >>> row["token"] == "" and row["tokenSaved"] is True
        True
    """
    return [_public_row(profile) for profile in profiles]


def save_profiles(
    store: object,
    incoming: list[object],
    box: SecretBox | None = None,
) -> list[UploadProfile]:
    """校验并保存上传配置。留空的秘密沿用同 id 的旧值。

    Args:
        store: 带 ``get_setting`` / ``save_settings`` 的状态库。
        incoming: 界面提交的配置列表。
        box: 加密盒。

    Returns:
        list[UploadProfile]: 保存后的明文配置。

    Raises:
        ValueError: 名称、地址或传参方式不合法。

    Examples:
        >>> class Store:
        ...     def __init__(self):
        ...         self.value = ""
        ...     def get_setting(self, key):
        ...         return self.value
        ...     def save_settings(self, settings):
        ...         self.value = settings["dataUploadProfiles"]
        >>> store = Store()
        >>> saved = save_profiles(store, [{"name": "甲", "url": "https://a"}], SecretBox(b"k" * 32))
        >>> saved[0].name
        '甲'
    """
    cipher = box or box_for_store(store)
    previous = {profile.id: profile for profile in load_profiles(store, cipher)}
    saved: list[UploadProfile] = []
    seen: set[str] = set()
    for item in incoming:
        if not isinstance(item, dict):
            raise ValueError("上传配置必须是对象")
        profile = _merge_secret(parse_profile(item), previous.get(_text(item, "id")))
        if profile.id in seen:
            raise ValueError(f"上传配置 id 重复: {profile.id}")
        seen.add(profile.id)
        _validate(profile)
        saved.append(profile)
    payload = json.dumps([_stored_row(profile, cipher) for profile in saved], ensure_ascii=False)
    store.save_settings({SETTINGS_KEY: payload})  # type: ignore[attr-defined]
    return saved


def get_profile(store: object, profile_id: str, box: SecretBox | None = None) -> UploadProfile | None:
    """按 id 取一条已解密的配置。

    Args:
        store: 状态库。
        profile_id: 配置 id。
        box: 加密盒。

    Returns:
        UploadProfile | None: 找不到时返回 None。

    Examples:
        >>> class Store:
        ...     def get_setting(self, key):
        ...         return ""
        >>> get_profile(Store(), "missing", SecretBox(b"k" * 32)) is None
        True
    """
    wanted = (profile_id or "").strip()
    if not wanted:
        return None
    for profile in load_profiles(store, box):
        if profile.id == wanted:
            return profile
    return None


def parse_profile(item: dict[str, Any]) -> UploadProfile:
    """从界面字段解析一条配置，不做秘密合并。

    Args:
        item: 同时接受 camelCase 和 snake_case。

    Returns:
        UploadProfile: 解析结果。id 为空时新生成。

    Examples:
        >>> parse_profile({"name": "甲", "url": "https://a"}).token_header_name
        'Authorization'
    """
    mode = _choice(_text(item, "tokenMode", "token_mode") or "header", {"header", "param"}, "header")
    param_in = _choice(
        _text(item, "tokenParamIn", "token_param_in") or "query",
        {"query", "form"},
        "query",
    )
    prefix = _mapping_value(item, "tokenValuePrefix", "token_value_prefix")
    return UploadProfile(
        id=_text(item, "id") or new_profile_id(),
        name=_text(item, "name"),
        url=_text(item, "url"),
        token=_text(item, "token"),
        token_mode=mode,
        token_header_name=_text(item, "tokenHeaderName", "token_header_name") or DEFAULT_HEADER_NAME,
        token_value_prefix=DEFAULT_PREFIX if prefix is None else str(prefix),
        token_param_name=_text(item, "tokenParamName", "token_param_name") or DEFAULT_PARAM_NAME,
        token_param_in=param_in,
        wifi_ssid=_text(item, "wifiSsid", "wifi_ssid"),
        wifi_password=_text(item, "wifiPassword", "wifi_password"),
        wifi_interface=_text(item, "wifiInterface", "wifi_interface"),
    )


def resolve_submitted_profile(store: object, payload: dict[str, Any]) -> UploadProfile:
    """测试按钮可以只传 id，也可以带上还没保存的草稿。

    草稿里的秘密若留空，且 id 已存在，则沿用库里的密文。

    Args:
        store: 状态库。
        payload: ``{"profileId": "..."}`` 或 ``{"profile": {...}}``。

    Returns:
        UploadProfile: 可直接拿去发请求的配置。

    Raises:
        ValueError: 找不到配置，或草稿不合法。

    Examples:
        >>> class Store:
        ...     def get_setting(self, key):
        ...         return ""
        >>> resolve_submitted_profile(Store(), {"profile": {"name": "甲", "url": "https://a"}}).name
        '甲'
    """
    inline = payload.get("profile")
    profile_id = _text(payload, "profileId", "profile_id")
    if isinstance(inline, dict):
        return _merge_secret(parse_profile(inline), get_profile(store, _text(inline, "id") or profile_id))
    found = get_profile(store, profile_id)
    if found is None:
        raise ValueError("上传配置不存在")
    _validate(found)
    return found


def _merge_secret(profile: UploadProfile, previous: UploadProfile | None) -> UploadProfile:
    """空秘密或掩码沿用旧值。

    Args:
        profile: 本次提交。
        previous: 同 id 的旧配置，可空。

    Returns:
        UploadProfile: 合并后的配置。

    Examples:
        >>> old = UploadProfile("up_1", "甲", "https://a", token="keep")
        >>> _merge_secret(UploadProfile("up_1", "甲", "https://a", token="******"), old).token
        'keep'
    """
    if previous is None:
        profile.token = "" if _is_mask(profile.token) else profile.token
        profile.wifi_password = "" if _is_mask(profile.wifi_password) else profile.wifi_password
        _validate(profile)
        return profile
    if _is_mask(profile.token):
        profile.token = previous.token
    if _is_mask(profile.wifi_password):
        profile.wifi_password = previous.wifi_password
    _validate(profile)
    return profile


def _validate(profile: UploadProfile) -> None:
    """检查必填项和枚举。

    Args:
        profile: 待保存的配置。

    Returns:
        None: 通过时什么都不返回。

    Raises:
        ValueError: 名称、地址或传参方式不合法。

    Examples:
        >>> _validate(UploadProfile("up_1", "甲", "https://a")) is None
        True
    """
    if not profile.name.strip():
        raise ValueError("上传配置名称不能为空")
    if not profile.url.strip():
        raise ValueError(f"上传配置「{profile.name}」的接口 URL 不能为空")
    if not profile.url.lower().startswith(("http://", "https://")):
        raise ValueError(f"上传配置「{profile.name}」的接口 URL 必须以 http:// 或 https:// 开头")
    if profile.token_mode not in {"header", "param"}:
        raise ValueError("token 传递方式只支持 header 或 param")
    if profile.token_mode == "header" and not profile.token_header_name.strip():
        raise ValueError("请求头名称不能为空")
    if profile.token_mode == "param" and not profile.token_param_name.strip():
        raise ValueError("参数名不能为空")
    if profile.token_param_in not in {"query", "form"}:
        raise ValueError("参数位置只支持 query 或 form")


def _from_stored(item: dict[str, Any], box: SecretBox) -> UploadProfile:
    """从落盘 JSON 还原，并解密秘密。

    Args:
        item: 一条已存储的配置。
        box: 加密盒。

    Returns:
        UploadProfile: 明文配置。

    Examples:
        >>> box = SecretBox(b"k" * 32)
        >>> row = {"id": "up_1", "name": "甲", "url": "https://a", "tokenEnc": box.encrypt("s")}
        >>> _from_stored(row, box).token
        's'
    """
    profile = parse_profile(item)
    profile.token = _decrypt_field(box, _text(item, "tokenEnc", "token_enc"))
    profile.wifi_password = _decrypt_field(box, _text(item, "wifiPasswordEnc", "wifi_password_enc"))
    return profile


def _stored_row(profile: UploadProfile, box: SecretBox) -> dict[str, Any]:
    """变成落盘 JSON。秘密只写密文。

    Args:
        profile: 明文配置。
        box: 加密盒。

    Returns:
        dict[str, Any]: 不含明文 token 和 WiFi 密码。

    Examples:
        >>> secret = "plain-token-value"
        >>> row = _stored_row(UploadProfile("up_1", "甲", "https://a", token=secret), SecretBox(b"k" * 32))
        >>> secret not in json.dumps(row)
        True
    """
    return {
        "id": profile.id,
        "name": profile.name.strip(),
        "url": profile.url.strip(),
        "tokenEnc": box.encrypt(profile.token),
        "tokenMode": profile.token_mode,
        "tokenHeaderName": profile.token_header_name.strip() or DEFAULT_HEADER_NAME,
        "tokenValuePrefix": profile.token_value_prefix,
        "tokenParamName": profile.token_param_name.strip() or DEFAULT_PARAM_NAME,
        "tokenParamIn": profile.token_param_in,
        "wifiSsid": profile.wifi_ssid.strip(),
        "wifiPasswordEnc": box.encrypt(profile.wifi_password),
        "wifiInterface": profile.wifi_interface.strip(),
    }


def _public_row(profile: UploadProfile) -> dict[str, Any]:
    """变成界面行。

    Args:
        profile: 明文配置。

    Returns:
        dict[str, Any]: 秘密字段为空。

    Examples:
        >>> _public_row(UploadProfile("up_1", "甲", "https://a")).get("tokenEnc") is None
        True
    """
    return {
        "id": profile.id,
        "name": profile.name,
        "url": profile.url,
        "token": "",
        "tokenSaved": bool(profile.token),
        "tokenMode": profile.token_mode,
        "tokenHeaderName": profile.token_header_name,
        "tokenValuePrefix": profile.token_value_prefix,
        "tokenParamName": profile.token_param_name,
        "tokenParamIn": profile.token_param_in,
        "wifiSsid": profile.wifi_ssid,
        "wifiPassword": "",
        "wifiPasswordSaved": bool(profile.wifi_password),
        "wifiInterface": profile.wifi_interface,
    }


def _decrypt_field(box: SecretBox, value: str) -> str:
    """解密一个字段；失败时当空，避免坏数据拖垮设置页。

    Args:
        box: 加密盒。
        value: 密文。

    Returns:
        str: 明文或空串。

    Examples:
        >>> _decrypt_field(SecretBox(b"k" * 32), "")
        ''
    """
    try:
        return box.decrypt(value)
    except SecretBoxError:
        return ""


def _read_raw(store: object) -> str:
    """读取设置原文。

    Args:
        store: 状态库。

    Returns:
        str: JSON 文本，没有时为空串。

    Examples:
        >>> _read_raw(type("S", (), {"get_setting": lambda self, key: None})())
        ''
    """
    getter = getattr(store, "get_setting", None)
    if not callable(getter):
        return ""
    return str(getter(SETTINGS_KEY) or "")


def _parse_raw(raw: str) -> list[dict[str, Any]]:
    """解析设置 JSON。

    Args:
        raw: 存储文本。

    Returns:
        list[dict[str, Any]]: 配置对象列表。非法内容当空列表。

    Examples:
        >>> _parse_raw("")
        []
    """
    if not raw.strip():
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, dict)]


def _text(item: dict[str, Any], *keys: str) -> str:
    """按候选键取字符串。

    Args:
        item: 源字典。
        *keys: 按顺序尝试的键。

    Returns:
        str: 去掉首尾空白的文本，都没有时为空串。

    Examples:
        >>> _text({"wifiSsid": " a "}, "wifi_ssid", "wifiSsid")
        'a'
    """
    for key in keys:
        if key in item and item[key] is not None:
            return str(item[key]).strip()
    return ""


def _mapping_value(item: dict[str, Any], *keys: str) -> object | None:
    """取原始值，用来区分「没传前缀」和「前缀就是空串」。

    Args:
        item: 源字典。
        *keys: 候选键。

    Returns:
        object | None: 第一个存在的值；都没有返回 None。

    Examples:
        >>> _mapping_value({"tokenValuePrefix": ""}, "tokenValuePrefix")
        ''
    """
    for key in keys:
        if key in item:
            return item[key]
    return None


def _choice(value: str, allowed: set[str], default: str) -> str:
    """把枚举收成允许值。

    Args:
        value: 原始文本。
        allowed: 允许的取值。
        default: 不在集合里时的默认值。

    Returns:
        str: 合法取值。

    Examples:
        >>> _choice("nope", {"header", "param"}, "header")
        'header'
    """
    text = (value or "").strip().lower()
    return text if text in allowed else default


def _is_mask(value: str) -> bool:
    """留空或六个星号都表示「别改已存的秘密」。

    Args:
        value: 界面传来的字段。

    Returns:
        bool: 是否应沿用旧值。

    Examples:
        >>> _is_mask("******")
        True
    """
    return (value or "").strip() in {"", MASK}
