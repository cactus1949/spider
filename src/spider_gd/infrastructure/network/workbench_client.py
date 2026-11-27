"""WifiRack 移动工作台客户端：支持 HTTP 与 USB 串口双通道，按 SSID 动态唤醒随身 WiFi。

EXE 不需要关心某台随身 WiFi 插在工作台的几号槽，直接传 ``ssid``（或场站名），
工作台会先扫在线状态，离线时再动态定位槽位、闭环短按/长按唤醒并返回结果。
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import httpx
from loguru import logger

SERIAL_PREFIX = "@@WIFIRACK@@"


@dataclass(frozen=True)
class WorkbenchConfig:
    """工作台连接配置。

    Args:
        enabled: 是否启用工作台联动。
        transport: 通信方式，支持 ``auto``、``http``、``serial``。
        base_url: ESP32 HTTP 地址，例如 ``http://wifirack.local`` 或 ``http://192.168.1.50``。
        api_token: HTTP Bearer Token。
        serial_port: USB 串口名（如 ``COM5`` 或 ``/dev/cu.usbserial-*``）；``auto`` 表示自动扫描。
        baudrate: 串口波特率，默认 ``115200``。
        wake_timeout_sec: 等待单台随身 WiFi 唤醒上线的默认超时秒数。
        request_timeout_sec: 单次 HTTP/串口指令超时秒数。
    """

    enabled: bool = False
    transport: str = "auto"
    base_url: str = "http://wifirack.local"
    api_token: str = "wifirack-1234"
    serial_port: str = ""
    baudrate: int = 115200
    wake_timeout_sec: float = 45.0
    request_timeout_sec: float = 8.0


@dataclass(frozen=True)
class WorkbenchWakeResult:
    """一次按需唤醒或预唤醒的结果。"""

    ok: bool
    status: str
    ssid: str = ""
    slot_id: int = 0
    rssi: int = -127
    job_id: int = 0
    message: str = ""
    transport: str = ""


def _parse_bool(raw: object, default: bool = False) -> bool:
    if isinstance(raw, bool):
        return raw
    text = str(raw or "").strip().lower()
    if text in {"1", "true", "yes", "on", "y"}:
        return True
    if text in {"0", "false", "no", "off", "n"}:
        return False
    return default


def _parse_float(raw: object, default: float) -> float:
    try:
        val = float(str(raw or "").strip())
        return val if val > 0 else default
    except (TypeError, ValueError):
        return default


def workbench_config_from_store(state_store: Any) -> WorkbenchConfig:
    """从 SQLite 应用设置表读取工作台配置。"""
    getter = getattr(state_store, "get_setting", None)
    if not callable(getter):
        return WorkbenchConfig()

    def _get(key: str, default: str = "") -> str:
        try:
            value = getter(key)
        except Exception:
            return default
        return default if value is None else str(value)

    transport = _get("workbenchTransport", "auto").strip().lower()
    if transport not in {"auto", "http", "serial"}:
        transport = "auto"
    return WorkbenchConfig(
        enabled=_parse_bool(_get("workbenchEnabled", "false"), False),
        transport=transport,
        base_url=_get("workbenchUrl", "http://wifirack.local").strip().rstrip("/") or "http://wifirack.local",
        api_token=_get("workbenchToken", "wifirack-1234").strip(),
        serial_port=_get("workbenchSerialPort", "").strip(),
        wake_timeout_sec=_parse_float(_get("workbenchWakeTimeoutSec", "45"), 45.0),
    )


def _auto_detect_serial_port() -> str:
    """尽量找到 ESP32 对应的 USB 串口 (CP210x / CH340 / USB Serial)。"""
    try:
        from serial.tools import list_ports
    except ImportError as exc:
        raise RuntimeError("未安装 pyserial，无法使用串口模式控制工作台") from exc

    ports = list(list_ports.comports())
    keywords = ("cp210", "ch340", "ch341", "ch910", "silicon labs", "usb-serial", "uart", "esp32")
    for port in ports:
        desc = f"{port.description or ''} {port.manufacturer or ''} {port.hwid or ''}".lower()
        if any(token in desc for token in keywords):
            return str(port.device)
    if len(ports) == 1:
        return str(ports[0].device)
    raise RuntimeError("未自动检测到工作台 USB 串口，请在设置中指定串口号 (如 COM5)")


HttpRequester = Callable[[str, str, dict[str, Any] | None, float], dict[str, Any]]
SerialCommander = Callable[[dict[str, Any], float], dict[str, Any]]


class WorkbenchClient:
    """WifiRack 工作台客户端（线程安全，可注入假传输层做单测）。"""

    def __init__(
        self,
        config: WorkbenchConfig,
        *,
        http_requester: HttpRequester | None = None,
        serial_commander: SerialCommander | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        poll_interval_sec: float = 1.0,
    ) -> None:
        self.config = config
        self._http_requester = http_requester
        self._serial_commander = serial_commander
        self._clock = clock
        self._sleeper = sleeper
        self._poll_interval = max(0.1, poll_interval_sec)
        self._serial_lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.config.enabled)

    # ── 传输层分发 ──

    def _transport_order(self) -> list[str]:
        mode = (self.config.transport or "auto").strip().lower()
        if mode == "http":
            return ["http"]
        if mode == "serial":
            return ["serial"]
        # auto: 配置了明确串口号时优先串口（断网/切专网不受影响），否则优先 HTTP 再回退串口
        if self.config.serial_port.strip() and self.config.serial_port.strip().lower() != "none":
            return ["serial", "http"]
        return ["http"]

    def _call(
        self,
        *,
        http_method: str,
        http_path: str,
        serial_cmd: str,
        payload: dict[str, Any] | None = None,
        timeout_sec: float | None = None,
    ) -> tuple[dict[str, Any], str]:
        timeout = timeout_sec or self.config.request_timeout_sec
        errors: list[str] = []
        for channel in self._transport_order():
            try:
                if channel == "http":
                    return self._call_http(http_method, http_path, payload, timeout), "http"
                return self._call_serial(serial_cmd, payload or {}, timeout), "serial"
            except Exception as exc:
                errors.append(f"{channel}: {exc}")
        raise RuntimeError("工作台通信失败 (" + "; ".join(errors) + ")")

    def _call_http(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None,
        timeout: float,
    ) -> dict[str, Any]:
        if self._http_requester is not None:
            return self._http_requester(method, path, payload, timeout)
        url = f"{self.config.base_url.rstrip('/')}{path}"
        headers: dict[str, str] = {}
        if self.config.api_token:
            headers["Authorization"] = f"Bearer {self.config.api_token}"
        with httpx.Client(timeout=timeout, trust_env=False) as client:
            if method.upper() == "GET":
                resp = client.get(url, headers=headers)
            else:
                resp = client.post(url, headers=headers, json=payload or {})
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict):
                raise RuntimeError("工作台返回了非 JSON 对象")
            return data

    def _call_serial(
        self,
        cmd: str,
        payload: dict[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        req_id = uuid.uuid4().hex[:8]
        frame = {"id": req_id, "cmd": cmd, **payload}
        if self._serial_commander is not None:
            return self._serial_commander(frame, timeout)

        try:
            import serial  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError("未安装 pyserial，无法使用串口模式控制工作台") from exc

        port = self.config.serial_port.strip()
        if not port or port.lower() == "auto":
            port = _auto_detect_serial_port()

        line = (
            json.dumps(frame, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        with self._serial_lock, serial.Serial(port, self.config.baudrate, timeout=0.4) as ser:
            ser.reset_input_buffer()
            ser.write(line)
            ser.flush()
            deadline = self._clock() + timeout
            while self._clock() < deadline:
                raw = ser.readline()
                if not raw:
                    continue
                text = raw.decode("utf-8", errors="ignore").strip()
                if not text.startswith(SERIAL_PREFIX):
                    continue
                body = text[len(SERIAL_PREFIX):].strip()
                try:
                    parsed = json.loads(body)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict) and str(parsed.get("id") or "") in {"", req_id}:
                    return parsed
        raise TimeoutError(f"串口 {port} 响应超时 (cmd={cmd})")

    # ── 业务能力接口 ──

    def get_status(self) -> dict[str, Any]:
        """获取工作台 6 槽实时状态、母线电压与当前可见热点。"""
        data, transport = self._call(
            http_method="GET",
            http_path="/api/status",
            serial_cmd="status",
        )
        data.setdefault("transport", transport)
        return data

    def list_online_ssids(self) -> set[str]:
        """返回工作台已知当前在线的 WiFi SSID 集合（含已认槽与未认槽热点）。"""
        try:
            status = self.get_status()
        except Exception as exc:
            logger.debug("读取工作台在线 SSID 失败: {}", exc)
            return set()
        found: set[str] = set()
        for slot in status.get("slots") or []:
            if not isinstance(slot, dict):
                continue
            slot_ssid = str(slot.get("ssid") or "").strip()
            if slot.get("wifiOnline") and slot_ssid:
                found.add(slot_ssid)
        for item in status.get("visibleKnown") or []:
            if isinstance(item, dict) and str(item.get("ssid") or "").strip():
                found.add(str(item["ssid"]).strip())
        return found

    def wake_by_ssid(
        self,
        ssid: str,
        *,
        name: str = "",
        wifi_password: str = "",
        slot: int = 0,
        allow_cold_boot: bool = True,
        wait: bool = True,
        timeout_sec: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> WorkbenchWakeResult:
        """按 SSID（或场站名/槽位号）动态查找并唤醒设备。

        Args:
            ssid: 目标随身 WiFi SSID。
            name: 场站名（可选，辅助匹配与工作台展示）。
            wifi_password: 随身 WiFi 密码（可选，供工作台脉冲认槽/读电量）。
            slot: 指定槽位 1..6；为 0 时走免固定槽位动态查找。
            allow_cold_boot: 允许在短按无响应时自动冷启离线槽。
            wait: 是否轮询直到唤醒结束。
            timeout_sec: 轮询总超时秒数。
            cancelled: 返回 True 时立即停止等待。
        """
        target_ssid = ssid.strip()
        target_name = name.strip()
        if not target_ssid and not target_name and not (1 <= slot <= 6):
            return WorkbenchWakeResult(ok=False, status="failed", message="缺少目标 SSID 或槽位")

        payload: dict[str, Any] = {
            "ssid": target_ssid,
            "name": target_name,
            "allowColdBoot": bool(allow_cold_boot),
        }
        if wifi_password:
            payload["wifiPass"] = wifi_password
        if 1 <= slot <= 6:
            payload["slot"] = slot

        try:
            data, transport = self._call(
                http_method="POST",
                http_path="/api/wake",
                serial_cmd="wake",
                payload=payload,
            )
        except Exception as exc:
            return WorkbenchWakeResult(
                ok=False,
                status="failed",
                ssid=target_ssid,
                message=str(exc),
            )

        job_id = int(data.get("jobId") or 0)
        status = str(data.get("status") or ("queued" if job_id else "failed"))
        terminal_states = {"already_online", "woke", "not_found", "empty", "failed"}
        if not wait or data.get("done") or status in terminal_states:
            return self._result_from_job(data, target_ssid, transport)

        budget = timeout_sec if timeout_sec is not None else self.config.wake_timeout_sec
        deadline = self._clock() + max(1.0, budget)
        last_data = data
        while self._clock() < deadline:
            if cancelled and cancelled():
                return WorkbenchWakeResult(
                    ok=False,
                    status="cancelled",
                    ssid=target_ssid,
                    job_id=job_id,
                    message="已取消等待工作台唤醒",
                    transport=transport,
                )
            self._sleeper(min(self._poll_interval, max(0.1, deadline - self._clock())))
            if cancelled and cancelled():
                return WorkbenchWakeResult(
                    ok=False,
                    status="cancelled",
                    ssid=target_ssid,
                    job_id=job_id,
                    message="已取消等待工作台唤醒",
                    transport=transport,
                )
            try:
                polled, transport = self._call(
                    http_method="GET",
                    http_path=f"/api/job?id={job_id}",
                    serial_cmd="job",
                    payload={"jobId": job_id},
                )
                last_data = polled
                polled_status = str(polled.get("status") or "")
                if polled.get("done") or polled_status not in {"queued", "running"}:
                    return self._result_from_job(polled, target_ssid, transport)
            except Exception as exc:
                logger.debug("轮询工作台唤醒任务 #{} 暂出错: {}", job_id, exc)

        return WorkbenchWakeResult(
            ok=False,
            status="timeout",
            ssid=target_ssid,
            slot_id=int(last_data.get("slotId") or 0),
            job_id=job_id,
            message=f"等待工作台唤醒 {target_ssid or target_name} 超时",
            transport=transport,
        )

    def prewake_by_ssid(
        self,
        ssid: str,
        *,
        name: str = "",
        wifi_password: str = "",
    ) -> WorkbenchWakeResult:
        """非阻塞预唤醒下一台随身 WiFi（跑第 N 台时预唤醒第 N+1 台）。"""
        target_ssid = ssid.strip()
        target_name = name.strip()
        if not target_ssid and not target_name:
            return WorkbenchWakeResult(ok=False, status="failed", message="缺少目标 SSID")
        payload: dict[str, Any] = {
            "ssid": target_ssid,
            "name": target_name,
            "allowColdBoot": True,
        }
        if wifi_password:
            payload["wifiPass"] = wifi_password
        try:
            data, transport = self._call(
                http_method="POST",
                http_path="/api/prewake",
                serial_cmd="prewake",
                payload=payload,
            )
            return WorkbenchWakeResult(
                ok=bool(data.get("ok", True)),
                status=str(data.get("status") or "queued"),
                ssid=target_ssid,
                slot_id=int(data.get("slotId") or 0),
                job_id=int(data.get("jobId") or 0),
                message=str(data.get("message") or "预唤醒已提交"),
                transport=transport,
            )
        except Exception as exc:
            return WorkbenchWakeResult(
                ok=False,
                status="failed",
                ssid=target_ssid,
                message=str(exc),
            )

    def wake_all(self) -> dict[str, Any]:
        """一键唤醒工作台全部在位随身 WiFi。"""
        data, transport = self._call(
            http_method="POST",
            http_path="/api/wake-all",
            serial_cmd="wake_all",
        )
        data.setdefault("transport", transport)
        return data

    def discover_slots(self, *, wait: bool = False, timeout_sec: float = 90.0) -> dict[str, Any]:
        """触发工作台全槽动态认槽（自动识别各槽插的哪个 WiFi）。"""
        data, transport = self._call(
            http_method="POST",
            http_path="/api/discover",
            serial_cmd="discover",
        )
        data.setdefault("transport", transport)
        job_id = int(data.get("jobId") or 0)
        if not wait or not job_id or data.get("done"):
            return data
        deadline = self._clock() + max(1.0, timeout_sec)
        while self._clock() < deadline:
            self._sleeper(min(self._poll_interval, max(0.1, deadline - self._clock())))
            polled, transport = self._call(
                http_method="GET",
                http_path=f"/api/job?id={job_id}",
                serial_cmd="job",
                payload={"jobId": job_id},
            )
            polled.setdefault("transport", transport)
            if polled.get("done") or str(polled.get("status") or "") not in {"queued", "running"}:
                return polled
        return data

    def sync_profiles(self, profiles: Sequence[Any]) -> dict[str, Any]:
        """把 EXE 场站档案（名称 + WiFi SSID + 密码）同步给工作台用于动态认槽。"""
        rows: list[dict[str, str]] = []
        for item in profiles:
            if isinstance(item, dict):
                raw_ssid = item.get("wifi_ssid") or item.get("wifiSsid") or item.get("ssid") or ""
                ssid = str(raw_ssid).strip()
                name = str(item.get("name") or ssid).strip()
                raw_pass = (
                    item.get("wifi_password")
                    or item.get("wifiPassword")
                    or item.get("wifiPass")
                    or ""
                )
                wifi_pass = str(raw_pass)
            else:
                ssid = str(getattr(item, "wifi_ssid", "") or "").strip()
                name = str(getattr(item, "name", "") or ssid).strip()
                wifi_pass = str(getattr(item, "wifi_password", "") or "")
            if not ssid:
                continue
            rows.append({"name": name, "ssid": ssid, "wifiPass": wifi_pass})
        data, transport = self._call(
            http_method="POST",
            http_path="/api/sync-profiles",
            serial_cmd="sync_profiles",
            payload={"profiles": rows},
        )
        data.setdefault("transport", transport)
        data.setdefault("synced", len(rows))
        return data

    def set_slot_charge(self, slot: int, *, on: bool, minutes: int = 30) -> dict[str, Any]:
        """控制指定槽位磁吸供电开关。"""
        data, transport = self._call(
            http_method="POST",
            http_path=f"/api/slot/{slot}/charge",
            serial_cmd="charge",
            payload={"slot": slot, "on": bool(on), "minutes": int(minutes)},
        )
        data.setdefault("transport", transport)
        return data

    @staticmethod
    def _result_from_job(
        data: dict[str, Any], target_ssid: str, transport: str
    ) -> WorkbenchWakeResult:
        status = str(data.get("status") or "failed")
        ok = bool(data.get("online")) or status in {"already_online", "woke"}
        return WorkbenchWakeResult(
            ok=ok,
            status=status,
            ssid=str(data.get("targetSsid") or target_ssid),
            slot_id=int(data.get("slotId") or 0),
            rssi=int(data.get("rssi") if data.get("rssi") is not None else -127),
            job_id=int(data.get("jobId") or 0),
            message=str(data.get("message") or status),
            transport=transport,
        )
