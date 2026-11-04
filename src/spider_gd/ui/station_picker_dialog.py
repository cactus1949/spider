"""本机场站选择窗口。

独立进程运行，避免和浏览器、接口服务抢界面线程。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path


def match_station_id(stations: list[dict[str, str]], choice: str) -> str | None:
    """按编号或名称找到场站编号。

    Args:
        stations: 窗口收到的场站行。
        choice: 编号或完整名称。

    Returns:
        str | None: 命中的编号；没有匹配时为 None。

    Examples:
        >>> match_station_id([{"id": "a", "name": "阳江"}], "阳江")
        'a'
    """
    key = choice.strip()
    if not key:
        return None
    for station in stations:
        if station.get("id") == key or station.get("name") == key:
            station_id = str(station.get("id") or "").strip()
            return station_id or None
    return None


def load_request(path: Path) -> list[dict[str, str]]:
    """读取窗口要展示的场站。

    Args:
        path: 请求 JSON 路径。

    Returns:
        list[dict[str, str]]: 场站行。缺字段时补成空字符串。

    Raises:
        ValueError: 文件不是约定的 JSON 对象。

    Examples:
        >>> isinstance(load_request, object)
        True
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("stations"), list):
        raise ValueError("场站选择请求格式不正确")
    rows: list[dict[str, str]] = []
    for item in data["stations"]:
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "id": str(item.get("id") or ""),
                "name": str(item.get("name") or ""),
                "wifiSsid": str(item.get("wifiSsid") or ""),
                "status": str(item.get("status") or "已插入"),
                "deviceId": str(item.get("deviceId") or ""),
                "orgName": str(item.get("orgName") or ""),
            }
        )
    return rows


def request_is_multiple(path: Path) -> bool:
    """读取请求里是否要求多选。

    Args:
        path: 请求 JSON 路径。

    Returns:
        bool: ``multiple`` 为真时允许多选。

    Examples:
        >>> import tempfile
        >>> folder = tempfile.mkdtemp()
        >>> target = Path(folder) / "req.json"
        >>> _ = target.write_text('{"multiple": true}', encoding="utf-8")
        >>> request_is_multiple(target)
        True
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool(data.get("multiple")) if isinstance(data, dict) else False


def match_station_ids(stations: list[dict[str, str]], choice: str) -> list[str] | None:
    """按逗号分隔的编号或名称，按书写顺序找出场站。

    Args:
        stations: 窗口收到的场站行。
        choice: 一个或多个场站，用逗号分隔。

    Returns:
        list[str] | None: 场站编号。任一名称找不到时为 None。

    Examples:
        >>> rows = [{"id": "a", "name": "阳江"}, {"id": "b", "name": "阳西"}]
        >>> match_station_ids(rows, "阳西,阳江")
        ['b', 'a']
    """
    parts = [part.strip() for part in choice.replace("，", ",").split(",")]
    parts = [part for part in parts if part]
    if not parts:
        return None
    found: list[str] = []
    for part in parts:
        station_id = match_station_id(stations, part)
        if station_id is None:
            return None
        found.append(station_id)
    return found


def result_payload(
    *,
    station_id: str = "",
    station_ids: list[str] | None = None,
    cancelled: bool = False,
) -> dict[str, object]:
    """组装窗口进程写回的结果，不访问磁盘。

    Args:
        station_id: 单选时的场站编号。
        station_ids: 多选时按勾选顺序排列的编号。传入后以它为准。
        cancelled: 为 True 时表示用户取消。

    Returns:
        dict[str, object]: 含 ``id``、``ids`` 或 ``cancelled`` 的结果。

    Examples:
        >>> result_payload(station_id="a")
        {'id': 'a', 'cancelled': False}
        >>> result_payload(station_ids=["b", "a"])["ids"]
        ['b', 'a']
        >>> result_payload(cancelled=True)
        {'cancelled': True}
    """
    if station_ids is not None:
        ids = [item.strip() for item in station_ids if item.strip()]
        if cancelled or not ids:
            return {"cancelled": True, "ids": []}
        return {"id": ids[0], "ids": ids, "cancelled": False}
    if cancelled or not station_id:
        return {"cancelled": True}
    return {"id": station_id, "cancelled": False}


def write_result(
    path: Path,
    *,
    station_id: str = "",
    station_ids: list[str] | None = None,
    cancelled: bool = False,
) -> None:
    """把选择结果写到约定文件。

    Args:
        path: 结果 JSON 路径。
        station_id: 单选时的场站编号。
        station_ids: 多选时的场站编号。
        cancelled: 为 True 时表示用户取消。

    Returns:
        None: 只写文件。

    Examples:
        >>> result_payload(station_id="demo")["id"]
        'demo'
    """
    path.write_text(
        json.dumps(
            result_payload(
                station_id=station_id,
                station_ids=station_ids,
                cancelled=cancelled,
            ),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def run_dialog(stations: list[dict[str, str]]) -> str | None:
    """弹出本机场站选择窗口。

    Args:
        stations: 场站行，每项含 ``id``、``name``、``wifiSsid``。

    Returns:
        str | None: 选中的场站编号；取消或没有场站时为 None。

    Examples:
        >>> run_dialog([]) is None
        True
    """
    if not stations:
        return None
    import tkinter as tk
    from tkinter import ttk

    chosen: dict[str, str | None] = {"id": None}
    root = tk.Tk()
    root.title("证书选择")
    root.resizable(False, False)
    with contextlib.suppress(tk.TclError):
        root.attributes("-topmost", True)
    font_name = "PingFang SC" if sys.platform == "darwin" else "Microsoft YaHei"
    frame = ttk.Frame(root, padding=16)
    frame.grid(row=0, column=0, sticky="nsew")

    ttk.Label(frame, text="选择登录场站", font=(font_name, 16, "bold")).grid(
        row=0, column=0, sticky="w"
    )
    ttk.Label(
        frame,
        text="检测到多把 UKey。请选择要进入的场站，确认后使用该场站对应的 WiFi 和证书密码。",
        wraplength=460,
    ).grid(row=1, column=0, sticky="w", pady=(6, 12))

    columns = ("name", "wifi", "status")
    table = ttk.Treeview(frame, columns=columns, show="headings", height=min(8, len(stations)))
    table.heading("name", text="场站")
    table.heading("wifi", text="WiFi")
    table.heading("status", text="状态")
    table.column("name", width=240, anchor="w")
    table.column("wifi", width=140, anchor="w")
    table.column("status", width=80, anchor="center")
    for station in stations:
        table.insert(
            "",
            "end",
            iid=station["id"],
            values=(station["name"], station["wifiSsid"], station["status"]),
        )
    table.grid(row=2, column=0, sticky="ew")
    first = table.get_children()[0]
    table.selection_set(first)
    table.focus(first)

    hint = ttk.Label(frame, text="选中一行后点击确认。")
    hint.grid(row=3, column=0, sticky="w", pady=(8, 0))

    def confirm() -> None:
        """记录当前选中行并关闭窗口。"""
        selected = table.selection()
        if not selected:
            hint.configure(text="请先选择一个场站。")
            return
        chosen["id"] = selected[0]
        root.destroy()

    def cancel() -> None:
        """关闭窗口且不选择场站。"""
        chosen["id"] = None
        root.destroy()

    buttons = ttk.Frame(frame)
    buttons.grid(row=4, column=0, sticky="e", pady=(12, 0))
    ttk.Button(buttons, text="取消", command=cancel).grid(row=0, column=0, padx=(0, 8))
    confirm_button = ttk.Button(buttons, text="确认", command=confirm)
    confirm_button.grid(row=0, column=1)
    table.bind("<Double-1>", lambda _event: confirm())
    root.bind("<Return>", lambda _event: confirm())
    root.bind("<Escape>", lambda _event: cancel())
    root.protocol("WM_DELETE_WINDOW", cancel)
    root.update_idletasks()
    width, height = 520, 360
    left = max(0, (root.winfo_screenwidth() - width) // 2)
    top = max(0, (root.winfo_screenheight() - height) // 2)
    root.geometry(f"{width}x{height}+{left}+{top}")
    confirm_button.focus_set()
    root.mainloop()
    return chosen["id"]


def run_multi_dialog(stations: list[dict[str, str]]) -> list[str] | None:
    """弹出可多选的场站窗口，按勾选顺序返回。

    Args:
        stations: 场站行，每项含 ``id``、``name``、``wifiSsid``、``deviceId``。

    Returns:
        list[str] | None: 勾选顺序中的场站编号。取消或没有场站时为 None。

    Examples:
        >>> run_multi_dialog([]) is None
        True
    """
    if not stations:
        return None
    import tkinter as tk
    from tkinter import ttk

    chosen: dict[str, list[str] | None] = {"ids": None}
    order: list[str] = []
    root = tk.Tk()
    root.title("选择执行场站")
    root.resizable(False, False)
    with contextlib.suppress(tk.TclError):
        root.attributes("-topmost", True)
    font_name = "PingFang SC" if sys.platform == "darwin" else "Microsoft YaHei"
    frame = ttk.Frame(root, padding=16)
    frame.grid(row=0, column=0, sticky="nsew")
    ttk.Label(frame, text="选择本次执行的场站", font=(font_name, 16, "bold")).grid(
        row=0, column=0, sticky="w"
    )
    ttk.Label(
        frame,
        text=(
            "检测到多把 UKey。勾选要执行的场站，可以只选一个，也可以选多个。"
            "执行顺序就是勾选顺序。每个场站会用它自己的 WiFi 和证书密码登录。"
        ),
        wraplength=560,
    ).grid(row=1, column=0, sticky="w", pady=(6, 12))

    table = ttk.Frame(frame)
    table.grid(row=2, column=0, sticky="ew")
    variables: dict[str, tk.BooleanVar] = {}

    def describe() -> str:
        """把当前勾选顺序写成一行说明。"""
        names = []
        for station_id in order:
            station = next((item for item in stations if item["id"] == station_id), None)
            names.append(station["name"] if station else station_id)
        if not names:
            return "尚未勾选。勾选顺序就是场站执行顺序。"
        return "执行顺序：" + " → ".join(names)

    hint = ttk.Label(frame, text=describe(), wraplength=560)

    def toggle(station_id: str) -> None:
        """按勾选或取消维护执行顺序。"""
        if variables[station_id].get():
            if station_id not in order:
                order.append(station_id)
        elif station_id in order:
            order.remove(station_id)
        hint.configure(text=describe())

    for index, station in enumerate(stations):
        variable = tk.BooleanVar(value=False)
        variables[station["id"]] = variable
        code = station.get("deviceId") or ""
        short_code = code[-12:] if len(code) > 12 else code
        bound = not station["id"].startswith("usb:")
        status = "已绑定场站名" if bound else "未绑定场站名"
        org_name = station.get("orgName") or "未配置"
        wifi_name = station.get("wifiSsid") or "未配置"
        text = f"{station['name']}    单位 {org_name}    WiFi {wifi_name}    {status}"
        if short_code:
            text += f"    特征码 {short_code}"
        ttk.Checkbutton(
            table,
            text=text,
            variable=variable,
            command=lambda station_id=station["id"]: toggle(station_id),
        ).grid(row=index, column=0, sticky="w", pady=2)

    hint.grid(row=3, column=0, sticky="w", pady=(8, 0))

    def select_all() -> None:
        """按列表顺序勾选全部场站。"""
        order.clear()
        for station in stations:
            variables[station["id"]].set(True)
            order.append(station["id"])
        hint.configure(text=describe())

    def confirm() -> None:
        """保存勾选顺序并关闭窗口。"""
        if not order:
            hint.configure(text="请至少勾选一个场站。")
            return
        chosen["ids"] = list(order)
        root.destroy()

    def cancel() -> None:
        """关闭窗口且不执行。"""
        chosen["ids"] = None
        root.destroy()

    buttons = ttk.Frame(frame)
    buttons.grid(row=4, column=0, sticky="e", pady=(12, 0))
    ttk.Button(buttons, text="全选", command=select_all).grid(row=0, column=0, padx=(0, 8))
    ttk.Button(buttons, text="取消", command=cancel).grid(row=0, column=1, padx=(0, 8))
    confirm_button = ttk.Button(buttons, text="确认", command=confirm)
    confirm_button.grid(row=0, column=2)
    root.bind("<Return>", lambda _event: confirm())
    root.bind("<Escape>", lambda _event: cancel())
    root.protocol("WM_DELETE_WINDOW", cancel)
    root.update_idletasks()
    width, height = 640, min(520, 220 + 28 * len(stations))
    left = max(0, (root.winfo_screenwidth() - width) // 2)
    top = max(0, (root.winfo_screenheight() - height) // 2)
    root.geometry(f"{width}x{height}+{left}+{top}")
    confirm_button.focus_set()
    root.mainloop()
    return chosen["ids"]


def main(argv: list[str] | None = None) -> int:
    """按命令行参数选择场站并写出结果。

    Args:
        argv: 参数列表；None 时读取进程参数。

    Returns:
        int: 0 表示已选择，2 表示取消，1 表示失败。

    Examples:
        >>> main(["--help"])  # doctest: +SKIP
        0
    """
    parser = argparse.ArgumentParser(description="弹出本机场站选择窗口")
    parser.add_argument("--input", required=True, help="场站请求 JSON")
    parser.add_argument("--output", required=True, help="选择结果 JSON")
    parser.add_argument("--choice", default="", help="直接指定场站编号或名称，不打开窗口")
    args = parser.parse_args(argv)
    result_path = Path(args.output)
    try:
        stations = load_request(Path(args.input))
        multiple = request_is_multiple(Path(args.input))
        if args.choice.strip():
            if multiple:
                station_ids = match_station_ids(stations, args.choice)
                if station_ids is None:
                    print(f"没有名为「{args.choice.strip()}」的场站", file=sys.stderr)
                    return 1
                write_result(result_path, station_ids=station_ids)
                return 0
            station_id = match_station_id(stations, args.choice)
            if station_id is None:
                print(f"没有名为「{args.choice.strip()}」的场站", file=sys.stderr)
                return 1
            write_result(result_path, station_id=station_id)
            return 0
        if multiple:
            station_ids = run_multi_dialog(stations)
            if not station_ids:
                write_result(result_path, station_ids=[], cancelled=True)
                return 2
            write_result(result_path, station_ids=station_ids)
            return 0
        station_id = run_dialog(stations)
    except Exception as exc:
        print(f"无法打开场站选择窗口：{exc}", file=sys.stderr)
        return 1
    if not station_id:
        write_result(result_path, cancelled=True)
        return 2
    write_result(result_path, station_id=station_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
