"""PyInstaller 入口：将采集后端编译为独立 exe 时使用。

运行时行为：
1. 以 exe 所在目录为工作目录（相对路径如 ./data ./logs 均相对于 exe 目录）
2. 若 configs/ 不存在则从 PyInstaller bundle 中解包
3. 若设置了 PLAYWRIGHT_BROWSERS_PATH 则使用该路径
"""

import io
import json
import os
import sys
import traceback
from pathlib import Path


def _fix_console() -> None:
    """Windows 控制台编码修复：避免 GBK/UTF-8 乱码导致启动失败。"""
    if sys.platform != "win32":
        return
    try:
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace"
        )
        sys.stderr = io.TextIOWrapper(
            sys.stderr.buffer, encoding="utf-8", errors="replace"
        )
    except Exception:
        pass


def _exe_dir() -> Path:
    """exe 或脚本所在目录（PyInstaller frozen 时用 sys.executable）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _unpack_data(exe_dir: Path, name: str) -> None:
    """若目录不存在，从 PyInstaller bundle 解包。"""
    target = exe_dir / name
    if target.exists():
        return
    bundle = Path(getattr(sys, "_MEIPASS", ""))
    source = bundle / name
    if source.exists():
        _copy_tree(source, target)
        print(f"[bootstrap] {name} copied to {target}")


def _unpack_configs(exe_dir: Path) -> None:
    """解包 configs（endpoints.yaml 等系统配置，不含预置指标）。"""
    _unpack_data(exe_dir, "configs")


def _copy_tree(src: Path, dst: Path) -> None:
    """递归复制目录。"""
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        target = dst / item.name
        if item.is_dir():
            _copy_tree(item, target)
        else:
            target.write_bytes(item.read_bytes())


def _fix_env_encoding(exe_dir: Path) -> None:
    """正式版保留 GBK → UTF-8 转换；测试版的 .env 输入保持只读。"""
    from spider_gd.release_channel import runtime_release

    if runtime_release()["channel"] == "beta":
        return
    env_path = exe_dir / ".env"
    if not env_path.exists():
        return
    try:
        env_path.read_text("utf-8")
        return  # 已经是 UTF-8
    except UnicodeDecodeError:
        pass
    # 尝试 GBK 读取并转写 UTF-8
    try:
        content = env_path.read_text("gbk")
        env_path.write_text(content, encoding="utf-8")
        print("[bootstrap] .env converted GBK -> UTF-8")
    except Exception:
        pass


def _dispatch_picker(argv: list[str]) -> int | None:
    """场站窗口请求只弹窗，不再启动第二套后端。

    Args:
        argv: 当前进程参数，第一项通常是 exe 路径。

    Returns:
        int | None: 窗口进程退出码。普通启动时为 None。

    Raises:
        SystemExit: 窗口参数触发 argparse 帮助或错误时。

    Examples:
        >>> _dispatch_picker(["spider-gd-service.exe"]) is None
        True
    """
    from spider_gd.core.station_picker import extract_picker_argv

    forwarded = extract_picker_argv(argv)
    if forwarded is None:
        return None
    from spider_gd.ui.station_picker_dialog import main as picker_main

    return int(picker_main(forwarded))


def _dispatch_build_self_test(argv: list[str]) -> int | None:
    """只读构建身份和导入依赖，不初始化配置、HOME 或服务。"""
    if "--self-test-build" not in argv:
        return None
    try:
        from spider_gd.build_stamp import BUILD_ID
        from spider_gd.release_channel import runtime_release

        release = runtime_release()
        if sys.platform == "win32":
            import pythoncom

            pythoncom.CoInitialize()
            try:
                import pywintypes  # noqa: F401
                import uiautomation  # noqa: F401
                import win32com.client  # noqa: F401
                import wmi  # noqa: F401
            finally:
                pythoncom.CoUninitialize()
        print(json.dumps({
            "build_id": BUILD_ID,
            "channel": release["channel"],
            "version": release["version"],
        }))
    except Exception:
        traceback.print_exc()
        return 1
    return 0


def _dispatch_ocr_self_test(argv: list[str]) -> int | None:
    """冻结产物用 ``--self-test-ocr`` 自己加载验证码依赖。"""
    if "--self-test-ocr" not in argv:
        return None
    try:
        from spider_gd.core.crawler.ocr_probe import ocr_self_test

        print(ocr_self_test())
    except Exception:
        traceback.print_exc()
        return 1
    return 0


def main() -> int:
    """启动采集后端；参数属于场站窗口时只弹窗。

    打包后的服务程序被再次拉起时，不能再走 ``run_app``，否则会重复占用 8800。

    Returns:
        int: 进程退出码。窗口取消为 2，失败为 1。

    Examples:
        >>> callable(main)
        True
    """
    _fix_console()
    build_code = _dispatch_build_self_test(sys.argv)
    if build_code is not None:
        return build_code
    ocr_code = _dispatch_ocr_self_test(sys.argv)
    if ocr_code is not None:
        return ocr_code
    picker_code = _dispatch_picker(sys.argv)
    if picker_code is not None:
        return picker_code

    try:
        exe_dir = _exe_dir()
        os.chdir(str(exe_dir))
        print(f"[bootstrap] CWD: {exe_dir}")

        _unpack_configs(exe_dir)

        # 确保 tools/ 在 Python path（PyInstaller 打包后路径可能偏移）
        tools_dir = exe_dir / "tools"
        if not tools_dir.exists():
            # PyInstaller bundle 里
            bundle = Path(getattr(sys, "_MEIPASS", ""))
            tools_dir = bundle / "tools"
        if tools_dir.exists() and str(tools_dir) not in sys.path:
            sys.path.insert(0, str(tools_dir))

        # 修复 .env 编码
        _fix_env_encoding(exe_dir)

        # Playwright
        if not os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
            browsers_dir = exe_dir / "playwright_browsers"
            if browsers_dir.exists():
                os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers_dir)
                print("[bootstrap] PLAYWRIGHT_BROWSERS_PATH set")

        from spider_gd.config.settings import load_config
        from spider_gd.app import run_app

        # 默认配置
        config_path = exe_dir / "configs" / "mode_a_wired.yaml"
        if not config_path.exists():
            config_path = exe_dir / "configs" / "mode_d_offline.yaml"

        print(f"[bootstrap] loading config: {config_path}")
        settings = load_config(str(config_path))
        return run_app(settings)

    except Exception:
        # PyInstaller 打包后控制台一闪而过，打印完整 traceback
        traceback.print_exc()
        print("\nPress Enter to exit...")
        input()
        return 1


if __name__ == "__main__":
    sys.exit(main())
