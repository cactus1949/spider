"""在本机弹出场站选择窗口，不把选择框画进浏览器页面。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

from spider_gd.core.station_profile import (
    StationProfile,
    find_station,
    rows_for_inserted_ukeys,
    station_run_plan,
)

DialogOpener = Callable[[list[StationProfile], float], str | None]
MultiDialogOpener = Callable[[list[StationProfile], float], list[str] | None]


class StationPickError(RuntimeError):
    """场站选择无法完成。

    Examples:
        >>> str(StationPickError("没有这个场站"))
        '没有这个场站'
    """


def pick_station(
    profiles: list[StationProfile],
    *,
    choice: str | None = None,
    force_window: bool = False,
    timeout_sec: float = 180,
    opener: DialogOpener | None = None,
) -> StationProfile | None:
    """按场站列表选出本次登录要用的那一把 UKey。

    只有一个场站时直接使用它。多个场站则打开本机窗口。
    ``SPIDER_STATION_PICK`` 可指定编号或名称，用来在测试里跳过窗口。

    Args:
        profiles: 当前可登录的场站。
        choice: 直接指定的编号或名称；空表示交给窗口。
        force_window: 为 True 时，即使只有一个场站也打开窗口。
        timeout_sec: 等待窗口的秒数。
        opener: 打开窗口的函数，测试可替换。默认启动独立进程。

    Returns:
        StationProfile | None: 选中的场站。用户取消时为 None。

    Raises:
        StationPickError: 指定的场站不存在，或窗口没有正常返回。

    Examples:
        >>> from spider_gd.core.station_profile import demo_station_profiles
        >>> picked = pick_station(demo_station_profiles(), choice="阳西海上风电")
        >>> picked is not None and picked.wifi_ssid
        'YX-UKEY'
    """
    explicit = (choice or "").strip()
    if not explicit and not force_window:
        explicit = os.environ.get("SPIDER_STATION_PICK", "").strip()
    if explicit:
        found = find_station(profiles, explicit)
        if found is None:
            raise StationPickError(f"没有名为「{explicit}」的场站")
        return found
    if not profiles:
        return None
    if len(profiles) == 1 and not force_window:
        return profiles[0]
    launch = opener or _launch_dialog
    selected_id = launch(profiles, timeout_sec)
    if not selected_id:
        return None
    found = find_station(profiles, selected_id)
    if found is None:
        raise StationPickError("选择窗口返回了未知场站")
    return found


def _dialog_payload(
    profiles: list[StationProfile],
    *,
    multiple: bool = False,
) -> dict[str, object]:
    """组装选择窗口需要的场站行，不包含密码。

    Args:
        profiles: 要展示的场站。
        multiple: 为 True 时窗口允许多选。

    Returns:
        dict[str, object]: 传给窗口进程的 JSON 对象。

    Examples:
        >>> payload = _dialog_payload([StationProfile("a", "阳江", "YJ", "secret", "pin")])
        >>> payload["stations"][0]["wifiSsid"]
        'YJ'
        >>> "secret" in str(payload)
        False
    """
    return {
        "multiple": multiple,
        "stations": [
            {
                "id": profile.id,
                "name": profile.name,
                "wifiSsid": profile.wifi_ssid,
                "deviceId": profile.device_id,
                "orgName": profile.org_name,
                "status": "未绑定场站名" if profile.id.startswith("usb:") else "已绑定场站名",
            }
            for profile in profiles
        ],
    }


def _source_root() -> Path | None:
    """返回源码布局下的 src 目录。

    Returns:
        Path | None: 能直接 import ``spider_gd`` 的目录；正式安装后为 None。

    Examples:
        >>> root = _source_root()
        >>> root is None or (root / "spider_gd").is_dir()
        True
    """
    root = Path(__file__).resolve().parents[2]
    if (root / "spider_gd" / "__init__.py").is_file():
        return root
    return None


def _launch_dialog(profiles: list[StationProfile], timeout_sec: float) -> str | None:
    """启动独立进程显示单选场站窗口。

    Args:
        profiles: 要展示的场站。
        timeout_sec: 等待秒数，超时后结束该进程。

    Returns:
        str | None: 选中的场站编号；用户取消时为 None。

    Raises:
        StationPickError: 进程失败或超时。

    Examples:
        >>> callable(_launch_dialog)
        True
    """
    raw = _run_picker(_dialog_payload(profiles), timeout_sec)
    if raw is None:
        return None
    return _selected_id(raw)


def _launch_multi_dialog(profiles: list[StationProfile], timeout_sec: float) -> list[str] | None:
    """启动独立进程显示多选场站窗口。

    Args:
        profiles: 要展示的场站。
        timeout_sec: 等待秒数，超时后结束该进程。

    Returns:
        list[str] | None: 按勾选顺序排列的编号；取消时为 None。

    Raises:
        StationPickError: 进程失败或超时。

    Examples:
        >>> callable(_launch_multi_dialog)
        True
    """
    raw = _run_picker(_dialog_payload(profiles, multiple=True), timeout_sec)
    if raw is None:
        return None
    return _selected_ids(raw)


def _run_picker(payload: dict[str, object], timeout_sec: float) -> str | None:
    """运行窗口进程并读回结果文件。

    Args:
        payload: 传给窗口的 JSON 对象。
        timeout_sec: 等待秒数。

    Returns:
        str | None: 结果 JSON 文本。用户取消且没有结果文件时为 None。

    Raises:
        StationPickError: 进程失败、超时或没有写出结果。

    Examples:
        >>> callable(_run_picker)
        True
    """
    env = os.environ.copy()
    src = _source_root()
    if src is not None:
        env["PYTHONPATH"] = str(src) + os.pathsep + env.get("PYTHONPATH", "")
    with tempfile.TemporaryDirectory(prefix="spider-station-") as tmp:
        directory = Path(tmp)
        request_path = directory / "request.json"
        result_path = directory / "result.json"
        request_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        command = [
            sys.executable,
            "-m",
            "spider_gd.ui.station_picker_dialog",
            "--input",
            str(request_path),
            "--output",
            str(result_path),
        ]
        try:
            completed = subprocess.run(
                command,
                env=env,
                capture_output=True,
                timeout=timeout_sec,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise StationPickError("场站选择窗口等待超时") from exc
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        if completed.returncode not in {0, 2}:
            detail = stderr or f"退出码 {completed.returncode}"
            raise StationPickError(f"场站选择窗口启动失败：{detail}")
        if not result_path.is_file():
            if completed.returncode == 2:
                return None
            raise StationPickError("场站选择窗口没有返回结果")
        return result_path.read_text(encoding="utf-8")


def _selected_id(raw: str) -> str | None:
    """从窗口进程写出的 JSON 里取出场站编号。

    Args:
        raw: 结果文件文本。

    Returns:
        str | None: 场站编号；取消时为 None。

    Raises:
        StationPickError: 结果不是约定的 JSON。

    Examples:
        >>> _selected_id('{"cancelled": true}') is None
        True
        >>> _selected_id('{"id": "demo-yangjiang"}')
        'demo-yangjiang'
    """
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise StationPickError("场站选择窗口返回的内容无法解析") from exc
    if not isinstance(data, dict) or data.get("cancelled"):
        return None
    selected = str(data.get("id") or "").strip()
    return selected or None


def _selected_ids(raw: str) -> list[str] | None:
    """从多选结果里按顺序取出场站编号。

    Args:
        raw: 结果文件文本。

    Returns:
        list[str] | None: 编号列表。取消时为 None。

    Raises:
        StationPickError: 结果不是约定的 JSON。

    Examples:
        >>> _selected_ids('{"id": "b", "ids": ["b", "a"], "cancelled": false}')
        ['b', 'a']
    """
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise StationPickError("场站选择窗口返回的内容无法解析") from exc
    if not isinstance(data, dict) or data.get("cancelled"):
        return None
    raw_ids = data.get("ids")
    if isinstance(raw_ids, list):
        ids = [str(item).strip() for item in raw_ids if str(item).strip()]
        return ids or None
    one = str(data.get("id") or "").strip()
    return [one] if one else None


def pick_stations(
    profiles: list[StationProfile],
    *,
    choice: str | None = None,
    force_window: bool = False,
    timeout_sec: float = 180,
    opener: MultiDialogOpener | None = None,
) -> list[StationProfile] | None:
    """按勾选顺序选出本次任务要执行的场站。

    Args:
        profiles: 当前可执行的场站。
        choice: 逗号分隔的编号或名称。传入后不打开窗口。
        force_window: 为 True 时，即使只有一个场站也打开窗口。
        timeout_sec: 等待窗口的秒数。
        opener: 打开多选窗口的函数，返回编号列表。测试可替换。

    Returns:
        list[StationProfile] | None: 选中的场站。用户取消时为 None。

    Raises:
        StationPickError: 指定的场站不存在，或窗口返回了未知场站。

    Examples:
        >>> from spider_gd.core.station_profile import demo_station_profiles
        >>> picked = pick_stations(demo_station_profiles(), choice="阳西海上风电,阳江明阳海上风电")
        >>> [item.wifi_ssid for item in picked]
        ['YX-UKEY', 'YJ-UKEY']
    """
    from spider_gd.ui.station_picker_dialog import match_station_ids

    explicit = (choice or "").strip()
    if explicit:
        ids = match_station_ids(
            [{"id": profile.id, "name": profile.name} for profile in profiles],
            explicit,
        )
        if ids is None:
            raise StationPickError(f"没有名为「{explicit}」的场站")
        return [_require_station(profiles, station_id) for station_id in ids]
    if not profiles:
        return []
    if len(profiles) == 1 and not force_window:
        return list(profiles)
    launch = opener or _launch_multi_dialog
    selected_ids = launch(profiles, timeout_sec)
    if selected_ids is None:
        return None
    return [_require_station(profiles, station_id) for station_id in selected_ids]


def choose_task_stations(
    profiles: list[StationProfile],
    plugged: list[tuple[str, str]],
    *,
    fallback_cert_password: str = "",
    opener: MultiDialogOpener | None = None,
) -> list[dict[str, str]] | None:
    """决定任务开始前要执行的场站。

    设置里有两个及以上场站时，立即执行前弹出多选窗口，可选一个或多个。
    只配置一个场站、且没有第二把未绑定的 UKey 时，直接用这一个，不弹窗。

    Args:
        profiles: 设置里的场站。
        plugged: 当前 UKey，每项是 ``(特征码, 设备显示名)``。
        fallback_cert_password: 场站没填证书密码时使用的全局密码。
        opener: 测试用的多选窗口。

    Returns:
        list[dict[str, str]] | None: 按执行顺序排列的场站计划。取消窗口时为 None。
        没有需要单独切换的场站时为空列表。

    Examples:
        >>> bound = StationProfile("a", "阳江", "YJ", "", "pin", "SN-YJ")
        >>> plan = choose_task_stations([bound], [("SN-YJ", "USB 设备")])
        >>> plan[0]["name"]
        '阳江'
        >>> two = [
        ...     StationProfile("a", "阳江", "YJ"),
        ...     StationProfile("b", "阳西", "YX"),
        ... ]
        >>> picked = choose_task_stations(two, [], opener=lambda rows, _timeout: ["b"])
        >>> [item["name"] for item in picked]
        ['阳西']
    """
    configured = [profile for profile in profiles if profile.name.strip()]
    usb_rows, usb_popup = rows_for_inserted_ukeys(profiles, plugged)
    if len(configured) >= 2 or usb_popup:
        known = {profile.id for profile in configured}
        window_rows = [
            *configured,
            *[row for row in usb_rows if row.id not in known],
        ]
        selected = pick_stations(window_rows, force_window=True, opener=opener)
        if selected is None:
            return None
        return [station_run_plan(item, fallback_cert_password) for item in selected]
    if len(configured) == 1:
        return [station_run_plan(configured[0], fallback_cert_password)]
    if len(usb_rows) == 1 and not usb_rows[0].id.startswith("usb:"):
        return [station_run_plan(usb_rows[0], fallback_cert_password)]
    return []


def _require_station(profiles: list[StationProfile], station_id: str) -> StationProfile:
    """按编号取出场站，找不到就报错。

    Args:
        profiles: 候选场站。
        station_id: 场站编号。

    Returns:
        StationProfile: 命中的场站。

    Raises:
        StationPickError: 编号不在候选里。

    Examples:
        >>> _require_station([StationProfile("a", "阳江", "YJ")], "a").name
        '阳江'
    """
    found = find_station(profiles, station_id)
    if found is None:
        raise StationPickError("选择窗口返回了未知场站")
    return found
