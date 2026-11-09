"""构建脚本：用 PyInstaller 将采集后端编译为独立 exe，输出到 frontend/resources/backend/。

用法：
    python build_backend.py              # 仅编译
    python build_backend.py --with-browsers  # 编译 + 打包 Playwright Chromium

注意：
    PyInstaller 不能跨平台编译。在 macOS 上编译的输出只能在 macOS 运行。
    要发布 Windows 版，需要在 Windows 机器上运行此脚本。
    Windows 下请先：pip install pyinstaller && pip install -e .[online]

前置：
    pip install pyinstaller
    pip install -e .[online]    # 安装项目依赖（含 web 模块）
"""

import os
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

    for src_abs, dst in DATA_FILES:
        cmd += ["--add-data", f"{src_abs}{os.pathsep}{dst}"]

    for mod in HIDDEN_IMPORTS:
        cmd += ["--hidden-import", mod]

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

    print("=== 检查依赖 ===")
    subprocess.run([sys.executable, "-c", "import PyInstaller"], check=True)
    subprocess.run([sys.executable, "-c", "import uvicorn; import fastapi"], check=False)
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
