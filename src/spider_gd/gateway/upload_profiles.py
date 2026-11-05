"""数据上传配置的读写。

配置整包存在应用设置 ``dataUploadProfiles``。敏感的请求头、参数和 WiFi
密码只以密文出现在 JSON 里；接口返回给界面时这些值留空，并用 ``saved`` /
``wifiPasswordSaved`` 表示已经存过。留空或 ``******`` 表示沿用旧密文。

旧配置里的 ``tokenMode`` / ``tokenHeaderName`` / ``tokenValuePrefix`` /
``tokenParamName`` / ``tokenParamIn`` / ``tokenEnc`` 在读取时迁成一行请求头
或一行参数（敏感）。请求头模式会把前缀拼进值，且已经带同样前缀时不重复加；
参数模式不加前缀。写回时只存新结构。

值按静态字符串使用，不展开 ``{filename}`` 之类的占位符。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from spider_gd.gateway.secret_box import SecretBox, SecretBoxError, box_for_store

SETTINGS_KEY = "dataUploadProfiles"
MASK = "******"
DEFAULT_HEADER_NAME = "Authorization"
DEFAULT_PREFIX = "Bearer "
DEFAULT_PARAM_NAME = "token"
DEFAULT_FILE_FIELD = "file"
_LEGACY_KEYS = {
    "token",
    "tokenEnc",
    "token_enc",
    "tokenMode",
    "token_mode",
    "tokenHeaderName",
    "token_header_name",
    "tokenValuePrefix",
    "token_value_prefix",
    "tokenParamName",
    "token_param_name",
    "tokenParamIn",
    "token_param_in",
}
_RESERVED_HEADERS = {"content-type", "content-length"}


@dataclass
class UploadHeader:
    """一条自定义请求头。内存里的值是明文。

    Args:
        name: 头名称。
        value: 头的值，按原文发送。
        secret: 为 True 时落盘加密，接口不回明文。
        id: 稳定标识，用来在留空时找回原值。

    Examples:
        >>> UploadHeader("Authorization", "Bearer s", True).secret
        True
    """

    name: str
    value: str = ""
    secret: bool = False
    id: str = ""


@dataclass
class UploadParam:
    """一条自定义参数。内存里的值是明文。

    Args:
        name: 参数名。
        value: 参数值，按原文发送。
        location: ``query`` 或 ``form``。JSON 里的字段名是 ``in``。
        secret: 为 True 时落盘加密，接口不回明文。
        id: 稳定标识，用来在留空时找回原值。

    Examples:
        >>> UploadParam("token", "s", "query", True).location
        'query'
    """

    name: str
    value: str = ""
    location: str = "query"
    secret: bool = False
    id: str = ""


@dataclass
class UploadProfile:
    """一条上传配置。内存里的秘密是明文。

    Args:
        id: 稳定标识，任务配置引用它。
        name: 显示名称。
        url: 上传接口的完整 URL。
        file_field_name: multipart 文件字段名，默认 ``file``。
        headers: 自定义请求头，顺序与配置一致。
        params: 自定义参数。``query`` 合并进 URL，``form`` 作为表单字段。
        wifi_ssid: 上传前要连接的 WiFi，可空。
        wifi_password: WiFi 密码，可空。
        wifi_interface: 无线网卡名，可空。

    Examples:
        >>> UploadProfile("up_1", "生产", "https://example.com/upload").file_field_name
        'file'
    """

    id: str
    name: str
    url: str
    file_field_name: str = DEFAULT_FILE_FIELD
    headers: list[UploadHeader] = field(default_factory=list)
    params: list[UploadParam] = field(default_factory=list)
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


def new_header_id() -> str:
    """生成一条请求头的 id。

    Returns:
        str: ``uh_`` 加 12 位十六进制。

    Examples:
        >>> new_header_id().startswith("uh_")
        True
    """
    return f"uh_{uuid.uuid4().hex[:12]}"


def new_param_id() -> str:
    """生成一条参数的 id。

    Returns:
        str: ``upm_`` 加 12 位十六进制。

    Examples:
        >>> new_param_id().startswith("upm_")
        True
    """
    return f"upm_{uuid.uuid4().hex[:12]}"


def apply_token_prefix(prefix: str, token: str) -> str:
    """按旧配置拼接头的值；token 已经带同样前缀时不重复加。

    只在把旧 token 头迁进自定义请求头时使用。

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
    """读出全部上传配置，秘密已解密。旧 token 字段会迁成新结构。

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
    profiles: list[UploadProfile] = []
    for item in _parse_raw(_read_raw(store)):
        profiles.append(_from_stored(item, cipher))
    return profiles


def public_profiles(profiles: list[UploadProfile]) -> list[dict[str, Any]]:
    """变成给界面的列表，秘密不出现。

    Args:
        profiles: 已解密的配置。

    Returns:
        list[dict[str, Any]]: 可直接 JSON 返回的字典。

    Examples:
        >>> header = UploadHeader("Authorization", "s", True, "uh_1")
        >>> row = public_profiles([UploadProfile("up_1", "生产", "https://a", headers=[header])])[0]
        >>> row["headers"][0]["value"] == "" and row["headers"][0]["saved"] is True
        True
    """
    return [_public_row(profile) for profile in profiles]


def save_profiles(
    store: object,
    incoming: list[object],
    box: SecretBox | None = None,
) -> list[UploadProfile]:
    """校验并保存上传配置。留空的敏感值沿用同 id 的旧值。

    传入旧 token 字段时先迁成新结构，落盘不再写 ``tokenMode`` 等旧键。

    Args:
        store: 带 ``get_setting`` / ``save_settings`` 的状态库。
        incoming: 界面提交的配置列表。
        box: 加密盒。

    Returns:
        list[UploadProfile]: 保存后的明文配置。

    Raises:
        ValueError: 名称、地址或请求头 / 参数不合法。

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
        validate_profile(profile)
        saved.append(profile)
    payload = json.dumps([_stored_row(profile, cipher) for profile in saved], ensure_ascii=False)
    store.save_settings({SETTINGS_KEY: payload})  # type: ignore[attr-defined]
    return saved


def get_profile(
    store: object, profile_id: str, box: SecretBox | None = None
) -> UploadProfile | None:
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

    没有 ``headers`` / ``params`` / ``fileFieldName``、但带旧 token 字段时，
    先迁成对应的一行敏感请求头或参数。

    Args:
        item: 同时接受 camelCase 和 snake_case。

    Returns:
        UploadProfile: 解析结果。id 为空时新生成。

    Raises:
        ValueError: 请求头或参数不是数组，或某一行不是对象。

    Examples:
        >>> parse_profile({"name": "甲", "url": "https://a"}).file_field_name
        'file'
        >>> migrated = parse_profile({"name": "甲", "url": "https://a", "token": "s"})
        >>> migrated.headers[0].value == "Bearer s" and migrated.headers[0].secret
        True
    """
    if _is_legacy_token(item):
        item = _legacy_fields_as_modern(item, _text(item, "token"))
    return UploadProfile(
        id=_text(item, "id") or new_profile_id(),
        name=_text(item, "name"),
        url=_text(item, "url"),
        file_field_name=_file_field_name(item),
        headers=_parse_headers(item.get("headers", [])),
        params=_parse_params(item.get("params", [])),
        wifi_ssid=_text(item, "wifiSsid", "wifi_ssid"),
        wifi_password=_text(item, "wifiPassword", "wifi_password"),
        wifi_interface=_text(item, "wifiInterface", "wifi_interface"),
    )


def resolve_submitted_profile(store: object, payload: dict[str, Any]) -> UploadProfile:
    """测试按钮可以只传 id，也可以带上还没保存的草稿。

    草稿里的敏感值若留空，且 id 已存在，则沿用库里的密文。

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
        found = get_profile(store, _text(inline, "id") or profile_id)
        return _merge_secret(parse_profile(inline), found)
    found = get_profile(store, profile_id)
    if found is None:
        raise ValueError("上传配置不存在")
    validate_profile(found)
    return found


def validate_profile(profile: UploadProfile) -> None:
    """检查必填项、重名和与文件字段冲突的表单参数。

    同名请求头按大小写不敏感判重。同一位置（query 或 form）里的同名参数判重。
    ``Content-Type`` 和 ``Content-Length`` 留给 multipart 自己写。

    Args:
        profile: 待保存或待发送的配置。

    Returns:
        None: 通过时什么都不返回。

    Raises:
        ValueError: 名称、地址、请求头或参数不合法。

    Examples:
        >>> validate_profile(UploadProfile("up_1", "甲", "https://a")) is None
        True
    """
    if not profile.name.strip():
        raise ValueError("上传配置名称不能为空")
    if not profile.url.strip():
        raise ValueError(f"上传配置「{profile.name}」的接口 URL 不能为空")
    if not profile.url.lower().startswith(("http://", "https://")):
        raise ValueError(f"上传配置「{profile.name}」的接口 URL 必须以 http:// 或 https:// 开头")
    file_field = profile.file_field_name.strip()
    _check_entry_name(file_field, "文件字段名")
    seen_headers: set[str] = set()
    seen_header_ids: set[str] = set()
    for header in profile.headers:
        _check_header_name(header.name)
        _remember_id(header.id, seen_header_ids, "请求头")
        folded = header.name.casefold()
        if folded in _RESERVED_HEADERS:
            raise ValueError(f"请求头「{header.name}」由上传请求自动生成，不能自定义")
        if folded in seen_headers:
            raise ValueError(f"请求头名称重复: {header.name}")
        seen_headers.add(folded)
    seen_params: set[tuple[str, str]] = set()
    seen_param_ids: set[str] = set()
    for param in profile.params:
        _check_entry_name(param.name, "参数名称")
        _remember_id(param.id, seen_param_ids, "参数")
        if param.location not in {"query", "form"}:
            raise ValueError(f"参数「{param.name}」的位置只支持 query 或 form")
        key = (param.location, param.name)
        if key in seen_params:
            where = "查询参数" if param.location == "query" else "表单参数"
            raise ValueError(f"{where}名称重复: {param.name}")
        seen_params.add(key)
        if param.location == "form" and param.name == file_field:
            raise ValueError(f"表单参数「{param.name}」与文件字段名冲突")


def _merge_secret(profile: UploadProfile, previous: UploadProfile | None) -> UploadProfile:
    """空敏感值或掩码沿用旧值。取消敏感并留空则写成空字符串。

    Args:
        profile: 本次提交。
        previous: 同 id 的旧配置，可空。

    Returns:
        UploadProfile: 合并后的配置。

    Examples:
        >>> old = UploadProfile(
        ...     "up_1", "甲", "https://a", headers=[UploadHeader("A", "keep", True, "uh_1")]
        ... )
        >>> new = UploadProfile(
        ...     "up_1", "甲", "https://a", headers=[UploadHeader("A", "******", True, "uh_1")]
        ... )
        >>> _merge_secret(new, old).headers[0].value
        'keep'
    """
    if previous is None:
        profile.headers = [_clear_masked_secret(item) for item in profile.headers]
        profile.params = [_clear_masked_secret(item) for item in profile.params]
        profile.wifi_password = "" if _is_mask(profile.wifi_password) else profile.wifi_password
        validate_profile(profile)
        return profile
    profile.headers = _merge_entries(profile.headers, previous.headers)
    profile.params = _merge_entries(profile.params, previous.params)
    if _is_mask(profile.wifi_password):
        profile.wifi_password = previous.wifi_password
    validate_profile(profile)
    return profile


def _merge_entries(
    incoming: list[UploadHeader] | list[UploadParam],
    previous: list[UploadHeader] | list[UploadParam],
) -> list[Any]:
    """按 id 找回敏感值，找不到再按名称（参数还要看位置）。

    Args:
        incoming: 本次提交的行。
        previous: 同配置里上次保存的行。

    Returns:
        list[Any]: 填好敏感值后的行，顺序与本次提交一致。

    Examples:
        >>> old = [UploadHeader("A", "keep", True, "uh_1")]
        >>> new = [UploadHeader("B", "", True, "uh_1")]
        >>> _merge_entries(new, old)[0].value
        'keep'
    """
    by_id = {item.id: item for item in previous if item.id}
    buckets: dict[tuple[str, str, str], list[Any]] = {}
    for item in previous:
        buckets.setdefault(_match_key(item), []).append(item)
    used: set[str] = set()
    for item in incoming:
        if item.secret and _is_mask(item.value):
            prev = _take_previous(item, by_id, buckets, used)
            if prev is not None and prev.secret:
                item.value = prev.value
            elif _is_mask(item.value):
                item.value = ""
    return list(incoming)


def _take_previous(
    item: UploadHeader | UploadParam,
    by_id: dict[str, Any],
    buckets: dict[tuple[str, str, str], list[Any]],
    used: set[str],
) -> Any | None:
    """取一条还没被本次提交用过的旧行。

    Args:
        item: 本次提交的行。
        by_id: 旧行 id 索引。
        buckets: 按名称分组的旧行，会从队列里弹出。
        used: 已经匹配过的旧 id。

    Returns:
        Any | None: 匹配到的旧行；没有则返回 None。

    Examples:
        >>> item = UploadHeader("A", "", True, "uh_1")
        >>> old = UploadHeader("A", "keep", True, "uh_1")
        >>> _take_previous(item, {"uh_1": old}, {}, set()).value
        'keep'
    """
    if item.id and item.id in by_id and item.id not in used:
        used.add(item.id)
        return by_id[item.id]
    bucket = buckets.get(_match_key(item), [])
    while bucket:
        prev = bucket.pop(0)
        if prev.id and prev.id in used:
            continue
        if prev.id:
            used.add(prev.id)
        return prev
    return None


def _match_key(item: UploadHeader | UploadParam) -> tuple[str, str, str]:
    """敏感值的名称匹配键。请求头忽略大小写，参数还区分位置。

    Args:
        item: 请求头或参数。

    Returns:
        tuple[str, str, str]: 种类、名称、位置。请求头的位置为空串。

    Examples:
        >>> _match_key(UploadHeader("Auth", "v"))
        ('header', 'auth', '')
    """
    if isinstance(item, UploadParam):
        return ("param", item.name, item.location)
    return ("header", item.name.casefold(), "")


def _clear_masked_secret(item: UploadHeader | UploadParam) -> UploadHeader | UploadParam:
    """新建配置没有旧值可沿用，掩码当成空。

    Args:
        item: 请求头或参数。

    Returns:
        UploadHeader | UploadParam: 原对象。敏感掩码会被改成空串。

    Examples:
        >>> _clear_masked_secret(UploadHeader("A", "******", True)).value
        ''
    """
    if item.secret and _is_mask(item.value):
        item.value = ""
    return item


def _is_legacy_token(item: dict[str, Any]) -> bool:
    """没有新结构字段、但还有旧 token 字段时需要迁移。

    Args:
        item: 一条配置的原始字典。

    Returns:
        bool: 是否按旧 token 模式读取。

    Examples:
        >>> _is_legacy_token({"tokenMode": "header", "token": "s"})
        True
        >>> _is_legacy_token({"headers": [], "tokenMode": "header"})
        False
    """
    if any(key in item for key in ("headers", "params", "fileFieldName", "file_field_name")):
        return False
    return any(key in item for key in _LEGACY_KEYS)


def _legacy_fields_as_modern(item: dict[str, Any], token: str) -> dict[str, Any]:
    """把旧 token 字段收成一行敏感请求头或参数。

    空 token 不产生行，这样请求和迁移前一样不带凭证。

    Args:
        item: 旧配置。
        token: 已解密的 token，可空。

    Returns:
        dict[str, Any]: 带 ``headers`` / ``params`` / ``fileFieldName`` 的副本。

    Examples:
        >>> row = _legacy_fields_as_modern({"tokenMode": "param", "tokenParamIn": "form"}, "s")
        >>> row["params"][0]["in"] == "form" and row["params"][0]["value"] == "s"
        True
    """
    mode = _choice(
        _text(item, "tokenMode", "token_mode") or "header",
        {"header", "param"},
        "header",
    )
    headers: list[dict[str, Any]] = []
    params: list[dict[str, Any]] = []
    if mode == "param":
        raw = (token or "").strip()
        if raw:
            param_in = _choice(
                _text(item, "tokenParamIn", "token_param_in") or "query",
                {"query", "form"},
                "query",
            )
            params.append(
                {
                    "id": new_param_id(),
                    "name": _text(item, "tokenParamName", "token_param_name") or DEFAULT_PARAM_NAME,
                    "value": raw,
                    "in": param_in,
                    "secret": True,
                }
            )
    else:
        prefix = _mapping_value(item, "tokenValuePrefix", "token_value_prefix")
        prefix_text = DEFAULT_PREFIX if prefix is None else str(prefix)
        value = apply_token_prefix(prefix_text, token)
        if value:
            headers.append(
                {
                    "id": new_header_id(),
                    "name": _text(item, "tokenHeaderName", "token_header_name")
                    or DEFAULT_HEADER_NAME,
                    "value": value,
                    "secret": True,
                }
            )
    modern = dict(item)
    modern["headers"] = headers
    modern["params"] = params
    modern["fileFieldName"] = _text(item, "fileFieldName", "file_field_name") or DEFAULT_FILE_FIELD
    return modern


def _from_stored(item: dict[str, Any], box: SecretBox) -> UploadProfile:
    """从落盘 JSON 还原，并解密秘密。旧结构会先迁移。

    Args:
        item: 一条已存储的配置。
        box: 加密盒。

    Returns:
        UploadProfile: 明文配置。

    Examples:
        >>> box = SecretBox(b"k" * 32)
        >>> header = {
        ...     "id": "uh_1", "name": "Authorization", "valueEnc": box.encrypt("s"), "secret": True
        ... }
        >>> stored = {
        ...     "id": "up_1", "name": "甲", "url": "https://a", "headers": [header], "params": []
        ... }
        >>> _from_stored(stored, box).headers[0].value
        's'
    """
    material = item
    if _is_legacy_token(item):
        token = _decrypt_field(box, _text(item, "tokenEnc", "token_enc"))
        material = _legacy_fields_as_modern(item, token)
    profile = parse_profile(_with_plain_secrets(material, box))
    profile.wifi_password = _decrypt_field(box, _text(item, "wifiPasswordEnc", "wifi_password_enc"))
    return profile


def _with_plain_secrets(item: dict[str, Any], box: SecretBox) -> dict[str, Any]:
    """把落盘行里的密文解成 ``value``，供 :func:`parse_profile` 读取。

    Args:
        item: 一条配置。
        box: 加密盒。

    Returns:
        dict[str, Any]: 副本。敏感行的 ``value`` 已是明文。

    Examples:
        >>> box = SecretBox(b"k" * 32)
        >>> header = {"name": "A", "secret": True, "valueEnc": box.encrypt("s")}
        >>> _with_plain_secrets({"headers": [header]}, box)["headers"][0]["value"]
        's'
    """
    copied = dict(item)
    copied["headers"] = [_decrypt_entry(entry, box) for entry in _as_list(item.get("headers"))]
    copied["params"] = [_decrypt_entry(entry, box) for entry in _as_list(item.get("params"))]
    return copied


def _decrypt_entry(entry: object, box: SecretBox) -> object:
    """解密一行请求头或参数。非敏感行原样保留明文 ``value``。

    Args:
        entry: 一行配置，或不是对象的非法值。
        box: 加密盒。

    Returns:
        object: 字典副本或原样返回的非法值。

    Examples:
        >>> row = {"name": "A", "value": "v", "secret": False}
        >>> _decrypt_entry(row, SecretBox(b"k" * 32))["value"]
        'v'
    """
    if not isinstance(entry, dict):
        return entry
    copied = dict(entry)
    if not _as_bool(copied.get("secret")):
        return copied
    enc = copied.get("valueEnc", copied.get("value_enc"))
    if isinstance(enc, str) and enc != "":
        copied["value"] = _decrypt_field(box, enc)
    elif enc is not None and str(enc).strip() == "":
        copied["value"] = ""
    copied.pop("valueEnc", None)
    copied.pop("value_enc", None)
    return copied


def _stored_row(profile: UploadProfile, box: SecretBox) -> dict[str, Any]:
    """变成落盘 JSON。敏感值只写密文。

    Args:
        profile: 明文配置。
        box: 加密盒。

    Returns:
        dict[str, Any]: 不含明文敏感值和 WiFi 密码。

    Examples:
        >>> secret = "plain-token-value"
        >>> profile = UploadProfile(
        ...     "up_1", "甲", "https://a", headers=[UploadHeader("A", secret, True)]
        ... )
        >>> secret not in json.dumps(_stored_row(profile, SecretBox(b"k" * 32)))
        True
    """
    return {
        "id": profile.id,
        "name": profile.name.strip(),
        "url": profile.url.strip(),
        "fileFieldName": profile.file_field_name.strip() or DEFAULT_FILE_FIELD,
        "headers": [_stored_entry(item, box) for item in profile.headers],
        "params": [_stored_entry(item, box) for item in profile.params],
        "wifiSsid": profile.wifi_ssid.strip(),
        "wifiPasswordEnc": box.encrypt(profile.wifi_password),
        "wifiInterface": profile.wifi_interface.strip(),
    }


def _stored_entry(entry: UploadHeader | UploadParam, box: SecretBox) -> dict[str, Any]:
    """把一行收成落盘对象。

    Args:
        entry: 请求头或参数。
        box: 加密盒。

    Returns:
        dict[str, Any]: 敏感行使用 ``valueEnc``，普通行使用 ``value``。

    Examples:
        >>> row = _stored_entry(UploadHeader("A", "v", False, "uh_1"), SecretBox(b"k" * 32))
        >>> row["value"] == "v" and row["secret"] is False
        True
    """
    row: dict[str, Any] = {"id": entry.id, "name": entry.name, "secret": bool(entry.secret)}
    if isinstance(entry, UploadParam):
        row["in"] = entry.location
    if entry.secret:
        row["valueEnc"] = box.encrypt(entry.value)
    else:
        row["value"] = entry.value
    return row


def _public_row(profile: UploadProfile) -> dict[str, Any]:
    """变成界面行。

    Args:
        profile: 明文配置。

    Returns:
        dict[str, Any]: 敏感值为空，并用 ``saved`` 表示已存。

    Examples:
        >>> row = _public_row(UploadProfile("up_1", "甲", "https://a"))
        >>> row.get("tokenEnc") is None and row["fileFieldName"] == "file"
        True
    """
    return {
        "id": profile.id,
        "name": profile.name,
        "url": profile.url,
        "fileFieldName": profile.file_field_name.strip() or DEFAULT_FILE_FIELD,
        "headers": [_public_entry(item) for item in profile.headers],
        "params": [_public_entry(item) for item in profile.params],
        "wifiSsid": profile.wifi_ssid,
        "wifiPassword": "",
        "wifiPasswordSaved": bool(profile.wifi_password),
        "wifiInterface": profile.wifi_interface,
    }


def _public_entry(entry: UploadHeader | UploadParam) -> dict[str, Any]:
    """变成界面上的一行。敏感值一律留空。

    Args:
        entry: 请求头或参数。

    Returns:
        dict[str, Any]: 含 ``saved``。参数额外带 ``in``。

    Examples:
        >>> _public_entry(UploadParam("token", "s", "form", True, "upm_1"))["saved"]
        True
    """
    secret = bool(entry.secret)
    row: dict[str, Any] = {
        "id": entry.id,
        "name": entry.name,
        "value": "" if secret else entry.value,
        "secret": secret,
        "saved": secret and bool(entry.value),
    }
    if isinstance(entry, UploadParam):
        row["in"] = entry.location
    return row


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


def _parse_headers(raw: object) -> list[UploadHeader]:
    """解析请求头数组。名称和值都空的行丢掉。

    Args:
        raw: ``headers`` 字段。

    Returns:
        list[UploadHeader]: 请求头。

    Raises:
        ValueError: 不是数组，或某一行不是对象。

    Examples:
        >>> _parse_headers([{"name": "A", "value": "v"}])[0].name
        'A'
    """
    rows = _require_rows(raw, "自定义请求头")
    parsed: list[UploadHeader] = []
    for item in rows:
        if not isinstance(item, dict):
            raise ValueError("自定义请求头的每一行必须是对象")
        header = _parse_header(item)
        if header is not None:
            parsed.append(header)
    return parsed


def _parse_params(raw: object) -> list[UploadParam]:
    """解析参数数组。名称和值都空的行丢掉。

    Args:
        raw: ``params`` 字段。

    Returns:
        list[UploadParam]: 参数。

    Raises:
        ValueError: 不是数组，或某一行不是对象。

    Examples:
        >>> _parse_params([{"name": "token", "value": "s", "in": "form"}])[0].location
        'form'
    """
    rows = _require_rows(raw, "自定义参数")
    parsed: list[UploadParam] = []
    for item in rows:
        if not isinstance(item, dict):
            raise ValueError("自定义参数的每一行必须是对象")
        param = _parse_param(item)
        if param is not None:
            parsed.append(param)
    return parsed


def _parse_header(item: dict[str, Any]) -> UploadHeader | None:
    """解析一行请求头。

    Args:
        item: 一行对象。

    Returns:
        UploadHeader | None: 空行返回 None。

    Examples:
        >>> _parse_header({"name": "", "value": ""}) is None
        True
    """
    name = _text(item, "name")
    value = _value_of(item, "value")
    if not name and _is_mask(value):
        return None
    return UploadHeader(
        id=_text(item, "id") or new_header_id(),
        name=name,
        value=value,
        secret=_as_bool(item.get("secret")),
    )


def _parse_param(item: dict[str, Any]) -> UploadParam | None:
    """解析一行参数。

    Args:
        item: 一行对象。

    Returns:
        UploadParam | None: 空行返回 None。

    Raises:
        ValueError: ``in`` 不是 query 或 form。

    Examples:
        >>> _parse_param({"name": "a", "in": "query"}).location
        'query'
    """
    name = _text(item, "name")
    value = _value_of(item, "value")
    if not name and _is_mask(value):
        return None
    return UploadParam(
        id=_text(item, "id") or new_param_id(),
        name=name,
        value=value,
        location=_param_location(item, name),
        secret=_as_bool(item.get("secret")),
    )


def _param_location(item: dict[str, Any], name: str) -> str:
    """读取参数位置。缺省是 query。

    Args:
        item: 参数行。
        name: 参数名，用于错误信息。

    Returns:
        str: ``query`` 或 ``form``。

    Raises:
        ValueError: 位置不是允许值。

    Examples:
        >>> _param_location({}, "token")
        'query'
    """
    raw = _mapping_value(item, "in", "location")
    if raw is None or str(raw).strip() == "":
        return "query"
    location = str(raw).strip().lower()
    if location not in {"query", "form"}:
        label = name or "未命名"
        raise ValueError(f"参数「{label}」的位置只支持 query 或 form")
    return location


def _file_field_name(item: dict[str, Any]) -> str:
    """读取文件字段名。没传时用 ``file``，传了空串则留给校验报错。

    Args:
        item: 配置对象。

    Returns:
        str: 去掉首尾空白后的字段名。

    Examples:
        >>> _file_field_name({})
        'file'
    """
    raw = _mapping_value(item, "fileFieldName", "file_field_name")
    if raw is None:
        return DEFAULT_FILE_FIELD
    return str(raw).strip()


def _require_rows(raw: object, label: str) -> list[object]:
    """确认字段是数组。``None`` 当成空数组。

    Args:
        raw: 原始字段。
        label: 中文名称，写进错误信息。

    Returns:
        list[object]: 行列表。

    Raises:
        ValueError: 不是数组。

    Examples:
        >>> _require_rows(None, "自定义请求头")
        []
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(f"{label}必须是数组")
    return list(raw)


def _as_list(raw: object) -> list[object]:
    """把可能缺失的字段收成列表。类型不对时原样包一层，交给解析报错。

    Args:
        raw: 原始字段。

    Returns:
        list[object]: 行列表。不是列表时返回单元素列表，让后续给出明确错误。

    Examples:
        >>> _as_list(None)
        []
    """
    if raw is None:
        return []
    if isinstance(raw, list):
        return list(raw)
    return [raw]


def _remember_id(entry_id: str, seen: set[str], label: str) -> None:
    """记录一行的 id，重复时报错。空 id 不检查。

    Args:
        entry_id: 行 id。
        seen: 已经见过的 id，会就地更新。
        label: ``请求头`` 或 ``参数``。

    Returns:
        None: 通过时什么都不返回。

    Raises:
        ValueError: 同一个列表里 id 重复。

    Examples:
        >>> seen = set()
        >>> _remember_id("uh_1", seen, "请求头")
        >>> "uh_1" in seen
        True
    """
    if not entry_id:
        return
    if entry_id in seen:
        raise ValueError(f"{label} id 重复: {entry_id}")
    seen.add(entry_id)


def _check_entry_name(name: str, label: str) -> None:
    """拒绝空名称和控制字符，避免把引号写进 multipart。

    Args:
        name: 已经去掉首尾空白的名称。
        label: 中文名称，写进错误信息。

    Returns:
        None: 通过时什么都不返回。

    Raises:
        ValueError: 名称为空或含有非法字符。

    Examples:
        >>> _check_entry_name("file", "文件字段名") is None
        True
    """
    if not name:
        raise ValueError(f"{label}不能为空")
    if any(ord(char) < 32 or char == "\x7f" or char == '"' for char in name):
        raise ValueError(f"{label}「{name}」含有非法字符")


def _check_header_name(name: str) -> None:
    """检查请求头名称。

    Args:
        name: 头名称。

    Returns:
        None: 通过时什么都不返回。

    Raises:
        ValueError: 名称为空、含非法字符、空格或冒号。

    Examples:
        >>> _check_header_name("X-Token") is None
        True
    """
    _check_entry_name(name, "请求头名称")
    if any(char.isspace() for char in name) or ":" in name:
        raise ValueError(f"请求头名称「{name}」不能包含空格或冒号")


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


def _value_of(item: dict[str, Any], *keys: str) -> str:
    """按候选键取原始字符串，不去掉首尾空白。

    Args:
        item: 源字典。
        *keys: 按顺序尝试的键。

    Returns:
        str: 原文字符串；缺失或 None 时为空串。

    Examples:
        >>> _value_of({"value": " Bearer "}, "value")
        ' Bearer '
    """
    for key in keys:
        if key in item and item[key] is not None:
            return str(item[key])
    return ""


def _mapping_value(item: dict[str, Any], *keys: str) -> object | None:
    """取原始值，用来区分「没传」和「传了空串」。

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


def _as_bool(value: object) -> bool:
    """把 JSON 里的敏感标记收成布尔值。

    Args:
        value: 原始值。

    Returns:
        bool: 真值。无法识别时为 False。

    Examples:
        >>> _as_bool("true")
        True
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return False


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
