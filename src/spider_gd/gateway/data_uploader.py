"""按上传配置把文件 POST 到客户接口。

协议是 ``multipart/form-data``。一次请求只带一个文件，字段名来自配置，默认 ``file``。
恰好一个普通文件时原样上传；多个文件、任意文件夹，或文件与文件夹混选时，先打成
一个 zip 再上传。自定义请求头、查询参数和表单字段按配置原样带上。失败后自动再试
3 次（连第一次一共 4 次）。配置了 WiFi 时先切过去，已经连着目标网络就不切；上传完
再切回原来的网络。
"""

from __future__ import annotations

import asyncio
import re
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from spider_gd.gateway.upload_profiles import (
    UploadHeader,  # noqa: F401  doctest 里要拼请求头
    UploadProfile,
    validate_profile,
)
from spider_gd.gateway.workshop_uploader import _failure_message, _upload_response_ok
from spider_gd.infrastructure.network.wifi_connector import (
    WifiConnectOptions,
    WifiConnector,
    WifiConnectResult,
)

#: 第一次失败后再自动重试的次数。
AUTO_RETRY_TIMES = 3
DEFAULT_ATTEMPTS = AUTO_RETRY_TIMES + 1
TEST_FILE_NAME = "spider-gd-upload-test.txt"
TEST_FILE_BODY = b"spider-gd upload test\n"


class WifiPort(Protocol):
    """WiFi 切换端口，测试里可以换成假实现。"""

    def current_ssid(self, interface_name: str = "") -> str:
        """读取当前 SSID。

        Args:
            interface_name: 网卡名，可空。

        Returns:
            str: 当前网络名，没有连接时为空串。
        """

    def connect(self, options: WifiConnectOptions) -> WifiConnectResult:
        """连接到指定网络。

        Args:
            options: SSID、密码和网卡。

        Returns:
            WifiConnectResult: 连接结果。
        """


ProgressCallback = Callable[[dict[str, Any]], None]


@dataclass
class PreparedFile:
    """一个即将放进 multipart 的文件。

    一次上传只有这一份。多个来源打成 zip 时，``note`` 写明打包了几项。

    Args:
        source: 用户选中的原始路径。多项打包时用换行拼在一起。
        name: 上传时使用的文件名。
        path: 实际读取的本地文件。
        kind: ``file`` 或 ``zip``。
        note: 打包说明。单个普通文件为空。

    Examples:
        >>> PreparedFile("/a", "a.txt", Path("a.txt"), "file").kind
        'file'
        >>> PreparedFile("/a", "a.zip", Path("a.zip"), "zip", "已打包 2 项").note
        '已打包 2 项'
    """

    source: str
    name: str
    path: Path
    kind: str
    note: str = ""


@dataclass
class WifiHop:
    """一次「切到上传网络」的结果，供上传后切回。

    Args:
        ok: 是否可以开始上传。
        skipped: 没有配置 WiFi，或已经连在目标上。
        already_connected: 当前就是目标 WiFi。
        switched: 这次确实发起了切换。
        original_ssid: 切换前的网络，可能为空。
        message: 给人看的说明。
        interface_name: 使用的网卡名。

    Examples:
        >>> WifiHop(True, True, True, False, "office", "已连接目标 WiFi", "").already_connected
        True
    """

    ok: bool
    skipped: bool
    already_connected: bool
    switched: bool
    original_ssid: str
    message: str
    interface_name: str
    details: list[str] = field(default_factory=list)


@dataclass
class UploadJobResult:
    """一次上传的最终结果。

    Args:
        ok: 接口是否接受了文件。
        message: 成功或失败原因。
        attempts: 实际请求次数。
        files: 每个文件的最终状态。

    Examples:
        >>> UploadJobResult(True, "上传成功", 1, []).ok
        True
    """

    ok: bool
    message: str
    attempts: int
    files: list[dict[str, Any]]
    wifi_message: str = ""
    restore_message: str = ""
    exec_id: str = ""


def prepare_upload_files(paths: list[str | Path], work_dir: Path) -> tuple[list[PreparedFile], list[dict[str, Any]]]:
    """把选中路径收成至多一个待上传文件。

    恰好一个普通文件、且没有文件夹时，原样上传。其余情况（多个文件、任意文件夹、
    文件和文件夹混选）打成一个 zip。缺失路径记为失败，不中断其余项。

    Args:
        paths: 文件或文件夹路径。
        work_dir: 存放临时 zip 的目录，调用方负责清理。

    Returns:
        tuple[list[PreparedFile], list[dict[str, Any]]]: 可上传的文件（0 或 1 个），
        以及无法处理的项（状态已经是 ``failed``）。

    Examples:
        >>> prepared, failed = prepare_upload_files([], Path("."))
        >>> prepared == [] and failed == []
        True
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    failed: list[dict[str, Any]] = []
    valid: list[Path] = []
    seen: set[Path] = set()
    for raw in paths:
        path = Path(raw)
        try:
            key = path.resolve()
        except OSError:
            key = path
        if key in seen:
            continue
        seen.add(key)
        source = str(path)
        label = path.name or source
        if not path.exists():
            failed.append(_file_state(source, label, "failed", "文件不存在"))
            continue
        if not path.is_file() and not path.is_dir():
            failed.append(_file_state(source, label, "failed", "不是文件或文件夹"))
            continue
        valid.append(path)
    items = _top_level_entries(valid)
    if not items:
        return [], failed
    if len(items) == 1 and items[0].is_file():
        item = items[0]
        return [PreparedFile(str(item), item.name or "upload", item, "file")], failed
    # 多项里的版本清单、登录态不进包。滤掉之后若只剩一个普通文件，仍按单文件上传。
    bundle = [
        item
        for item in items
        if not (item.is_file() and _skip_packed_file(item.parent, item, explicit=True))
    ]
    if not bundle:
        for item in items:
            failed.append(
                _file_state(str(item), item.name or str(item), "failed", "没有可上传的文件")
            )
        return [], failed
    if len(bundle) == 1 and bundle[0].is_file():
        item = bundle[0]
        return [PreparedFile(str(item), item.name or "upload", item, "file")], failed

    taken = {entry.name for entry in work_dir.iterdir()}
    archive_name = _unique_name(_bundle_zip_name(bundle[0].name, len(bundle)), taken)
    archive = work_dir / archive_name
    written = _zip_selection(bundle, archive)
    if written == 0:
        for item in bundle:
            failed.append(
                _file_state(str(item), item.name or str(item), "failed", "没有可上传的文件")
            )
        return [], failed
    note = f"已打包 {len(bundle)} 项"
    source = str(bundle[0]) if len(bundle) == 1 else "\n".join(str(item) for item in bundle)
    return [PreparedFile(source, archive_name, archive, "zip", note)], failed


def build_request_parts(profile: UploadProfile) -> tuple[str, dict[str, str], dict[str, str], dict[str, str]]:
    """按配置拼出 URL、请求头、查询参数和表单字段。

    查询参数会合并进 URL 里已有的 query，同名时以配置为准。表单字段留在 data 里，
    和 multipart 文件一起发出。值是静态字符串，不做占位符替换。

    Args:
        profile: 上传配置。

    Returns:
        tuple[str, dict[str, str], dict[str, str], dict[str, str]]:
        合并后的 URL、headers、params、data。

    Raises:
        ValueError: 请求头或参数重名，或和文件字段冲突。

    Examples:
        >>> profile = UploadProfile(
        ...     "up_1", "甲", "https://a/up",
        ...     headers=[UploadHeader("Authorization", "Bearer s")],
        ... )
        >>> url, headers, params, data = build_request_parts(profile)
        >>> url == "https://a/up" and headers["Authorization"] == "Bearer s"
        True
        >>> params == {} and data == {}
        True
    """
    validate_profile(profile)
    headers = {item.name: item.value for item in profile.headers}
    params: dict[str, str] = {}
    data: dict[str, str] = {}
    for item in profile.params:
        if item.location == "form":
            data[item.name] = item.value
        else:
            params[item.name] = item.value
    return _with_params(profile.url.strip(), params), headers, params, data


def switch_to_upload_wifi(profile: UploadProfile, port: WifiPort, interface_name: str = "") -> WifiHop:
    """上传前确保连上配置里的 WiFi。已经连着就直接返回。

    Args:
        profile: 上传配置。
        port: WiFi 端口。
        interface_name: 配置没写网卡时的回退网卡名。

    Returns:
        WifiHop: ``already_connected`` 时 ``message`` 为「已连接目标 WiFi」。

    Examples:
        >>> class Port:
        ...     def current_ssid(self, interface_name=""):
        ...         return "office"
        ...     def connect(self, options):
        ...         raise AssertionError("不应切换")
        >>> profile = UploadProfile("up_1", "甲", "https://a", wifi_ssid="office")
        >>> switch_to_upload_wifi(profile, Port()).message
        '已连接目标 WiFi'
    """
    interface = (profile.wifi_interface or interface_name or "").strip()
    target = profile.wifi_ssid.strip()
    if not target:
        return WifiHop(True, True, False, False, "", "未配置 WiFi，不切换", interface)
    current = (port.current_ssid(interface) or "").strip()
    if current == target:
        return WifiHop(True, True, True, False, current, "已连接目标 WiFi", interface)
    result = port.connect(
        WifiConnectOptions(
            ssid=target,
            password=profile.wifi_password,
            interface_name=interface,
        )
    )
    if not result.ok:
        return WifiHop(
            False,
            False,
            False,
            False,
            current,
            result.message or result.error or "连接目标 WiFi 失败",
            interface,
            list(result.details),
        )
    return WifiHop(
        True,
        False,
        False,
        True,
        current,
        result.message or f"已切换到目标 WiFi: {target}",
        interface,
        list(result.details),
    )


def restore_original_wifi(hop: WifiHop, port: WifiPort) -> WifiHop:
    """上传结束后切回原来的 WiFi。已经在目标上时不动。

    切回不带密码，沿用系统里已经保存的网络配置。

    Args:
        hop: :func:`switch_to_upload_wifi` 的结果。
        port: WiFi 端口。

    Returns:
        WifiHop: 切回结果。没切过网络时 ``ok`` 仍为 True。

    Examples:
        >>> hop = WifiHop(True, True, True, False, "office", "已连接目标 WiFi", "")
        >>> restore_original_wifi(hop, None).message  # doctest: +ELLIPSIS
        '已连接目标 WiFi，无需切回'
    """
    if hop.already_connected or not hop.switched:
        message = "已连接目标 WiFi，无需切回" if hop.already_connected else "未切换 WiFi，无需切回"
        return WifiHop(True, True, hop.already_connected, False, hop.original_ssid, message, hop.interface_name)
    original = hop.original_ssid.strip()
    if not original:
        return WifiHop(True, True, False, False, "", "上传前没有连接 WiFi，不切回", hop.interface_name)
    result = port.connect(
        WifiConnectOptions(ssid=original, password="", interface_name=hop.interface_name)
    )
    if not result.ok:
        return WifiHop(
            False,
            False,
            False,
            True,
            original,
            result.message or result.error or "切回原 WiFi 失败",
            hop.interface_name,
            list(result.details),
        )
    return WifiHop(
        True,
        False,
        False,
        True,
        original,
        result.message or f"已切回原 WiFi: {original}",
        hop.interface_name,
        list(result.details),
    )


def probe_wifi_switch(profile: UploadProfile, port: WifiPort, interface_name: str = "") -> dict[str, Any]:
    """测试切换：已连接就只提示；否则切过去再切回来。

    Args:
        profile: 上传配置。
        port: WiFi 端口。
        interface_name: 网卡回退名。

    Returns:
        dict[str, Any]: ``ok``、``message``、``alreadyConnected``、``restored``。

    Examples:
        >>> class Port:
        ...     def current_ssid(self, interface_name=""):
        ...         return "office"
        ...     def connect(self, options):
        ...         raise AssertionError("不应切换")
        >>> profile = UploadProfile("up_1", "甲", "https://a", wifi_ssid="office")
        >>> probe_wifi_switch(profile, Port())["alreadyConnected"]
        True
    """
    if not profile.wifi_ssid.strip():
        return {
            "ok": False,
            "message": "这条配置没有填写 WiFi 名",
            "alreadyConnected": False,
            "restored": False,
            "details": [],
        }
    hop = switch_to_upload_wifi(profile, port, interface_name)
    if not hop.ok:
        return {
            "ok": False,
            "message": hop.message,
            "alreadyConnected": False,
            "restored": False,
            "details": hop.details,
        }
    if hop.already_connected:
        return {
            "ok": True,
            "message": "已连接目标 WiFi",
            "alreadyConnected": True,
            "restored": False,
            "details": hop.details,
        }
    restored = restore_original_wifi(hop, port)
    message = hop.message if restored.ok else f"{hop.message}；切回失败: {restored.message}"
    return {
        "ok": restored.ok,
        "message": message,
        "alreadyConnected": False,
        "restored": restored.ok,
        "details": [*hop.details, *restored.details],
    }


async def upload_prepared_files(
    profile: UploadProfile,
    files: list[PreparedFile],
    *,
    client: httpx.AsyncClient | None = None,
    max_attempts: int = DEFAULT_ATTEMPTS,
    retry_delay_sec: float = 0.5,
    on_progress: ProgressCallback | None = None,
) -> UploadJobResult:
    """把准备好的文件打成一个 multipart 请求发出去，失败自动重试。

    Args:
        profile: 上传配置。
        files: :func:`prepare_upload_files` 的结果。
        client: 可注入的 HTTP 客户端。
        max_attempts: 总尝试次数，默认 4（首次加 3 次重试）。
        retry_delay_sec: 两次尝试之间的等待，单位秒。
        on_progress: 每次状态变化时回调。

    Returns:
        UploadJobResult: 全部文件共用这一次请求的成败。

    Examples:
        >>> UploadProfile("up_1", "甲", "https://a").url.startswith("https://")
        True
    """
    attempts = max(1, int(max_attempts))
    states = [
        _file_state(item.source, item.name, "ready", _status_message("等待上传", item.note))
        for item in files
    ]
    if not files:
        return UploadJobResult(False, "没有可上传的文件", 0, states)

    def emit(phase: str, message: str, ok: bool | None, attempt: int = 0) -> None:
        if on_progress is None:
            return
        done = sum(1 for item in states if item["status"] == "success")
        on_progress(
            {
                "event": "upload_progress",
                "phase": phase,
                "message": message,
                "ok": ok,
                "attempt": attempt,
                "done": done,
                "total": len(states),
                "percent": _percent(phase, done, len(states), attempt, attempts, ok),
                "files": [dict(item) for item in states],
            }
        )

    url, headers, params, data = build_request_parts(profile)
    owns = client is None
    client = client or httpx.AsyncClient(timeout=httpx.Timeout(60.0))
    result = UploadJobResult(False, "上传失败", 0, states)
    try:
        for attempt in range(1, attempts + 1):
            status = "uploading" if attempt == 1 else "retrying"
            note = "正在上传" if attempt == 1 else f"第 {attempt - 1}/{AUTO_RETRY_TIMES} 次重试"
            for item, state in zip(files, states, strict=True):
                state["status"] = status
                state["attempt"] = attempt
                state["message"] = _status_message(note, item.note)
            emit("upload", note, None, attempt)
            try:
                response_ok, message, _payload = await _post_once(
                    client,
                    url,
                    headers,
                    params,
                    data,
                    files,
                    profile.file_field_name,
                )
            except Exception as exc:
                response_ok = False
                message = str(exc)
            result = UploadJobResult(response_ok, message, attempt, states)
            if response_ok:
                for item, state in zip(files, states, strict=True):
                    state["status"] = "success"
                    state["message"] = _status_message("上传成功", item.note)
                emit("done", "上传成功", True, attempt)
                return result
            if attempt < attempts:
                for item in states:
                    item["status"] = "retrying"
                    item["message"] = message
                emit("retry", message, None, attempt)
                if retry_delay_sec > 0:
                    await asyncio.sleep(retry_delay_sec)
        for item in states:
            item["status"] = "failed"
            item["message"] = result.message
        emit("done", result.message, False, result.attempts)
        return result
    finally:
        if owns:
            await client.aclose()


async def run_upload(
    profile: UploadProfile,
    paths: list[str | Path],
    *,
    client: httpx.AsyncClient | None = None,
    wifi: WifiPort | None = None,
    interface_name: str = "",
    switch_wifi: bool = True,
    max_attempts: int = DEFAULT_ATTEMPTS,
    retry_delay_sec: float = 0.5,
    on_progress: ProgressCallback | None = None,
    work_dir: Path | None = None,
) -> UploadJobResult:
    """按打包规则准备一个文件、按需切换 WiFi，再上传。

    多个路径或含文件夹时只上传打好的那一个 zip。

    Args:
        profile: 上传配置。
        paths: 原始文件或文件夹。
        client: HTTP 客户端。
        wifi: WiFi 端口，默认使用本机 :class:`WifiConnector`。
        interface_name: 网卡回退名。
        switch_wifi: 为 False 时不碰 WiFi（测试上传用）。
        max_attempts: 总尝试次数。
        retry_delay_sec: 重试间隔，单位秒。
        on_progress: 进度回调。
        work_dir: 临时 zip 目录；缺省时用系统临时目录。

    Returns:
        UploadJobResult: 上传和 WiFi 说明都在里面。WiFi 切回失败不会把已成功的上传改成失败。

    Examples:
        >>> profile = UploadProfile("up_1", "甲", "https://a")
        >>> profile.file_field_name
        'file'
    """
    import tempfile

    port = wifi if wifi is not None else WifiConnector()
    own_dir = work_dir is None
    folder = Path(work_dir) if work_dir is not None else Path(tempfile.mkdtemp(prefix="spider-gd-upload-"))
    try:
        prepared, failed = prepare_upload_files(paths, folder)
        states = [
            *failed,
            *[
                _file_state(item.source, item.name, "ready", item.note or "已准备")
                for item in prepared
            ],
        ]
        prepare_message = "正在准备文件"
        if len(prepared) == 1 and prepared[0].note:
            prepare_message = prepared[0].note
        if on_progress is not None:
            on_progress(
                {
                    "event": "upload_progress",
                    "phase": "prepare",
                    "message": prepare_message,
                    "ok": None,
                    "attempt": 0,
                    "done": 0,
                    "total": len(states),
                    "percent": 8 if states else 0,
                    "files": states,
                }
            )
        if not prepared:
            message = failed[0]["message"] if failed else "没有可上传的文件"
            return UploadJobResult(False, message, 0, states)

        hop = WifiHop(True, True, False, False, "", "", interface_name)
        if switch_wifi:
            hop = await asyncio.to_thread(switch_to_upload_wifi, profile, port, interface_name)
            if on_progress is not None:
                on_progress(
                    {
                        "event": "upload_progress",
                        "phase": "wifi",
                        "message": hop.message,
                        "ok": None if hop.ok else False,
                        "attempt": 0,
                        "done": 0,
                        "total": len(states),
                        "percent": 15,
                        "files": states,
                    }
                )
            if not hop.ok:
                for item in states:
                    if item["status"] != "failed":
                        item["status"] = "failed"
                        item["message"] = hop.message
                return UploadJobResult(False, hop.message, 0, states, wifi_message=hop.message)

        result = await upload_prepared_files(
            profile,
            prepared,
            client=client,
            max_attempts=max_attempts,
            retry_delay_sec=retry_delay_sec,
            on_progress=_merge_failed(failed, on_progress),
        )
        result.files = [*failed, *result.files]
        result.wifi_message = hop.message
        if switch_wifi and hop.switched:
            restored = await asyncio.to_thread(restore_original_wifi, hop, port)
            result.restore_message = restored.message
            if on_progress is not None:
                on_progress(
                    {
                        "event": "upload_progress",
                        "phase": "restore",
                        "message": restored.message,
                        "ok": result.ok,
                        "attempt": result.attempts,
                        "done": sum(1 for item in result.files if item["status"] == "success"),
                        "total": len(result.files),
                        "percent": 100 if result.ok else 96,
                        "files": result.files,
                    }
                )
        return result
    finally:
        if own_dir:
            import shutil

            shutil.rmtree(folder, ignore_errors=True)


async def upload_test_file(
    profile: UploadProfile,
    *,
    client: httpx.AsyncClient | None = None,
    max_attempts: int = 1,
) -> UploadJobResult:
    """上传一个很小的测试文件，不切换 WiFi。

    Args:
        profile: 上传配置。
        client: HTTP 客户端。
        max_attempts: 测试默认只请求一次，避免把对方接口打太多次。

    Returns:
        UploadJobResult: 成功或失败原因在 ``message``。

    Examples:
        >>> profile = UploadProfile("up_1", "甲", "https://a")
        >>> profile.name
        '甲'
    """
    import tempfile

    folder = Path(tempfile.mkdtemp(prefix="spider-gd-upload-test-"))
    sample = folder / TEST_FILE_NAME
    sample.write_bytes(TEST_FILE_BODY)
    try:
        return await run_upload(
            profile,
            [sample],
            client=client,
            switch_wifi=False,
            max_attempts=max_attempts,
            retry_delay_sec=0,
            work_dir=folder,
        )
    finally:
        import shutil

        shutil.rmtree(folder, ignore_errors=True)


def default_wifi_port() -> WifiConnector:
    """本机 WiFi 连接器。

    Returns:
        WifiConnector: 沿用项目里已有的跨平台实现。

    Examples:
        >>> default_wifi_port().platform in {"windows", "macos", "linux", "unsupported"}
        True
    """
    return WifiConnector()


async def _post_once(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    params: dict[str, str],
    data: dict[str, str],
    files: list[PreparedFile],
    file_field_name: str = "file",
) -> tuple[bool, str, dict[str, Any]]:
    """发一次 multipart 请求。

    Args:
        client: HTTP 客户端。
        url: 上传地址。查询参数应已经合并进去。
        headers: 自定义请求头。
        params: 查询参数，会再合并一次，同名时以这里为准。
        data: 额外表单字段。
        files: 要上传的文件。
        file_field_name: multipart 里每个文件使用的字段名。

    Returns:
        tuple[bool, str, dict[str, Any]]: 是否成功、说明、响应 JSON。

    Raises:
        httpx.HTTPError: 网络层错误交给调用方重试。

    Examples:
        >>> callable(_post_once)
        True
    """
    field_name = (file_field_name or "").strip() or "file"
    handles = [item.path.open("rb") for item in files]
    try:
        payload = [
            (field_name, (item.name, handle, "application/octet-stream"))
            for item, handle in zip(files, handles, strict=True)
        ]
        response = await client.post(_with_params(url, params), headers=headers, data=data, files=payload)
    finally:
        for handle in handles:
            handle.close()
    try:
        body: dict[str, Any] = response.json()
    except Exception:
        body = {"raw": response.text}
    if _upload_response_ok(response, body):
        return True, "上传成功", body
    return False, _failure_message(response, body), body


def _with_params(url: str, params: dict[str, str]) -> str:
    """把查询参数合并进已有 URL。同名参数以 ``params`` 为准。

    Args:
        url: 原始地址。
        params: 要合并的参数。

    Returns:
        str: 合并后的地址。没有参数时原样返回。

    Examples:
        >>> _with_params("https://a/up", {})
        'https://a/up'
    """
    if not params:
        return url
    parts = urlsplit(url)
    existing = dict(parse_qsl(parts.query, keep_blank_values=True))
    existing.update(params)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(existing), parts.fragment))


_INVALID_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_PACK_SKIP_DIRS = frozenset({"_versions", "_failures", "_stable_delivery"})


def _safe_upload_label(name: str, *, limit: int = 80) -> str:
    """把名字收成可用的文件名片段。

    Args:
        name: 原始文件名或文件夹名。
        limit: 最长字符数。小于等于 0 时不截断。

    Returns:
        str: 去掉非法字符后的名字。空结果用 ``upload``。

    Examples:
        >>> _safe_upload_label("a.txt")
        'a.txt'
        >>> _safe_upload_label("..")
        'upload'
        >>> _safe_upload_label("a:b")
        'a_b'
        >>> _safe_upload_label(".notes.pdf")
        '.notes.pdf'
        >>> _safe_upload_label("报表")
        '报表'
    """
    cleaned = _INVALID_NAME.sub("_", name).strip(" ").rstrip(".")
    if not cleaned or cleaned in {".", ".."}:
        return "upload"
    if limit > 0 and len(cleaned) > limit:
        suffix = Path(cleaned).suffix
        if suffix and len(suffix) < limit:
            return cleaned[: limit - len(suffix)] + suffix
        return cleaned[:limit]
    return cleaned


def _bundle_zip_name(first_name: str, count: int) -> str:
    """生成打包后的 zip 文件名。

    只有一项时用「名字.zip」。多项时用「第一项_等N项.zip」。

    Args:
        first_name: 选中顺序里第一项的文件名或文件夹名。
        count: 打进同一个 zip 的顶层项数。

    Returns:
        str: zip 文件名，不含目录。

    Examples:
        >>> _bundle_zip_name("报表", 1)
        '报表.zip'
        >>> _bundle_zip_name("a.txt", 2)
        'a.txt_等2项.zip'
        >>> _bundle_zip_name("..", 3)
        'upload_等3项.zip'
    """
    label = _safe_upload_label(first_name)
    if count <= 1:
        return f"{label}.zip"
    return f"{label}_等{count}项.zip"


def _status_message(base: str, note: str) -> str:
    """给进度文案附上打包说明。

    Args:
        base: 状态原文，例如「上传成功」。
        note: 打包说明。空字符串时不追加。

    Returns:
        str: 界面上显示的一句话。

    Examples:
        >>> _status_message("上传成功", "")
        '上传成功'
        >>> _status_message("上传成功", "已打包 2 项")
        '上传成功（已打包 2 项）'
    """
    if not note:
        return base
    return f"{base}（{note}）"


def _top_level_entries(paths: list[Path]) -> list[Path]:
    """去掉重复路径，以及已经被选中文件夹包含的子路径。

    Args:
        paths: 已经确认存在的文件或文件夹，按用户选择顺序。

    Returns:
        list[Path]: 需要各自放进压缩包根上的项。

    Examples:
        >>> _top_level_entries([])
        []
    """
    directories = [path.resolve() for path in paths if path.is_dir()]
    kept: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if resolved in seen:
            continue
        nested = any(
            resolved != directory and resolved.is_relative_to(directory)
            for directory in directories
        )
        if nested:
            continue
        seen.add(resolved)
        kept.append(path)
    return kept


def _skip_packed_file(root: Path, path: Path, *, explicit: bool) -> bool:
    """判断打包时要不要跳过这个文件。

    目录遍历沿用下载产物过滤，版本清单、登录态、``_versions`` 和历史 zip 都不进包。
    用户直接选中的 zip 仍会写入。落在 ``_versions``、``_failures``、``_stable_delivery``
    里的文件，无论是扫出来的还是点名选中的，都不进入压缩包。

    Args:
        root: 过滤时的根目录。文件必须位于其下，否则视为内部文件。
        path: 候选文件。
        explicit: 为 True 时表示用户直接选中了这个文件，而不是从文件夹扫出来的。

    Returns:
        bool: 应跳过时为 True。

    Examples:
        >>> root = Path("/downloads")
        >>> _skip_packed_file(root, root / ".file_versions.json", explicit=False)
        True
        >>> _skip_packed_file(root, root / "_versions" / "old.pdf", explicit=False)
        True
        >>> _skip_packed_file(root / "_versions", root / "_versions" / "old.pdf", explicit=False)
        True
        >>> _skip_packed_file(root, root / "报表.pdf", explicit=False)
        False
        >>> _skip_packed_file(root, root / "报表.zip", explicit=True)
        False
        >>> _skip_packed_file(root, root / "报表.zip", explicit=False)
        True
        >>> _skip_packed_file(root, root / ".notes.pdf", explicit=False)
        False
    """
    if _PACK_SKIP_DIRS.intersection(path.parts[:-1]):
        return True
    from spider_gd.app import _is_internal_download_artifact

    if not _is_internal_download_artifact(root, path):
        return False
    if explicit and path.suffix.lower() == ".zip":
        renamed = path.with_suffix(".bin")
        if not _is_internal_download_artifact(root, renamed):
            return False
    return True


def _relative_arc(path: Path, folder: Path, prefix: str) -> str:
    """生成文件在 zip 里的相对路径。

    Args:
        path: 源文件。
        folder: 该文件所属的选中文件夹。
        prefix: 压缩包内的目录前缀。单个文件夹打包时为空。

    Returns:
        str: 使用 ``/`` 的 zip 条目名。

    Examples:
        >>> _relative_arc(Path("/data/报表/子目录/b.txt"), Path("/data/报表"), "")
        '子目录/b.txt'
        >>> _relative_arc(Path("/data/报表/a.txt"), Path("/data/报表"), "报表")
        '报表/a.txt'
    """
    parts = [_safe_upload_label(part, limit=200) for part in path.relative_to(folder).parts]
    relative = "/".join(parts)
    if prefix:
        return f"{prefix}/{relative}"
    return relative


def _write_tree(handle: zipfile.ZipFile, folder: Path, prefix: str, archive: Path) -> int:
    """把文件夹里的可上传文件写进 zip。

    Args:
        handle: 已打开的 zip。
        folder: 源目录。
        prefix: 压缩包内的目录前缀。单个文件夹打包时为空，多项时为文件夹名。
        archive: 正在写入的 zip 路径，避免把包自己再扫进去。

    Returns:
        int: 写入的文件数。

    Examples:
        >>> import shutil, tempfile, zipfile as zf
        >>> folder = Path(tempfile.mkdtemp())
        >>> _ = (folder / "a.txt").write_text("甲", encoding="utf-8")
        >>> archive = Path(tempfile.mkdtemp()) / "out.zip"
        >>> with zf.ZipFile(archive, "w") as handle:
        ...     count = _write_tree(handle, folder, "报表", archive)
        >>> count
        1
        >>> with zf.ZipFile(archive) as handle:
        ...     handle.namelist()
        ['报表/a.txt']
        >>> shutil.rmtree(folder)
        >>> shutil.rmtree(archive.parent)
    """
    written = 0
    try:
        archive_resolved = archive.resolve()
    except OSError:
        archive_resolved = archive
    for path in sorted(folder.rglob("*")):
        if not path.is_file():
            continue
        try:
            if path.resolve() == archive_resolved:
                continue
        except OSError:
            continue
        if _skip_packed_file(folder, path, explicit=False):
            continue
        handle.write(path, _relative_arc(path, folder, prefix))
        written += 1
    return written


def _zip_selection(items: list[Path], archive: Path) -> int:
    """把多项打进一个 zip。

    单个文件夹时，条目相对该文件夹。多项时，每一项用自己的名字放在 zip 根上。
    内部下载产物不会写入。

    Args:
        items: 顶层文件或文件夹。
        archive: 目标 zip 路径。

    Returns:
        int: 写入的文件条目数。没有任何可上传文件时删除空包并返回 0。

    Examples:
        >>> _zip_selection([], Path("unused.zip"))
        0
    """
    if not items:
        return 0
    single_dir = len(items) == 1 and items[0].is_dir()
    written = 0
    used: set[str] = set()
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as handle:
        for item in items:
            if item.is_dir():
                prefix = "" if single_dir else _unique_name(_safe_upload_label(item.name), used)
                written += _write_tree(handle, item, prefix, archive)
                continue
            if not item.is_file() or _skip_packed_file(item.parent, item, explicit=True):
                continue
            arc = _unique_name(_safe_upload_label(item.name, limit=200), used)
            handle.write(item, arc)
            written += 1
    if written == 0:
        archive.unlink(missing_ok=True)
    return written


def _unique_name(name: str, used: set[str]) -> str:
    """避免同一次上传里出现重名文件。

    Args:
        name: 期望的文件名。
        used: 已经占用的名字，会就地更新。

    Returns:
        str: 不冲突的文件名。

    Examples:
        >>> _unique_name("a.txt", {"a.txt"})
        'a-2.txt'
    """
    if name not in used:
        used.add(name)
        return name
    stem = Path(name).stem
    suffix = Path(name).suffix
    index = 2
    while True:
        candidate = f"{stem}-{index}{suffix}"
        if candidate not in used:
            used.add(candidate)
            return candidate
        index += 1


def _file_state(source: str, name: str, status: str, message: str, attempt: int = 0) -> dict[str, Any]:
    """组装界面上的单文件状态。

    Args:
        source: 原始路径。
        name: 展示和上传用的文件名。
        status: ``pending`` / ``zipping`` / ``ready`` / ``uploading`` / ``retrying`` / ``success`` / ``failed``。
        message: 说明。
        attempt: 当前第几次请求。

    Returns:
        dict[str, Any]: 可放进进度事件的字典。

    Examples:
        >>> _file_state("/a", "a.txt", "ready", "等待")["status"]
        'ready'
    """
    return {
        "source": source,
        "name": name,
        "status": status,
        "message": message,
        "attempt": attempt,
    }


def _percent(phase: str, done: int, total: int, attempt: int, attempts: int, ok: bool | None) -> int:
    """整体进度百分比。

    Args:
        phase: ``prepare`` / ``upload`` / ``retry`` / ``done`` 等。
        done: 已成功的文件数。
        total: 文件总数。
        attempt: 当前尝试序号，从 1 开始。
        attempts: 允许的尝试次数。
        ok: 结束时是否成功。

    Returns:
        int: 0 到 100。

    Examples:
        >>> _percent("done", 1, 1, 1, 4, True)
        100
    """
    if ok is True or (phase == "done" and ok is not False and total and done == total):
        return 100
    if phase == "done":
        return 100
    if attempts <= 0:
        return 0
    span = 70
    walked = max(0, attempt - 1) / attempts
    return min(95, 20 + int(span * walked))


def _merge_failed(
    failed: list[dict[str, Any]],
    callback: ProgressCallback | None,
) -> ProgressCallback | None:
    """让进度事件带上压缩前就失败的项。

    Args:
        failed: 不参与本次请求的失败项。
        callback: 原始回调。

    Returns:
        ProgressCallback | None: 包装后的回调；没有原始回调时返回 None。

    Examples:
        >>> _merge_failed([], None) is None
        True
    """
    if callback is None:
        return None

    def emit(event: dict[str, Any]) -> None:
        files = [*failed, *event.get("files", [])]
        done = sum(1 for item in files if item.get("status") == "success")
        callback({**event, "files": files, "total": len(files), "done": done})

    return emit

