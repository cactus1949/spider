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

import contextlib
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

    OUTPUT.mkdir(parents=True, exist_ok=True)
    clean_build()

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--console",
        "--name", APP_NAME,
        "--distpath", str(OUTPUT),
        "--workpath", str(BUILD),
        "--specpath", str(ROOT / "build"),
        "--add-data", f"{SRC}{os.pathsep}spider_gd",
        "--paths", str(SRC),
        "--paths", str(ROOT),   # 让 PyInstaller 找到 tools/
    ]

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

    # curl_cffi 可选：TLS 指纹伪装（JA3/JA4 绕过）
    try:
        import curl_cffi  # noqa: F401
        cmd += ["--hidden-import", "curl_cffi"]
        cmd += ["--collect-all", "curl_cffi"]
    except ImportError:
        print("[warn] curl_cffi 未安装，打包将不含 TLS 伪装（httpx 回退）")

    # 收集 ddddocr 的 onnx 模型文件
    cmd += ["--collect-all", "ddddocr"]

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

    subprocess.run(cmd, check=True, env=env)

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


def copy_browsers() -> None:
    """复制 Playwright Chromium 浏览器到输出目录。"""
    cache_dir = None
    for candidate in [
        Path.home() / "AppData" / "Local" / "ms-playwright",        # Windows
        Path.home() / ".cache" / "ms-playwright",                   # Linux
        Path.home() / "Library" / "Caches" / "ms-playwright",       # macOS
    ]:
        if candidate.exists():
            cache_dir = candidate
            break

    if not cache_dir:
        print("[warn] 未找到 Playwright 浏览器缓存，请先运行: playwright install chromium")
        return

    target = OUTPUT / "playwright_browsers"
    if target.exists():
        print(f"[skip] {target} 已存在")
        return

    print(f"复制 Playwright 浏览器: {cache_dir} -> {target}")
    shutil.copytree(cache_dir, target, dirs_exist_ok=True)
    total_size = sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
    print(f"[done] Playwright 浏览器已复制 ({total_size / (1024*1024):.0f} MB)")


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

    print(f"\n=== 完成 ===")
    print(f"  输出: {OUTPUT}")
    if not IS_WIN:
        print(f"  ⚠ 当前平台非 Windows，编译产物只能在 {sys.platform} 运行")
        print(f"  发布 Windows 版请在 Windows 机器上运行此脚本")


if __name__ == "__main__":
    main()
