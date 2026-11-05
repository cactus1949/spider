"""采集结束后的上传，以及文件中心的手动上传。

自动上传和手动上传都写进同一次执行日志：开始、重试、成功、失败和原因。
文件中心没有采集日志时，另开一条 ``data_upload`` 记录，仍出现在执行日志页。
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

from spider_gd.gateway.data_uploader import (
    AUTO_RETRY_TIMES,
    UploadJobResult,
    probe_wifi_switch,
    run_upload,
    upload_test_file,
)
from spider_gd.gateway.upload_profiles import (
    UploadProfile,
    get_profile,
    load_profiles,
    public_profiles,
    resolve_submitted_profile,
    save_profiles,
)
from spider_gd.infrastructure.network.wifi_connector import WifiConnector
from spider_gd.interfaces.storage import ExecutionLogRecord

EventSink = Callable[[dict[str, Any]], None]


def list_public_profiles(store: object) -> list[dict[str, Any]]:
    """返回给界面的上传配置，不含明文秘密。

    Args:
        store: 状态库。

    Returns:
        list[dict[str, Any]]: 掩码后的配置。

    Examples:
        >>> class Store:
        ...     def get_setting(self, key):
        ...         return ""
        >>> list_public_profiles(Store())
        []
    """
    return public_profiles(load_profiles(store))


def replace_profiles(store: object, incoming: list[object]) -> list[dict[str, Any]]:
    """保存上传配置并返回掩码结果。

    Args:
        store: 状态库。
        incoming: 界面提交的列表。

    Returns:
        list[dict[str, Any]]: 掩码后的配置。

    Raises:
        ValueError: 配置不合法。

    Examples:
        >>> class Store:
        ...     def __init__(self):
        ...         self.value = ""
        ...     def get_setting(self, key):
        ...         return self.value
        ...     def save_settings(self, settings):
        ...         self.value = next(iter(settings.values()))
        >>> replace_profiles(Store(), [{"name": "甲", "url": "https://a"}])[0]["name"]
        '甲'
    """
    return public_profiles(save_profiles(store, incoming))


async def maybe_upload_after_run(
    store: object,
    *,
    profile_id: str,
    auto_upload: bool,
    exec_id: str,
    download_dir: str,
    before: dict[str, tuple[int, int]],
    interface_name: str = "",
    on_event: EventSink | None = None,
    wifi: object | None = None,
    client: object | None = None,
    retry_delay_sec: float = 0.5,
) -> dict[str, Any] | None:
    """采集跑完后，按任务上的上传配置处理本次产物。

    没选配置时什么都不做。选了但没开自动上传，只把文件路径交给界面上的「上传」按钮。
    开了自动上传就立刻传；失败自动重试，并把结果写进这条执行日志。上传失败不改采集状态。
    产物含新增或改动的文件，以及内容未变、但本轮重新确认过的文件。
    ``.file_versions.json`` 和 ``_versions`` 归档不会进入上传列表。
    多个产物打成一个 zip 后上传；只有一个普通文件时原样上传。

    Args:
        store: 状态库。
        profile_id: 上传配置 id，可空。
        auto_upload: 是否自动上传。
        exec_id: 这次采集的执行日志 id。
        download_dir: 本次下载根目录。
        before: 采集前的文件快照，来自 ``_snapshot_download_files``。
        interface_name: 配置没写网卡时的回退。
        on_event: 进度事件，通常推进 SSE 队列。
        wifi: 可注入的 WiFi 端口。
        client: 可注入的 HTTP 客户端。
        retry_delay_sec: 重试间隔，单位秒。

    Returns:
        dict[str, Any] | None: 给界面的 ``upload_offer``；完全没配置时返回 None。

    Examples:
        >>> class Store:
        ...     def get_setting(self, key):
        ...         return ""
        ...     def append_execution_log_detail(self, exec_id, line):
        ...         return True
        >>> import asyncio
        >>> asyncio.run(maybe_upload_after_run(
        ...     Store(), profile_id="", auto_upload=False, exec_id="e",
        ...     download_dir=".", before={},
        ... )) is None
        True
    """
    selected = (profile_id or "").strip()
    if not selected and not auto_upload:
        return None
    if not selected:
        _log(store, exec_id, "☁️ 已开启自动上传，但没有选择上传配置，跳过")
        return None
    profile = get_profile(store, selected)
    if profile is None:
        _log(store, exec_id, f"☁️ 上传配置不存在: {selected}")
        offer = _offer(selected, exec_id, [], auto_upload, False, "上传配置不存在")
        _emit_offer(store, exec_id, offer, on_event)
        return offer
    paths = [str(path) for path in _changed_files(download_dir, before)]
    if not auto_upload:
        message = "等待手动上传" if paths else "本次没有新的产物"
        if paths:
            _log(store, exec_id, f"☁️ 已选择上传配置「{profile.name}」，未开启自动上传，可在结果里手动上传")
        else:
            _log(store, exec_id, "☁️ 未开启自动上传，且本次没有新的产物")
        offer = _offer(profile.id, exec_id, paths, False, None, message, profile.name)
        _emit_offer(store, exec_id, offer, on_event)
        return offer
    if not paths:
        _log(store, exec_id, "☁️ 自动上传跳过：本次没有新的产物")
        offer = _offer(profile.id, exec_id, [], True, True, "本次没有新的产物", profile.name)
        _emit_offer(store, exec_id, offer, on_event)
        return offer
    result = await _upload_profile_paths(
        store,
        profile,
        paths,
        exec_id=exec_id,
        source_name=profile.name,
        interface_name=interface_name,
        on_event=on_event,
        wifi=wifi,
        client=client,
        retry_delay_sec=retry_delay_sec,
        create_log=False,
    )
    offer = _offer(
        profile.id,
        exec_id,
        paths,
        True,
        result.ok,
        result.message,
        profile.name,
    )
    _emit_offer(store, exec_id, offer, on_event)
    return offer


async def upload_selected_paths(
    store: object,
    profile: UploadProfile,
    paths: list[str],
    *,
    exec_id: str = "",
    interface_name: str = "",
    on_event: EventSink | None = None,
    wifi: object | None = None,
    client: object | None = None,
    retry_delay_sec: float = 0.5,
    log_name: str = "数据上传",
) -> UploadJobResult:
    """文件中心或结果弹窗里的手动上传。

    路径列表走同一套打包规则：至多上传一个文件或一个 zip。

    Args:
        store: 状态库。
        profile: 已解密的配置。
        paths: 要上传的路径。
        exec_id: 已有执行日志就往里追加；空则新建一条。
        interface_name: 网卡回退名。
        on_event: 进度回调。
        wifi: WiFi 端口。
        client: HTTP 客户端。
        retry_delay_sec: 重试间隔，单位秒。
        log_name: 新建日志时的名称。

    Returns:
        UploadJobResult: 上传结果。

    Examples:
        >>> profile = UploadProfile("up_1", "甲", "https://a")
        >>> profile.id
        'up_1'
    """
    return await _upload_profile_paths(
        store,
        profile,
        paths,
        exec_id=exec_id,
        source_name=log_name,
        interface_name=interface_name,
        on_event=on_event,
        wifi=wifi,
        client=client,
        retry_delay_sec=retry_delay_sec,
        create_log=not (exec_id or "").strip(),
    )


async def run_test_upload(store: object, payload: dict[str, Any], client: object | None = None) -> dict[str, Any]:
    """设置页「测试上传」：发一个小文件，不切 WiFi。

    Args:
        store: 状态库。
        payload: 配置 id 或草稿。
        client: HTTP 客户端。

    Returns:
        dict[str, Any]: ``ok`` 和 ``message``。

    Raises:
        ValueError: 配置不合法。

    Examples:
        >>> UploadProfile("up_1", "甲", "https://a").url
        'https://a'
    """
    profile = resolve_submitted_profile(store, payload)
    result = await upload_test_file(profile, client=client)  # type: ignore[arg-type]
    return {"ok": result.ok, "message": result.message, "attempts": result.attempts}


def run_test_wifi(store: object, payload: dict[str, Any], wifi: object | None = None, interface_name: str = "") -> dict[str, Any]:
    """设置页「测试切换 WiFi」。已连接目标时不会把它切走。

    Args:
        store: 状态库。
        payload: 配置 id 或草稿。
        wifi: WiFi 端口，缺省用本机连接器。
        interface_name: 网卡回退名。

    Returns:
        dict[str, Any]: ``ok``、``message``、``alreadyConnected``。

    Raises:
        ValueError: 配置不合法。

    Examples:
        >>> callable(run_test_wifi)
        True
    """
    profile = resolve_submitted_profile(store, payload)
    port = wifi if wifi is not None else WifiConnector()
    return probe_wifi_switch(profile, port, interface_name)  # type: ignore[arg-type]


async def iter_upload_events(
    store: object,
    *,
    profile_id: str,
    paths: list[str],
    exec_id: str = "",
    interface_name: str = "",
    log_name: str = "数据上传",
    wifi: object | None = None,
    client: object | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """手动上传的 SSE 事件流。

    Args:
        store: 状态库。
        profile_id: 上传配置 id。
        paths: 文件或文件夹。
        exec_id: 追加到这条日志；空则新建。
        interface_name: 网卡回退名。
        log_name: 新建日志的名称。
        wifi: WiFi 端口。
        client: HTTP 客户端。

    Yields:
        dict[str, Any]: ``upload_progress`` 或 ``upload_settled``。

    Examples:
        >>> callable(iter_upload_events)
        True
    """
    import asyncio

    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    async def worker() -> None:
        try:
            profile = get_profile(store, profile_id)
            if profile is None:
                queue.put_nowait({"event": "upload_settled", "ok": False, "message": "上传配置不存在", "files": []})
                return
            result = await upload_selected_paths(
                store,
                profile,
                paths,
                exec_id=exec_id,
                interface_name=interface_name,
                on_event=queue.put_nowait,
                wifi=wifi,
                client=client,
                log_name=log_name,
            )
            queue.put_nowait(
                {
                    "event": "upload_settled",
                    "ok": result.ok,
                    "message": result.message,
                    "files": result.files,
                    "execId": str(getattr(result, "exec_id", "") or exec_id),
                }
            )
        except Exception as exc:
            queue.put_nowait({"event": "upload_settled", "ok": False, "message": str(exc), "files": []})
        finally:
            queue.put_nowait(None)

    task = asyncio.create_task(worker())
    try:
        while True:
            item = await queue.get()
            if item is None:
                break
            yield item
    finally:
        await task


async def _upload_profile_paths(
    store: object,
    profile: UploadProfile,
    paths: list[str],
    *,
    exec_id: str,
    source_name: str,
    interface_name: str,
    on_event: EventSink | None,
    wifi: object | None,
    client: object | None,
    retry_delay_sec: float,
    create_log: bool,
) -> UploadJobResult:
    """执行上传并把关键节点写进执行日志。

    Args:
        store: 状态库。
        profile: 上传配置。
        paths: 路径列表。
        exec_id: 日志 id，新建时会被改写到本地变量。
        source_name: 日志名称。
        interface_name: 网卡回退名。
        on_event: 进度回调。
        wifi: WiFi 端口。
        client: HTTP 客户端。
        retry_delay_sec: 重试间隔，单位秒。
        create_log: 是否新建 ``data_upload`` 日志。

    Returns:
        UploadJobResult: 上传结果。

    Examples:
        >>> callable(_upload_profile_paths)
        True
    """
    active_id = exec_id
    if create_log:
        active_id = _create_upload_log(store, source_name, profile.id)
    _log(store, active_id, f"☁️ 开始上传「{profile.name}」，选中 {len(paths)} 项")
    seen_retry: set[int] = set()

    def observe(event: dict[str, Any]) -> None:
        phase = str(event.get("phase") or "")
        message = str(event.get("message") or "")
        attempt = int(event.get("attempt") or 0)
        if phase == "wifi" and message:
            _log(store, active_id, f"☁️ {message}")
        elif phase == "retry" and attempt not in seen_retry:
            seen_retry.add(attempt)
            _log(
                store,
                active_id,
                f"☁️ 上传失败，准备重试 {attempt}/{AUTO_RETRY_TIMES}：{message}",
            )
        elif phase == "restore" and message:
            _log(store, active_id, f"☁️ {message}")
        if on_event is not None:
            on_event(event)

    result = await run_upload(
        profile,
        paths,
        client=client,  # type: ignore[arg-type]
        wifi=wifi,  # type: ignore[arg-type]
        interface_name=interface_name,
        switch_wifi=True,
        retry_delay_sec=retry_delay_sec,
        on_progress=observe,
    )
    if result.ok:
        uploaded = [
            str(item.get("name") or "")
            for item in result.files
            if item.get("status") == "success"
        ]
        shown = "、".join(name for name in uploaded if name) or "文件"
        failed_count = sum(1 for item in result.files if item.get("status") == "failed")
        extra = f"，另有 {failed_count} 项未上传" if failed_count else ""
        _log(store, active_id, f"☁️ 上传成功：{shown}（{result.attempts} 次请求{extra}）")
        if create_log:
            _finish_upload_log(store, active_id, "success", "")
    else:
        retried = max(0, result.attempts - 1)
        _log(store, active_id, f"☁️ 上传失败：{result.message}（已自动重试 {retried} 次）")
        if create_log:
            _finish_upload_log(store, active_id, "failed", result.message[:200])
    result.exec_id = active_id
    return result


def _changed_files(download_dir: str, before: dict[str, tuple[int, int]]) -> list[Any]:
    """找出这次采集要上传的文件。

    含新增或改动的文件，以及内容未变、但版本清单在本轮被重新确认的产物。
    ``.file_versions.json`` 和 ``_versions`` 归档不在其中。

    Args:
        download_dir: 下载根目录。
        before: 采集前快照，来自 ``_snapshot_download_files``。

    Returns:
        list[Any]: 文件路径。

    Examples:
        >>> _changed_files("", {}) == []
        True
    """
    if not download_dir:
        return []
    from spider_gd.app import _changed_download_files

    return _changed_download_files(download_dir, before)


def _create_upload_log(store: object, name: str, profile_id: str) -> str:
    """新建一条数据上传日志。

    Args:
        store: 状态库。
        name: 显示名称。
        profile_id: 上传配置 id。

    Returns:
        str: 新的 exec_id。

    Examples:
        >>> callable(_create_upload_log)
        True
    """
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    exec_id = f"upl_{uuid.uuid4().hex[:12]}"
    store.create_execution_log(  # type: ignore[attr-defined]
        ExecutionLogRecord(
            exec_id=exec_id,
            name=name or "数据上传",
            type="data_upload",
            target_id=profile_id,
            status="running",
            reason="",
            created_at=now,
            updated_at=now,
        )
    )
    return exec_id


def _finish_upload_log(store: object, exec_id: str, status: str, reason: str) -> None:
    """结束一条独立的上传日志。

    Args:
        store: 状态库。
        exec_id: 日志 id。
        status: ``success`` 或 ``failed``。
        reason: 失败原因，成功时为空。

    Returns:
        None: 写完即返回。

    Examples:
        >>> callable(_finish_upload_log)
        True
    """
    updater = getattr(store, "update_execution_log", None)
    if not callable(updater):
        return
    updater(exec_id, status=status, reason=reason, updated_at=time.strftime("%Y-%m-%d %H:%M:%S"))


def _log(store: object, exec_id: str, line: str) -> None:
    """往执行日志追加一行。

    Args:
        store: 状态库。
        exec_id: 日志 id。
        line: 一行中文说明。

    Returns:
        None: 没有日志接口时静默跳过。

    Examples:
        >>> _log(object(), "", "跳过") is None
        True
    """
    if not exec_id:
        return
    writer = getattr(store, "append_execution_log_detail", None)
    if callable(writer):
        writer(exec_id, line)


def _offer(
    profile_id: str,
    exec_id: str,
    paths: list[str],
    auto: bool,
    ok: bool | None,
    message: str,
    profile_name: str = "",
) -> dict[str, Any]:
    """组装结果弹窗要用的上传提议。

    Args:
        profile_id: 配置 id。
        exec_id: 执行日志 id。
        paths: 可上传的绝对路径。
        auto: 是否已经自动上传过。
        ok: 自动上传结果；还没传时为 None。
        message: 说明。
        profile_name: 配置名称。

    Returns:
        dict[str, Any]: ``upload_offer`` 事件。

    Examples:
        >>> _offer("up_1", "e", [], False, None, "等待")["event"]
        'upload_offer'
    """
    return {
        "event": "upload_offer",
        "profile_id": profile_id,
        "profile_name": profile_name,
        "exec_id": exec_id,
        "paths": paths,
        "auto": auto,
        "ok": ok,
        "message": message,
    }


def _emit_offer(store: object, exec_id: str, offer: dict[str, Any], on_event: EventSink | None) -> None:
    """把提议写进日志，并通知 SSE。

    Args:
        store: 状态库。
        exec_id: 日志 id。
        offer: 提议正文。
        on_event: SSE 回调。

    Returns:
        None: 写完即返回。

    Examples:
        >>> _emit_offer(object(), "", {"event": "upload_offer"}, None) is None
        True
    """
    import json

    _log(store, exec_id, "📌 upload_offer: " + json.dumps(offer, ensure_ascii=False))
    if on_event is not None:
        on_event(offer)
        on_event({"event": "upload_settled", "ok": offer.get("ok"), "message": offer.get("message") or ""})
