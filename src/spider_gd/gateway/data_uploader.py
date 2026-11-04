"""按上传配置把文件 POST 到客户接口。

协议是 ``multipart/form-data``，每个文件的字段名都是 ``file``，一次请求可以带多个
文件。文件夹会先打成 zip。失败后自动再试 3 次（连第一次一共 4 次）。配置了 WiFi
时先切过去，已经连着目标网络就不切；上传完再切回原来的网络。
"""

from __future__ import annotations

import asyncio
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from spider_gd.gateway.upload_profiles import UploadProfile, apply_token_prefix
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

    Args:
        source: 用户选中的原始路径。
        name: 上传时使用的文件名。
        path: 实际读取的本地文件。
        kind: ``file`` 或 ``zip``。

    Examples:
        >>> PreparedFile("/a", "a.txt", Path("a.txt"), "file").kind
        'file'
    """

    source: str
    name: str
    path: Path
    kind: str


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
    """把选中的文件留下，把文件夹打成 zip。

    Args:
        paths: 文件或文件夹路径。
        work_dir: 存放临时 zip 的目录，调用方负责清理。

    Returns:
        tuple[list[PreparedFile], list[dict[str, Any]]]: 可上传的文件，以及无法处理的项
        （状态已经是 ``failed``）。

    Examples:
        >>> prepared, failed = prepare_upload_files([], Path("."))
        >>> prepared == [] and failed == []
        True
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    ready: list[PreparedFile] = []
    failed: list[dict[str, Any]] = []
    used_names: set[str] = set()
    for raw in paths:
        path = Path(raw)
        source = str(path)
        if not path.exists():
            failed.append(_file_state(source, path.name or source, "failed", "文件不存在"))
            continue
        if path.is_dir():
            archive_name = _unique_name(f"{path.name}.zip", used_names)
            archive = work_dir / archive_name
            _zip_directory(path, archive)
            ready.append(PreparedFile(source, archive_name, archive, "zip"))
            continue
        if not path.is_file():
            failed.append(_file_state(source, path.name, "failed", "不是文件或文件夹"))
            continue
        ready.append(PreparedFile(source, _unique_name(path.name, used_names), path, "file"))
    return ready, failed


def build_request_parts(profile: UploadProfile) -> tuple[str, dict[str, str], dict[str, str], dict[str, str]]:
    """按 token 传递方式拼出 URL、请求头、查询参数和表单字段。

    头模式把 ``前缀 + token`` 放进可配置的头，默认 ``Authorization: Bearer <token>``，
    和录制工坊现有上传一致。参数模式把 token 放进 query 或 form，不加前缀。

    Args:
        profile: 上传配置。

    Returns:
        tuple[str, dict[str, str], dict[str, str], dict[str, str]]: URL、headers、params、data。

    Examples:
        >>> profile = UploadProfile("up_1", "甲", "https://a/up", token="s")
        >>> _url, headers, params, data = build_request_parts(profile)
        >>> headers["Authorization"] == "Bearer s" and params == {} and data == {}
        True
    """
    url = profile.url.strip()
    headers: dict[str, str] = {}
    params: dict[str, str] = {}
    data: dict[str, str] = {}
    token = (profile.token or "").strip()
    if not token:
        return url, headers, params, data
    if profile.token_mode == "param":
        name = profile.token_param_name.strip()
        if profile.token_param_in == "form":
            data[name] = token
        else:
            params[name] = token
        return url, headers, params, data
    headers[profile.token_header_name.strip()] = apply_token_prefix(profile.token_value_prefix, token)
    return url, headers, params, data


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
    states = [_file_state(item.source, item.name, "ready", "等待上传") for item in files]
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
            for item in states:
                item["status"] = status
                item["attempt"] = attempt
                item["message"] = note
            emit("upload", note, None, attempt)
            try:
                response_ok, message, _payload = await _post_once(client, url, headers, params, data, files)
            except Exception as exc:
                response_ok = False
                message = str(exc)
            result = UploadJobResult(response_ok, message, attempt, states)
            if response_ok:
                for item in states:
                    item["status"] = "success"
                    item["message"] = "上传成功"
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
    """压缩文件夹、按需切换 WiFi，再上传。

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
        >>> profile.token_mode
        'header'
    """
    import tempfile

    port = wifi if wifi is not None else WifiConnector()
    own_dir = work_dir is None
    folder = Path(work_dir) if work_dir is not None else Path(tempfile.mkdtemp(prefix="spider-gd-upload-"))
    try:
        prepared, failed = prepare_upload_files(paths, folder)
        states = [
            *failed,
            *[_file_state(item.source, item.name, "ready", "已准备") for item in prepared],
        ]
        if on_progress is not None:
            on_progress(
                {
                    "event": "upload_progress",
                    "phase": "prepare",
                    "message": "正在准备文件",
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
) -> tuple[bool, str, dict[str, Any]]:
    """发一次 multipart 请求。

    Args:
        client: HTTP 客户端。
        url: 上传地址。
        headers: 含 token 的请求头。
        params: 查询参数。
        data: 额外表单字段，参数模式的 token 在这里。
        files: 要上传的文件。

    Returns:
        tuple[bool, str, dict[str, Any]]: 是否成功、说明、响应 JSON。

    Raises:
        httpx.HTTPError: 网络层错误交给调用方重试。

    Examples:
        >>> callable(_post_once)
        True
    """
    handles = [item.path.open("rb") for item in files]
    try:
        payload = [("file", (item.name, handle, "application/octet-stream")) for item, handle in zip(files, handles, strict=True)]
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
    """把查询参数合并进已有 URL。

    Args:
        url: 原始地址。
        params: 要追加的参数。

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


def _zip_directory(folder: Path, archive: Path) -> None:
    """把文件夹压成 zip，条目名相对该文件夹。

    Args:
        folder: 源目录。
        archive: 目标 zip 路径。

    Returns:
        None: 写完即返回。

    Examples:
        >>> callable(_zip_directory)
        True
    """
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as handle:
        has_entry = False
        for path in sorted(folder.rglob("*")):
            if not path.is_file():
                continue
            handle.write(path, path.relative_to(folder).as_posix())
            has_entry = True
        if not has_entry:
            # 空目录也给一个占位，避免发出 0 字节又没有条目的包被当成失败。
            info = zipfile.ZipInfo(f"{folder.name}/")
            handle.writestr(info, b"")


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

