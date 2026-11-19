"""构建脚本：用 PyInstaller 将采集后端编译为独立 exe，输出到 frontend/resources/backend/。

用法（必须在要发布的那个 Windows 里、用 x64 Python 运行）：
    python build_backend.py                 # 编译。会先用当前解释器安装 .[online,win]
    python build_backend.py --with-browsers # 编译 + 打包 Playwright Chromium
    python build_backend.py --allow-arm64   # 仅当目标机器也是 Windows ARM64 时使用

注意：
    PyInstaller 不能跨平台编译。在 macOS 上编译的输出只能在 macOS 运行。
    要发布 Windows 版，需要在 Windows 机器上运行此脚本。
    Mac 上的 Parallels / UTM 若装的是 Windows ARM64，默认 Python 往往也是 ARM64，
    打出来的 exe 无法在现场的 x64 Windows 上运行。请安装 python.org 的
    Windows installer (64-bit)，不要选 ARM64，并用那个解释器运行本脚本。
    脚本会用 ``sys.executable -m pip`` 把 WMI 和 pywin32 装进同一个解释器。
    漏装或装错解释器时，冻结后的 U-Key 健康检查会报「WMI 未安装」。

前置：
    python -m pip install pyinstaller
"""

import ast
import contextlib
import importlib
import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "frontend" / "resources" / "backend"
ENTRY = ROOT / "backend_entry.py"
SRC = ROOT / "src"
PACKAGE = SRC / "spider_gd"
BUILD = ROOT / "build" / "pyinstaller"

# 平台相关
IS_WIN = sys.platform == "win32"
EXE_SUFFIX = ".exe" if IS_WIN else ""
APP_NAME = "spider-gd-service"

# 需要作为 data 打包的文件/目录：(源绝对路径, bundle 内相对路径)
DATA_FILES: list[tuple[str, str]] = [
    (str(ROOT / "configs"), "configs"),
    (str(ROOT / "workflows"), "workflows"),
    (str(ROOT / "tools"), "tools"),
]

# 动态 import 的模块（PyInstaller 静态分析不到）
HIDDEN_IMPORTS = [
    "spider_gd.config.settings",
    "spider_gd.config.logging",
    "spider_gd.config.deploy_mode",
    "spider_gd.gateway.control_service",
    "spider_gd.gateway.api.server",
    "spider_gd.gateway.alert.alert_gateway",
    "spider_gd.gateway.agent.agent_adapter",
    "spider_gd.gateway.agent.command_executor",
    "spider_gd.gateway.agent.handlers",
    "spider_gd.gateway.agent.permission_guard",
    "spider_gd.gateway.agent.stream_client",
    "spider_gd.core.crawler.api_fetcher",
    "spider_gd.core.crawler.captcha_solver",
    "spider_gd.core.crawler.crawl_orchestrator",
    "spider_gd.core.crawler.login_flow",
    "spider_gd.core.crawler.playwright_runner",
    "spider_gd.core.crawler.rpa_fetcher",
    "spider_gd.core.crawler.session_keeper",
    "spider_gd.core.crawler.session_provider",
    "spider_gd.core.pipeline.local_landing",
    "spider_gd.core.pipeline.data_processor",
    "spider_gd.core.report_dates",
    "spider_gd.core.ukey_plan",
    "spider_gd.core.custom_filename",
    "spider_gd.infrastructure.windows.cert_dialog",
    "spider_gd.infrastructure.windows.cert_availability",
    "spider_gd.infrastructure.network.wifi_wait",
    "spider_gd.core.scheduler.task_scheduler",
    "spider_gd.core.scheduler.backfill",
    "spider_gd.infrastructure.network.network_validator",
    "spider_gd.infrastructure.storage.database",
    "spider_gd.infrastructure.storage.retention",
    # tools 目录
    "workflow_runner",
    # 第三方 (uvicorn 子模块需要显式声明)
    "uvicorn",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "fastapi",
    "starlette",
    "pydantic",
    "pydantic_settings",
    "yaml",
    "playwright",
    "apscheduler",
    "loguru",
    "httpx",
    # curl_cffi 可选：提供 TLS 指纹伪装（JA3/JA4 绕过）
    "openpyxl",
    "pandas",
    "ddddocr",
    # 场站窗口在打包后由同一个 exe 以 --station-picker 拉起，tk 在函数内部才 import
    "spider_gd.ui.station_picker_dialog",
    "spider_gd.core.station_picker",
    "tkinter",
    "tkinter.ttk",
]

# wmi 在函数里延迟导入，PyInstaller 静态分析看不到。
# pywin32 还要带上 pythoncom / pywintypes 的 DLL，否则冻结进程 import wmi 会失败。
WMI_HIDDEN_IMPORTS = [
    "wmi",
    "win32com",
    "win32com.client",
    "win32com.client.dynamic",
    "win32com.client.gencache",
    "pythoncom",
    "pywintypes",
    "win32api",
    "win32timezone",
]
_WMI_PROBE = "import wmi, win32com.client, pythoncom, pywintypes"
_ARM64_MACHINES = {"arm64", "aarch64"}


def windows_wmi_pyinstaller_args(*, is_win: bool, dlls: list[Path] | None = None) -> list[str]:
    """生成让冻结包带上 WMI / pywin32 的 PyInstaller 参数。

    Args:
        is_win: 当前是不是 Windows。其他系统不要加这些参数，否则会去找不存在的模块。
        dlls: ``pythoncom`` / ``pywintypes`` 的 DLL。放进包根目录，供冻结进程加载。

    Returns:
        list[str]: 追加到 PyInstaller 命令后面的参数。非 Windows 时为空。

    Examples:
        >>> args = windows_wmi_pyinstaller_args(is_win=True, dlls=[])
        >>> "wmi" in args and "pythoncom" in args and "pywintypes" in args
        True
        >>> windows_wmi_pyinstaller_args(is_win=False)
        []
    """
    if not is_win:
        return []
    args: list[str] = []
    for mod in WMI_HIDDEN_IMPORTS:
        args += ["--hidden-import", mod]
    args += ["--collect-submodules", "win32com"]
    args += ["--collect-binaries", "win32"]
    for dll in dlls or []:
        args += ["--add-binary", f"{dll}{os.pathsep}."]
    return args


def pywin32_dlls(search_roots: list[Path]) -> list[Path]:
    """在 site-packages 里找出 pywin32 运行时 DLL。

    Args:
        search_roots: 要查看的目录，通常是 site-packages 和 ``pywin32_system32``。

    Returns:
        list[Path]: ``pythoncom*.dll`` 与 ``pywintypes*.dll``，同名只留一份。

    Examples:
        >>> pywin32_dlls([])
        []
    """
    found: list[Path] = []
    seen: set[str] = set()
    folders: list[Path] = []
    for root in search_roots:
        folders.append(root)
        folders.append(root / "pywin32_system32")
        folders.append(root / "win32")
    for folder in folders:
        if not folder.is_dir():
            continue
        for dll in folder.glob("*.dll"):
            name = dll.name.lower()
            if name in seen:
                continue
            if name.startswith(("pythoncom", "pywintypes")):
                seen.add(name)
                found.append(dll)
        for binary in folder.glob("*.pyd"):
            name = binary.name.lower()
            if name in seen:
                continue
            if name.startswith(("pythoncom", "pywintypes")):
                seen.add(name)
                found.append(binary)
    return found


def is_arm64_python(arch: str | None = None) -> bool:
    """判断这个解释器是不是 Windows ARM64 / aarch64。

    x64 Python 跑在 ARM 虚拟机的仿真层上时，``platform.machine()`` 仍是 AMD64。
    那种情况可以给现场的 x64 电脑出包。

    Args:
        arch: ``platform.machine()`` 的结果。空则读取当前解释器。

    Returns:
        bool: 架构名是 ``ARM64`` 或 ``aarch64`` 时为 True。

    Examples:
        >>> is_arm64_python("ARM64")
        True
        >>> is_arm64_python("AMD64")
        False
    """
    machine = platform.machine() if arch is None else arch
    return machine.lower() in _ARM64_MACHINES


def arm64_windows_python_hint(arch: str) -> str:
    """说明为什么 Mac 上的 Windows 虚拟机必须用 x64 Python 打包。

    Args:
        arch: 当前解释器报告的 CPU 架构。

    Returns:
        str: 给构建日志和退出信息看的中文说明。

    Examples:
        >>> "x64" in arm64_windows_python_hint("ARM64")
        True
    """
    return (
        f"当前 Python 是 {arch}。在 Apple Silicon 的 Parallels / UTM 里，"
        "Windows 经常是 ARM64，打出来的 spider-gd-service.exe 也是 ARM64，"
        "不能在现场的 x64 Windows 上运行。\n"
        "请改用 x64 Python：\n"
        "  1. 打开 https://www.python.org/downloads/windows/\n"
        "  2. 下载 Windows installer (64-bit)，不要选 ARM64\n"
        "  3. 用那个 python.exe 运行 build_backend.py\n"
        "  4. 自检：python -c \"import platform; print(platform.machine())\"\n"
        "     应打印 AMD64。\n"
        "pywin32 312 起才有 win_arm64 wheel。只有目标电脑也是 Windows ARM64 时，"
        "才加 --allow-arm64。"
    )


def win_extra_install_command(python: str) -> list[str]:
    """构造安装命令，解释器必须和即将运行的 PyInstaller 是同一个。

    Args:
        python: ``sys.executable``。不要用 PATH 里随便一个 pip。

    Returns:
        list[str]: ``python -m pip install -e .[online,win]``。

    Examples:
        >>> cmd = win_extra_install_command(r"C:\\Python312\\python.exe")
        >>> cmd[:3] == [r"C:\\Python312\\python.exe", "-m", "pip"]
        True
        >>> cmd[-1]
        '.[online,win]'
    """
    return [python, "-m", "pip", "install", "-e", ".[online,win]"]


def wmi_import_failure_message(detail: str, *, arch: str, python: str) -> str:
    """把 WMI 导入失败写成会让打包中止的说明。

    Args:
        detail: ``python -c "import wmi..."`` 的标准错误。
        arch: 当前解释器架构。
        python: 实际用来打包的解释器路径。

    Returns:
        str: 多行中文错误。

    Examples:
        >>> text = wmi_import_failure_message("No module named wmi", arch="AMD64", python="py")
        >>> "import wmi" in text
        True
    """
    lines = [
        "当前解释器无法 import wmi / win32com.client。",
        "PyInstaller 只会打包这个解释器里已经装好的模块，冻结后的 U-Key 检查会报「WMI 未安装」。",
        f"解释器: {python}",
        f"架构: {arch}",
        detail,
    ]
    if is_arm64_python(arch):
        lines.append(arm64_windows_python_hint(arch))
    return "\n".join(line for line in lines if line)


def probe_wmi_import(python: str) -> str:
    """在指定解释器里试着导入 WMI 和 pywin32。

    Args:
        python: 解释器路径。

    Returns:
        str: 成功时为空字符串；失败时是解释器的错误输出。

    Examples:
        >>> callable(probe_wmi_import)
        True
    """
    probe = subprocess.run(
        [python, "-c", _WMI_PROBE],
        capture_output=True,
        check=False,
    )
    if probe.returncode == 0:
        return ""
    return (probe.stderr or probe.stdout or b"").decode("utf-8", errors="replace").strip()


def ensure_windows_wmi(*, allow_arm64: bool) -> None:
    """把 WMI / pywin32 装进当前解释器，导入失败则中止打包。

    Args:
        allow_arm64: 为 True 时允许用 ARM64 Python 继续。默认拒绝，避免把
            ARM64 exe 发给 x64 现场机。

    Returns:
        None: 依赖可用时直接返回。

    Raises:
        SystemExit: 架构不对、pip 安装失败，或安装后仍不能导入。

    Examples:
        >>> callable(ensure_windows_wmi)
        True
    """
    arch = platform.machine()
    python = sys.executable
    print(f"解释器: {python}")
    print(f"架构: {arch}")
    if is_arm64_python(arch) and not allow_arm64:
        raise SystemExit(arm64_windows_python_hint(arch))
    if is_arm64_python(arch):
        print("[warn] 已指定 --allow-arm64，产物只能在 Windows ARM64 上运行。")
    print("=== 安装 WMI / pywin32 到当前解释器 ===")
    try:
        subprocess.run(win_extra_install_command(python), check=True, cwd=str(ROOT))
    except subprocess.CalledProcessError as exc:
        hint = arm64_windows_python_hint(arch) if is_arm64_python(arch) else ""
        raise SystemExit(
            "无法把 .[online,win] 装进当前解释器。\n"
            f"命令: {python} -m pip install -e \".[online,win]\"\n"
            f"{hint}"
        ) from exc
    detail = probe_wmi_import(python)
    if detail:
        raise SystemExit(wmi_import_failure_message(detail, arch=arch, python=python))
    binaries = pywin32_dlls(_pywin32_search_roots())
    if binaries:
        names = ", ".join(path.name for path in binaries)
        print(f"pywin32 运行库: {names}")
    else:
        print(
            "[warn] 没找到 pythoncom / pywintypes 的 DLL 或 PYD。"
            "冻结后若仍报 WMI 未安装，请确认 pywin32 装在上面这个解释器里。"
        )


def _pywin32_search_roots() -> list[Path]:
    """列出当前解释器可能放 pywin32 DLL 的目录。

    Returns:
        list[Path]: site-packages，以及 ``pywintypes`` / ``win32com`` 所在目录。

    Examples:
        >>> isinstance(_pywin32_search_roots(), list)
        True
    """
    import site

    roots: list[Path] = []
    with contextlib.suppress(OSError):
        roots.extend(Path(item) for item in site.getsitepackages())
    user_site = ""
    with contextlib.suppress(OSError):
        user_site = site.getusersitepackages()
    if user_site:
        roots.append(Path(user_site))
    for module_name in ("pywintypes", "pythoncom", "win32com"):
        try:
            module = __import__(module_name)
        except Exception:
            continue
        file_name = getattr(module, "__file__", "") or ""
        if file_name:
            roots.append(Path(file_name).resolve().parent)
    return roots


def source_package_modules(package: Path | None = None) -> list[str]:
    """按仓库里的 ``.py`` 文件列出 ``spider_gd`` 模块名。

    不靠当前解释器已经 import 过什么。开发环境能 import，只说明源码在
    ``sys.path`` 上，不能说明这些文件进了冻结包。

    Args:
        package: 包目录。默认是仓库里的 ``src/spider_gd``。

    Returns:
        list[str]: 排序后的模块名。``__init__.py`` 记成包名本身。

    Examples:
        >>> "spider_gd.core.report_dates" in source_package_modules()
        True
    """
    root = package or PACKAGE
    names: list[str] = []
    if not root.is_dir():
        return names
    for path in root.rglob("*.py"):
        if path.name == "__init__.py":
            rel = path.parent.relative_to(root.parent)
        else:
            rel = path.relative_to(root.parent).with_suffix("")
        parts = [part for part in rel.parts if part]
        if parts:
            names.append(".".join(parts))
    return sorted(set(names))


def spider_gd_imports_in(paths: list[Path]) -> list[str]:
    """读出这些源文件里写到的 ``spider_gd`` 导入。

    函数内部的延迟 import 也算。登录填表就是这样才碰到 ``report_dates`` 的。

    Args:
        paths: 要扫描的 Python 文件。

    Returns:
        list[str]: 模块名，排序去重。语法错误的文件跳过。

    Examples:
        >>> isinstance(spider_gd_imports_in([]), list)
        True
    """
    found: set[str] = set()
    for path in paths:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            else:
                continue
            for name in modules:
                if name == "spider_gd" or name.startswith("spider_gd."):
                    found.add(name)
    return sorted(found)


def runtime_tool_imports() -> list[str]:
    """登录和任务执行会加载的工具脚本里的 ``spider_gd`` 导入。

    Returns:
        list[str]: 模块名。

    Examples:
        >>> "spider_gd.core.report_dates" in runtime_tool_imports()
        True
    """
    tools = [ROOT / "tools" / "workflow_runner.py", ROOT / "tools" / "task_runner.py"]
    return spider_gd_imports_in([path for path in tools if path.is_file()])


def missing_repo_modules(imported: list[str], packaged: list[str] | None = None) -> list[str]:
    """导入了、但仓库源码里没有对应文件的模块。

    Args:
        imported: ``runtime_tool_imports`` 一类的结果。
        packaged: ``source_package_modules`` 的结果。空则现扫仓库。

    Returns:
        list[str]: 源码里不存在的模块名。

    Examples:
        >>> missing_repo_modules(["spider_gd.core.report_dates"], ["spider_gd.core.report_dates"])
        []
    """
    have = set(packaged if packaged is not None else source_package_modules())
    return [name for name in imported if name not in have]


def spider_gd_pyinstaller_args() -> list[str]:
    """生成把整个 ``spider_gd`` 打进冻结包的参数。

    登录填表会在函数里才 import。只手写几个 hidden-import 时，漏掉的模块
    在现场就是 ``No module named ...``。这里按仓库里的 ``.py`` 逐个写上。
    ``--add-data`` 必须指向 ``src/spider_gd``。0.2.0 把整个 ``src`` 标成
    ``spider_gd``，冻结目录多了一层，``core/report_dates.py`` 对不上导入路径。

    Returns:
        list[str]: 追加到 PyInstaller 命令后面的参数。

    Examples:
        >>> args = spider_gd_pyinstaller_args()
        >>> args[:2]
        ['--collect-submodules', 'spider_gd']
        >>> args.count("spider_gd.core.report_dates") >= 1
        True
    """
    args = ["--collect-submodules", "spider_gd"]
    for name in source_package_modules():
        args += ["--hidden-import", name]
    if PACKAGE.exists():
        args += ["--add-data", f"{PACKAGE}{os.pathsep}spider_gd"]
    return args


def current_build_id() -> str:
    """这次打包的构建身份。同版本不同提交也要能分开。"""
    try:
        rev = subprocess.check_output(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        rev = ""
    if not rev:
        rev = "unknown"
    return rev


def install_build_stamp(build_id: str) -> str:
    """把构建身份写进将被冻结的源码，并返回原来的文件内容。"""
    path = PACKAGE / "build_stamp.py"
    original = path.read_text(encoding="utf-8")
    path.write_text(
        '"""冻结时写入。运行中的进程读这个常量，不读磁盘上的新文件。"""\n\n'
        "from __future__ import annotations\n\n"
        f"BUILD_ID = {build_id!r}\n",
        encoding="utf-8",
    )
    return original


def restore_build_stamp(original: str) -> None:
    """打包结束后把仓库里的构建身份文件放回去。"""
    (PACKAGE / "build_stamp.py").write_text(original, encoding="utf-8")


def assert_frozen_ocr(exe: Path) -> None:
    """让刚打出来的 exe 自己导入验证码依赖。

    不是构建机直接加载 dll。非 Windows 不运行这个 exe。

    Args:
        exe: 冻结产物。

    Raises:
        SystemExit: Windows 上自测失败。
    """
    if not IS_WIN:
        print("[skip] 冻结产物的验证码自测只在 Windows 上运行该 exe")
        return
    completed = subprocess.run(
        [str(exe), "--self-test-ocr"],
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )
    output = ((completed.stdout or "") + (completed.stderr or "")).strip()
    if completed.returncode != 0 or not (completed.stdout or "").lstrip().startswith("ok"):
        raise SystemExit("冻结产物无法加载验证码依赖：\n" + output[-2000:])
    print("[done] 冻结产物已加载 onnxruntime 扩展和验证码模型")


def assert_repo_package_wins() -> None:
    """打包前确认要收进冻结包的是仓库源码，不是环境里残留的旧安装。

    Raises:
        SystemExit: ``report_dates`` 解析不到仓库文件时。

    Examples:
        >>> callable(assert_repo_package_wins)
        True
    """
    src = str(SRC)
    while src in sys.path:
        sys.path.remove(src)
    sys.path.insert(0, src)
    importlib.invalidate_caches()
    spec = importlib.util.find_spec("spider_gd.core.report_dates")
    origin = Path(spec.origin).resolve() if spec and spec.origin else None
    package = PACKAGE.resolve()
    if origin is None or package not in origin.parents:
        raise SystemExit(
            "打包解析到的 spider_gd.core.report_dates 不在这个仓库的 src 里"
            f"（实际是 {origin}）。先卸掉过期安装，再从这个仓库打包。"
        )


def certificate_ui_pyinstaller_args(*, is_win: bool) -> list[str]:
    """生成把系统证书窗口自动化库打进冻结包的参数。

    ``uiautomation`` 在点击「立即登录」时才 import。不写进参数的话，
    冻结包里没有它，证书选择窗口不会被自动点确定。

    Args:
        is_win: 当前是不是 Windows。其他系统不打这套库。

    Returns:
        list[str]: 追加到 PyInstaller 命令后面的参数。非 Windows 时为空。

    Examples:
        >>> "uiautomation" in certificate_ui_pyinstaller_args(is_win=True)
        True
        >>> certificate_ui_pyinstaller_args(is_win=False)
        []
    """
    if not is_win:
        return []
    return [
        "--hidden-import", "uiautomation",
        "--collect-submodules", "uiautomation",
        "--collect-submodules", "comtypes",
    ]


def onnxruntime_package_dir() -> Path | None:
    """定位当前解释器里的 onnxruntime 包目录。

    Returns:
        Path | None: 含 ``capi`` 的目录。没安装时为 None。

    Examples:
        >>> onnxruntime_package_dir() is None or onnxruntime_package_dir().name == "onnxruntime"
        True
    """
    try:
        import onnxruntime
    except ImportError:
        return None
    file_name = getattr(onnxruntime, "__file__", "") or ""
    if not file_name:
        return None
    return Path(file_name).resolve().parent


def onnxruntime_native_files(package_dir: Path) -> list[tuple[Path, str]]:
    """列出要和验证码 pyd 放在同一目录的本机库。

    ``onnxruntime_pybind11_state.pyd`` 按 Windows 加载规则到自己所在的
    ``onnxruntime/capi`` 找 ``onnxruntime.dll``。库被放到冻结包根目录时，
    现场就是「找不到指定的模块」。

    Args:
        package_dir: ``onnxruntime`` 包目录。

    Returns:
        list[tuple[Path, str]]: ``(本机文件, 冻结包内目录)``。

    Examples:
        >>> onnxruntime_native_files(Path("/no/such/onnxruntime"))
        []
    """
    if not package_dir.is_dir():
        return []
    found: list[tuple[Path, str]] = []
    seen: set[str] = set()
    for path in package_dir.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in {".dll", ".pyd"}:
            continue
        key = path.name.lower()
        if key in seen:
            continue
        seen.add(key)
        found.append((path, "onnxruntime/capi"))
    return found


def ocr_pyinstaller_args(package_dir: Path | None) -> list[str]:
    """生成把验证码模型和 onnxruntime 本机库打进冻结包的参数。

    Args:
        package_dir: ``onnxruntime`` 包目录。没有时仍收集包名，本机文件留空。

    Returns:
        list[str]: 追加到 PyInstaller 命令后面的参数。

    Examples:
        >>> args = ocr_pyinstaller_args(None)
        >>> "--collect-all" in args and "onnxruntime" in args and "ddddocr" in args
        True
    """
    args = [
        "--hidden-import", "onnxruntime",
        "--hidden-import", "onnxruntime.capi.onnxruntime_pybind11_state",
        "--collect-all", "onnxruntime",
        "--collect-binaries", "onnxruntime",
        "--collect-all", "ddddocr",
    ]
    for path, dest in onnxruntime_native_files(package_dir or Path()):
        args += ["--add-binary", f"{path}{os.pathsep}{dest}"]
    return args


def _normalize_toc_text(toc_text: str) -> str:
    """把清单里的反斜杠和连续斜杠收成单层正斜杠。

    PyInstaller 的 toc 会把 Windows 路径写成双反斜杠。只替换一次时，
    ``onnxruntime\\\\capi`` 会变成 ``onnxruntime//capi``，后面的目录判断会落空。

    Args:
        toc_text: PyInstaller toc 或打包参数原文。

    Returns:
        str: 小写、单斜杠的文本。

    Examples:
        >>> _normalize_toc_text(r"onnxruntime\\\\capi\\\\onnxruntime.dll")
        'onnxruntime/capi/onnxruntime.dll'
    """
    text = toc_text.replace("\\", "/").lower()
    while "//" in text:
        text = text.replace("//", "/")
    return text


def _ocr_file_in_capi(toc_text: str, filename: str) -> bool:
    """文件名附近要有 ``onnxruntime/capi``，只在别处提到名字不算。

    Args:
        toc_text: PyInstaller toc 或打包参数原文。
        filename: 要查找的 dll 或 pyd 文件名。

    Returns:
        bool: 文件名附近能看到 ``onnxruntime/capi`` 时为 True。

    Examples:
        >>> _ocr_file_in_capi("onnxruntime//capi//onnxruntime.dll", "onnxruntime.dll")
        True
    """
    text = _normalize_toc_text(toc_text)
    needle = filename.lower()
    window = 220
    start = 0
    while True:
        pos = text.find(needle, start)
        if pos < 0:
            return False
        chunk = text[max(0, pos - window): pos + len(needle) + window]
        if "onnxruntime/capi" in chunk:
            return True
        start = pos + len(needle)


def missing_ocr_binaries(toc_text: str) -> list[str]:
    """检查冻结清单里，验证码 pyd 和它的 dll 是否都靠着 ``onnxruntime/capi``。

    只出现文件名不算。现场的 ``DLL load failed`` 就是 pyd 旁边没有它要的本机库。

    Args:
        toc_text: PyInstaller toc 或打包参数文本。

    Returns:
        list[str]: 缺失或放错目录的文件。都在时为空列表。

    Examples:
        >>> sample = "onnxruntime/capi/onnxruntime.dll capi/onnxruntime_pybind11_state.pyd"
        >>> missing_ocr_binaries(sample)
        []
        >>> doubled = "onnxruntime//capi//onnxruntime.dll onnxruntime//capi//onnxruntime_pybind11_state.pyd"
        >>> missing_ocr_binaries(doubled)
        []
        >>> "onnxruntime.dll" in missing_ocr_binaries("onnxruntime_pybind11_state.pyd")
        True
    """
    missing: list[str] = []
    if not _ocr_file_in_capi(toc_text, "onnxruntime.dll"):
        missing.append("onnxruntime.dll")
    if not _ocr_file_in_capi(toc_text, "onnxruntime_pybind11_state"):
        missing.append("onnxruntime_pybind11_state.pyd")
    return missing


def probe_onnxruntime_dll(dll: Path) -> tuple[bool, str]:
    """在 Windows 上真正加载 ``onnxruntime.dll``。

    其他系统不加载，也不据此要求安装 VC 运行库。

    Args:
        dll: 打包前解释器里的 dll。

    Returns:
        tuple[bool, str]: 第一项为是否加载成功。失败时第二项是系统报错原文。

    Examples:
        >>> ok, detail = probe_onnxruntime_dll(Path("/no/such.dll"))
        >>> ok is False and bool(detail)
        True
    """
    if sys.platform != "win32":
        return False, "当前不是 Windows，没有加载 onnxruntime.dll"
    if not dll.is_file():
        return False, f"没有找到 {dll}"
    import ctypes

    try:
        ctypes.WinDLL(str(dll))
    except OSError as exc:
        return False, str(exc)
    return True, ""


def assert_ocr_frozen(workpath: Path) -> None:
    """冻结清单里没有验证码本机库时中止打包。

    Args:
        workpath: PyInstaller 的 workpath。

    Returns:
        None: 清单里同时有 dll 和 pyd 时返回。

    Raises:
        SystemExit: 找不到 toc，或清单里缺本机库。

    Examples:
        >>> callable(assert_ocr_frozen)
        True
    """
    tocs = sorted(workpath.rglob("*.toc"))
    if not tocs:
        raise SystemExit(f"未找到 PyInstaller 清单，无法确认验证码库已打进包：{workpath}")
    blob = "\n".join(path.read_text(encoding="utf-8", errors="replace") for path in tocs)
    missing = missing_ocr_binaries(blob)
    if missing:
        raise SystemExit(
            "冻结包里没有验证码所需的本机库："
            + "、".join(missing)
            + "。现场会报 DLL load failed while importing onnxruntime_pybind11_state。"
        )
    package = onnxruntime_package_dir()
    dll = package / "capi" / "onnxruntime.dll" if package else Path()
    if IS_WIN:
        loaded, detail = probe_onnxruntime_dll(dll)
        if not loaded:
            raise SystemExit(f"无法加载验证码依赖 {dll}：{detail}")
        print(f"[done] 已在 Windows 上加载 {dll}")
    else:
        print("[done] 验证码本机库已进入冻结清单；Windows 上才会实际加载 onnxruntime.dll")


def required_frozen_modules(*, include_windows_ui: bool = False) -> list[str]:
    """冻结包必须能导入的模块。

    源码里的每个 ``spider_gd`` 模块都要在。Windows 上再加证书窗口用的
    ``uiautomation``。不要只抽查登录当天报错过的那一个名字。

    Args:
        include_windows_ui: 为 True 时把 ``uiautomation`` 算进必需名单。

    Returns:
        list[str]: 模块名。

    Examples:
        >>> "spider_gd.core.report_dates" in required_frozen_modules()
        True
        >>> "uiautomation" in required_frozen_modules(include_windows_ui=True)
        True
    """
    names = source_package_modules()
    if include_windows_ui:
        names.append("uiautomation")
    return names


def frozen_module_names(exe: Path) -> set[str]:
    """读出冻结 exe 里可以 import 的模块名。

    Args:
        exe: ``spider-gd-service.exe``。

    Returns:
        set[str]: PYZ 里的模块名。

    Raises:
        SystemExit: exe 里没有 PYZ。

    Examples:
        >>> callable(frozen_module_names)
        True
    """
    from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader

    reader = CArchiveReader(str(exe))
    pyz_name = next(
        (name for name in reader.toc if name.upper().endswith("PYZ.PYZ")),
        "",
    )
    if not pyz_name:
        raise SystemExit(f"冻结包里没有 PYZ：{exe}")
    blob_path = exe.with_suffix(".pyz-check")
    blob_path.write_bytes(reader.extract(pyz_name))
    try:
        return set(ZlibArchiveReader(str(blob_path)).toc)
    finally:
        blob_path.unlink(missing_ok=True)


def missing_frozen_modules(
    names: set[str],
    *,
    required: list[str] | None = None,
) -> list[str]:
    """返回冻结包里还不能导入的模块。

    Args:
        names: ``frozen_module_names`` 的结果，或测试里的替身集合。
        required: 必需模块。空则用当前平台的 :func:`required_frozen_modules`。

    Returns:
        list[str]: 缺失的模块名。都在时为空列表。

    Examples:
        >>> missing_frozen_modules({"a.b"}, required=["a.b"])
        []
        >>> missing_frozen_modules(set(), required=["a.b"])
        ['a.b']
    """
    needed = required if required is not None else required_frozen_modules(
        include_windows_ui=sys.platform == "win32",
    )
    missing: list[str] = []
    for name in needed:
        present = name in names or any(item.startswith(name + ".") for item in names)
        if not present:
            missing.append(name)
    return missing


def assert_frozen_modules(exe: Path) -> None:
    """冻结包缺登录或证书窗口模块时中止打包。

    Args:
        exe: 刚打出来的后端 exe。

    Returns:
        None: 必需模块都在时返回。

    Raises:
        SystemExit: 有模块不能导入。

    Examples:
        >>> callable(assert_frozen_modules)
        True
    """
    missing = missing_frozen_modules(
        frozen_module_names(exe),
        required=required_frozen_modules(include_windows_ui=IS_WIN),
    )
    if missing:
        raise SystemExit("冻结包不能导入：" + "、".join(missing))
    print("[done] 冻结包已包含仓库里的 spider_gd 模块")


def existing_data_files(pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """保留打包时源路径仍然存在的数据目录。

    工作流放在用户目录，仓库里可以没有 ``workflows/``。
    把不存在的路径交给 PyInstaller 会让整次打包失败。

    Args:
        pairs: ``(源绝对路径, 包内相对路径)``。

    Returns:
        list[tuple[str, str]]: 源路径存在的那些项，顺序不变。

    Examples:
        >>> existing_data_files([("/no/such/workflows", "workflows")])
        []
    """
    return [(src, dst) for src, dst in pairs if Path(src).exists()]


def clean_build() -> None:
    """清理上次构建产物，避免 PyInstaller 缓存问题。"""
    if BUILD.exists():
        shutil.rmtree(BUILD, ignore_errors=True)
    spec = ROOT / "build" / f"{APP_NAME}.spec"
    if spec.exists():
        spec.unlink()


def run_pyinstaller() -> None:
    print("=== 编译 Python 后端 ===")
    assert_repo_package_wins()
    absent = missing_repo_modules(runtime_tool_imports())
    if absent:
        raise SystemExit("工具脚本导入了仓库里没有的模块：" + "、".join(absent))

    OUTPUT.mkdir(parents=True, exist_ok=True)
    clean_build()
    build_id = current_build_id()

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--console",
        "--name", APP_NAME,
        "--distpath", str(OUTPUT),
        "--workpath", str(BUILD),
        "--specpath", str(ROOT / "build"),
        "--paths", str(SRC),
        "--paths", str(ROOT),   # 让 PyInstaller 找到 tools/
    ]
    cmd += spider_gd_pyinstaller_args()

    present = existing_data_files(DATA_FILES)
    missing = [src for src, _dst in DATA_FILES if not Path(src).exists()]
    for src_abs in missing:
        print(f"[warn] 跳过不存在的数据目录: {src_abs}")
    for src_abs, dst in present:
        cmd += ["--add-data", f"{src_abs}{os.pathsep}{dst}"]

    for mod in HIDDEN_IMPORTS:
        cmd += ["--hidden-import", mod]

    if IS_WIN:
        cmd += windows_wmi_pyinstaller_args(is_win=True, dlls=pywin32_dlls(_pywin32_search_roots()))
        cmd += certificate_ui_pyinstaller_args(is_win=True)

    # curl_cffi 可选：TLS 指纹伪装（JA3/JA4 绕过）
    try:
        import curl_cffi  # noqa: F401
        cmd += ["--hidden-import", "curl_cffi"]
        cmd += ["--collect-all", "curl_cffi"]
    except ImportError:
        print("[warn] curl_cffi 未安装，打包将不含 TLS 伪装（httpx 回退）")

    # ddddocr 只带上模型不够。pyd 还要在 onnxruntime/capi 里找到 onnxruntime.dll。
    cmd += ocr_pyinstaller_args(onnxruntime_package_dir())

    # 精简包体：排除不会被调用的重量级模块
    cmd += [
        "--exclude-module", "jinja2",
        "--exclude-module", "tensorflow",
        "--exclude-module", "torch",
        "--exclude-module", "matplotlib",
    ]

    cmd.append(str(ENTRY))

    print(f"[cmd] {' '.join(cmd)}")

    # conda 环境的 C 运行库（libexpat / libssl / libcrypto / sqlite3 / ffi 等）放在
    # <env>\Library\bin，不在默认 PATH 里时 PyInstaller 解析不到依赖，打出的 exe 会在
    # 启动阶段报「DLL load failed while importing pyexpat」。
    env = os.environ.copy()
    path_parts = [p for p in env.get("PATH", "").split(os.pathsep) if p]
    for prefix in {sys.prefix, sys.base_prefix}:
        lib_bin = Path(prefix) / "Library" / "bin"
        if lib_bin.is_dir() and str(lib_bin) not in path_parts:
            path_parts.insert(0, str(lib_bin))
    env["PATH"] = os.pathsep.join(path_parts)

    original_stamp = install_build_stamp(build_id)
    try:
        subprocess.run(cmd, check=True, env=env)
    finally:
        restore_build_stamp(original_stamp)
    (OUTPUT / "build_stamp.json").write_text(
        json.dumps({"build_id": build_id}, ensure_ascii=False),
        encoding="utf-8",
    )
    exe = OUTPUT / f"{APP_NAME}{EXE_SUFFIX}"
    if not exe.exists():
        alt = OUTPUT / APP_NAME
        exe = alt if alt.exists() else exe
    if exe.exists():
        assert_frozen_modules(exe)
        assert_frozen_ocr(exe)
    if IS_WIN:
        assert_ocr_frozen(BUILD)

    # 复制 .env 模板（UTF-8 编码）
    env_template = OUTPUT / ".env"
    if not env_template.exists():
        env_template.write_text(
            "# spider-gd backend env\n"
            "# CRAWLER__USERNAME=your_username\n"
            "# CRAWLER__PASSWORD=your_password\n"
            "# CRAWLER__CERT_PASSWORD=\n"
            "# ALERT__DINGTALK_WEBHOOK=\n"
            "# ALERT__DINGTALK_SECRET=\n",
            encoding="utf-8"
        )
        print(f"[done] 已生成 .env 模板 -> {env_template}")

    exe = OUTPUT / f"{APP_NAME}{EXE_SUFFIX}"
    if exe.exists():
        size_mb = exe.stat().st_size / (1024 * 1024)
        print(f"[done] {exe} ({size_mb:.1f} MB)")
    else:
        # PyInstaller 可能不添加后缀，检查无后缀版本
        alt = OUTPUT / APP_NAME
        if alt.exists():
            size_mb = alt.stat().st_size / (1024 * 1024)
            print(f"[done] {alt} ({size_mb:.1f} MB)")
        else:
            print("[warn] 未找到输出文件，请检查 PyInstaller 日志")


def chromium_dir_name(manifest: dict) -> str:
    """从 Playwright 的 browsers.json 取出当前 Chromium 目录名。

    Args:
        manifest: ``browsers.json`` 解析后的对象。

    Returns:
        str: 缓存目录名，例如 ``chromium-1255``。

    Raises:
        ValueError: 清单里没有带版本号的 chromium。

    Examples:
        >>> chromium_dir_name({"browsers": [{"name": "chromium", "revision": "1255"}]})
        'chromium-1255'
        >>> chromium_dir_name({"browsers": [{"name": "firefox", "revision": "1"}]})
        Traceback (most recent call last):
        ValueError: browsers.json 里没有当前 Chromium 版本
    """
    for item in manifest.get("browsers", []):
        if item.get("name") == "chromium" and item.get("revision"):
            return f"chromium-{item['revision']}"
    raise ValueError("browsers.json 里没有当前 Chromium 版本")


def playwright_manifest_path(package_dir: Path) -> Path:
    """定位当前 Playwright 安装里的 browsers.json。

    Args:
        package_dir: ``playwright`` 包目录。

    Returns:
        Path: 清单文件。

    Raises:
        SystemExit: 两个已知位置都没有清单。

    Examples:
        >>> try:
        ...     playwright_manifest_path(Path("/no/such/playwright"))
        ... except SystemExit as exc:
        ...     "browsers.json" in str(exc)
        True
    """
    candidates = [
        package_dir / "driver" / "package" / "browsers.json",
        package_dir / "driver" / "browsers.json",
    ]
    for path in candidates:
        if path.is_file():
            return path
    raise SystemExit(f"未找到 Playwright 浏览器清单：{candidates[0]}")


def chromium_directory(cache_dir: Path, folder_name: str) -> Path:
    """确认缓存里有且能启动的那一版 Chromium。

    Args:
        cache_dir: Playwright 浏览器缓存根目录。
        folder_name: 当前版本的目录名，例如 ``chromium-1255``。

    Returns:
        Path: 该版本的目录。

    Raises:
        SystemExit: 目录不存在，或其中没有 chrome 可执行文件。

    Examples:
        >>> import tempfile
        >>> root = Path(tempfile.mkdtemp())
        >>> folder = root / "chromium-1255" / "chrome-win64"
        >>> folder.mkdir(parents=True)
        >>> (folder / "chrome.exe").write_bytes(b"")
        0
        >>> chromium_directory(root, "chromium-1255").name
        'chromium-1255'
    """
    directory = cache_dir / folder_name
    if not directory.is_dir():
        raise SystemExit(f"未找到当前 Playwright 所需的浏览器：{directory}")
    executables = [
        path for path in directory.rglob("*")
        if path.is_file() and path.name.lower() in {"chrome.exe", "chrome", "chromium"}
    ]
    if not executables:
        raise SystemExit(f"浏览器目录里没有可执行文件：{directory}")
    return directory


def copy_browsers() -> None:
    """只复制当前 Playwright 所需的那一版 Chromium。

    缓存里的旧版本不会进入安装包。输出目录已有浏览器时先清空再复制。

    Returns:
        None: 复制完成时返回。

    Raises:
        SystemExit: 没有缓存、没有清单，或没有当前版本的浏览器。

    Examples:
        >>> callable(copy_browsers)
        True
    """
    cache_dir = None
    for candidate in [
        Path.home() / "AppData" / "Local" / "ms-playwright",
        Path.home() / ".cache" / "ms-playwright",
        Path.home() / "Library" / "Caches" / "ms-playwright",
    ]:
        if candidate.exists():
            cache_dir = candidate
            break
    if cache_dir is None:
        raise SystemExit("未找到 Playwright 浏览器缓存，请先运行: playwright install chromium")

    import playwright

    package_dir = Path(playwright.__file__).resolve().parent
    manifest = json.loads(playwright_manifest_path(package_dir).read_text(encoding="utf-8"))
    folder_name = chromium_dir_name(manifest)
    source = chromium_directory(cache_dir, folder_name)
    target = OUTPUT / "playwright_browsers"
    if target.exists():
        shutil.rmtree(target)
    destination = target / folder_name
    print(f"复制 Playwright 浏览器: {source} -> {destination}")
    shutil.copytree(source, destination)
    copied = sorted(path.name for path in target.glob("chromium-*") if path.is_dir())
    if copied != [folder_name]:
        raise SystemExit(f"浏览器目录应只有 {folder_name}，实际是 {copied}")
    total_size = sum(path.stat().st_size for path in destination.rglob("*") if path.is_file())
    print(f"[done] Playwright 浏览器已复制 {folder_name} ({total_size / (1024 * 1024):.0f} MB)")


def main() -> None:
    with_browsers = "--with-browsers" in sys.argv
    allow_arm64 = "--allow-arm64" in sys.argv

    print("=== 检查依赖 ===")
    subprocess.run([sys.executable, "-c", "import PyInstaller"], check=True)
    subprocess.run([sys.executable, "-c", "import uvicorn; import fastapi"], check=False)
    if IS_WIN:
        ensure_windows_wmi(allow_arm64=allow_arm64)
    print("  OK")

    run_pyinstaller()

    if with_browsers:
        copy_browsers()

    print("\n=== 完成 ===")
    print(f"  输出: {OUTPUT}")
    if not IS_WIN:
        print(f"  ⚠ 当前平台非 Windows，编译产物只能在 {sys.platform} 运行")
        print("  发布 Windows 版请在 Windows 机器上运行此脚本")


if __name__ == "__main__":
    main()
