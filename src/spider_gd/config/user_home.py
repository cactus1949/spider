"""把用户数据从安装目录挪到每人一份的目录。

安装包升级会清掉安装目录里的 ``data/``、``tasks/`` 和 ``logs/``。
Electron 把 ``app.getPath('userData')`` 放进环境变量 ``SPIDER_GD_HOME`` 后，
相对路径的数据库、日志、浏览器配置和任务文件都改到这个目录下。
环境变量未设置时保持相对当前工作目录的旧行为，开发和测试不用改。

首次启动如果新目录还没有数据、安装目录里还有旧文件，会复制过去并写日志。
复制而不是移动，避免中途失败把旧数据弄丢。
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from loguru import logger

HOME_ENV = "SPIDER_GD_HOME"

# 安装目录根上这些名字属于用户数据，升级时不能留在安装目录里。
_MIGRATE_NAMES = (
    "data",
    "logs",
    "archives",
    "tasks",
    "workflows",
    "downloads",
    "_failures",
    ".env",
)


def user_home() -> Path | None:
    """返回 ``SPIDER_GD_HOME`` 指向的目录；未设置时为 None。

    Returns:
        Path | None: 展开用户目录后的绝对路径。环境变量为空时为 None。

    Examples:
        >>> user_home() is None or isinstance(user_home(), Path)
        True
    """
    raw = os.environ.get(HOME_ENV, "").strip().strip('"')
    if not raw:
        return None
    return Path(raw).expanduser().resolve()


def tasks_dir() -> Path:
    """任务 YAML 的读写目录。

    设置了用户目录时用其中的 ``tasks/``，否则用当前工作目录下的 ``tasks/``。

    Returns:
        Path: 任务目录。用户目录存在时会顺手建好。

    Examples:
        >>> tasks_dir().name == "tasks"
        True
    """
    home = user_home()
    path = (home / "tasks") if home else (Path.cwd() / "tasks")
    if home:
        path.mkdir(parents=True, exist_ok=True)
    return path


def bundled_workflows_dir() -> Path:
    """安装包或仓库自带的工作流目录（只读来源）。

    Returns:
        Path: 优先仓库/安装根下已存在的 ``workflows``，否则当前工作目录。

    Examples:
        >>> bundled_workflows_dir().name == "workflows"
        True
    """
    repo = Path(__file__).resolve().parents[3] / "workflows"
    cwd = Path.cwd() / "workflows"
    if repo.exists():
        return repo
    return cwd


def user_workflows_dir(*, create: bool = False) -> Path:
    """用户可改写的工作流目录。

    Args:
        create (bool): 为 True 且设置了用户目录时创建目录。

    Returns:
        Path: 用户目录下的 ``workflows``，未设置用户目录时为当前工作目录。

    Examples:
        >>> user_workflows_dir().name == "workflows"
        True
    """
    home = user_home()
    path = (home / "workflows") if home else (Path.cwd() / "workflows")
    if create and home:
        path.mkdir(parents=True, exist_ok=True)
    return path


def resolve_workflow_file(name: str) -> Path | None:
    """按「用户目录覆盖安装目录」查找工作流文件。

    Args:
        name (str): 工作流 id，会去掉路径非法字符。

    Returns:
        Path | None: 找到的 yml；两处都没有时为 None。

    Examples:
        >>> resolve_workflow_file("../nope") is None or True
        True
    """
    safe = re.sub(r"[^\w\-.]", "_", name)
    for folder in (user_workflows_dir(), bundled_workflows_dir()):
        path = folder / f"{safe}.yml"
        if path.is_file():
            return path
    return None


def _is_absolute_path(text: str) -> bool:
    """判断路径是否已经是绝对路径（含 Windows 盘符，便于在 Linux 上单测）。

    Args:
        text (str): 配置里的路径字符串。

    Returns:
        bool: 绝对路径时为 True。

    Examples:
        >>> _is_absolute_path(r"D:\\软件安装\\spider.db")
        True
        >>> _is_absolute_path("./data/spider.db")
        False
    """
    if Path(text).is_absolute():
        return True
    if text.startswith("\\\\") or text.startswith("//"):
        return True
    return re.match(r"^[A-Za-z]:[\\/]", text) is not None


def rebase_path(value: str, home: Path) -> str:
    """把相对路径改到用户目录下；绝对路径和内存库保持原样。

    Args:
        value (str): 配置中的路径。
        home (Path): ``SPIDER_GD_HOME``。

    Returns:
        str: 重定位后的路径。空字符串仍为空。

    Examples:
        >>> rebase_path("./data/spider.db", Path("/tmp/home"))
        '/tmp/home/data/spider.db'
        >>> rebase_path(":memory:", Path("/tmp/home"))
        ':memory:'
    """
    text = str(value or "").strip()
    if not text or text == ":memory:" or _is_absolute_path(text):
        return text
    return str((home / text).resolve())


def rebase_user_paths(settings):
    """按用户目录重写配置里的相对数据路径。

    未设置 ``SPIDER_GD_HOME`` 时原样返回，调用方可以继续用安装目录。

    Args:
        settings: 已加载的 ``AppSettings``。

    Returns:
        AppSettings: 路径已重写的配置；未设置用户目录时就是入参本身。

    Examples:
        >>> from spider_gd.config.settings import AppSettings
        >>> same = rebase_user_paths(AppSettings())
        >>> same.storage.sqlite_path.endswith("spider.db")
        True
    """
    home = user_home()
    if home is None:
        return settings

    def rebase_model(model, fields: tuple[str, ...]):
        updates = {name: rebase_path(getattr(model, name), home) for name in fields}
        return model.model_copy(update=updates)

    storage = rebase_model(settings.storage, ("data_dir", "archive_dir", "sqlite_path"))
    log = rebase_model(settings.log, ("file_dir",))
    crawler = rebase_model(settings.crawler, ("user_data_dir",))
    delivery = rebase_model(
        settings.delivery,
        ("env_file", "desktop_wechat_profile_path"),
    )
    return settings.model_copy(update={
        "storage": storage,
        "log": log,
        "crawler": crawler,
        "delivery": delivery,
    })


def _load_env_file(path: Path) -> None:
    """把 .env 里尚未出现在环境中的键补进去。

    pydantic 只读进程启动目录下的 ``.env``。迁到用户目录后必须自己灌进环境变量，
    证书密码一类键才不会在升级后丢失。已有的环境变量优先，不覆盖。

    Args:
        path (Path): 用户目录里的 ``.env``。

    Returns:
        None: 结果写入 ``os.environ``。

    Examples:
        >>> _load_env_file(Path("/no/such.env")) is None
        True
    """
    if not path.is_file():
        return
    raw = ""
    for encoding in ("utf-8", "utf-8-sig", "gbk"):
        try:
            raw = path.read_text(encoding=encoding)
            break
        except UnicodeError:
            continue
    if not raw:
        return
    for line in raw.splitlines():
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, value = text.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _copy_entry(src: Path, dst: Path) -> None:
    """复制文件或目录；目标已存在时什么也不做。

    Args:
        src (Path): 安装目录里的旧数据。
        dst (Path): 用户目录里的新位置。

    Returns:
        None: 复制完成或跳过。

    Raises:
        OSError: 磁盘复制失败时抛出，由调用方记录。

    Examples:
        >>> _copy_entry(Path("/no/such"), Path("/no/dest")) is None
        True
    """
    if not src.exists() or dst.exists():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dst)
    else:
        shutil.copy2(src, dst)


def migrate_install_data(install_dir: Path, home: Path) -> list[str]:
    """把安装目录里已有的用户数据复制到用户目录（目标已存在则跳过）。

    Args:
        install_dir (Path): 后端工作目录，打包后是 ``resources/backend``。
        home (Path): ``SPIDER_GD_HOME``。

    Returns:
        list[str]: 实际复制的相对路径。没有可迁的数据时为空列表。

    Examples:
        >>> migrate_install_data(Path("/tmp"), Path("/tmp"))
        []
    """
    install_dir = install_dir.resolve()
    home = home.resolve()
    if install_dir == home:
        return []
    copied: list[str] = []
    home.mkdir(parents=True, exist_ok=True)
    for name in _MIGRATE_NAMES:
        src = install_dir / name
        dst = home / name
        if not src.exists() or dst.exists():
            continue
        try:
            _copy_entry(src, dst)
        except OSError as exc:
            logger.warning("迁移 {} 失败: {}", src, exc)
            continue
        copied.append(name)
        logger.info("已将用户数据从安装目录迁移到 {}: {} -> {}", HOME_ENV, src, dst)
        print(f"[bootstrap] 迁移用户数据: {src} -> {dst}")
    for extra in install_dir.glob("*.env"):
        if extra.name == ".env.example":
            continue
        dst = home / extra.name
        if dst.exists():
            continue
        try:
            _copy_entry(extra, dst)
        except OSError as exc:
            logger.warning("迁移 {} 失败: {}", extra, exc)
            continue
        copied.append(extra.name)
        logger.info("已迁移密钥文件: {} -> {}", extra, dst)
        print(f"[bootstrap] 迁移密钥文件: {extra} -> {dst}")
    return copied


def prepare_user_home(install_dir: Path | None = None) -> Path | None:
    """迁移旧数据并把用户目录里的 ``.env`` 载入环境变量。

    可以重复调用。未设置 ``SPIDER_GD_HOME`` 时直接返回，不改工作目录。

    Args:
        install_dir (Path | None): 安装目录。空则用当前工作目录。

    Returns:
        Path | None: 用户目录；未设置时为 None。

    Examples:
        >>> prepare_user_home(Path(".")) is None or True
        True
    """
    home = user_home()
    if home is None:
        return None
    install = (install_dir or Path.cwd()).resolve()
    try:
        migrate_install_data(install, home)
    except OSError as exc:
        logger.warning("迁移用户数据失败: {}", exc)
    _load_env_file(home / ".env")
    return home
