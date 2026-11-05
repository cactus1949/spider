"""配置加载：pydantic-settings + 多 yaml（``--config`` + ``inherits`` 递归合并）。

- ``.env`` 提供敏感凭据（嵌套用双下划线分隔，如 ``CRAWLER__PASSWORD``）。
- yaml 文件提供结构化配置，``inherits: common.yaml`` 实现公共配置继承。
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from spider_gd.config.deploy_mode import DeployMode, ModuleToggle
from spider_gd.interfaces.crawler import EndpointConfig


class PlatformSettings(BaseSettings):
    domain: str = "pm.gd.csg.cn"
    login_url: str = "https://pm.gd.csg.cn/"


class StorageSettings(BaseSettings):
    data_dir: str = "./data"
    archive_dir: str = "./archives"
    sqlite_path: str = "./data/spider.db"
    enable_wal: bool = True


class LogSettings(BaseSettings):
    persist_to_file: bool = True
    file_dir: str = "./logs"
    console_level: str = "INFO"
    file_level: str = "DEBUG"
    retention_days: int = 30


class CrawlerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CRAWLER__")

    username: str = ""
    password: str = ""
    cert_password: str = ""  # U-key 证书密码
    headless: bool = True
    login_timeout_sec: int = 60
    user_data_dir: str = "./data/.browser"


class UKeySettings(BaseSettings):
    """U-Key 监控与有效期。"""

    vid_pid: str = "auto"  # 形如 "1234:5678"，auto 表示不限定
    poll_interval: int = 3
    valid_until: str = ""  # U-Key/CA 证书到期日 YYYY-MM-DD，空表示不管理有效期
    warn_days: int = 30  # 到期前多少天开始续期告警


class AlertSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ALERT__")

    channels: list[dict[str, Any]] = Field(default_factory=list)
    silence_window: str = ""  # 例 "23:00-07:00"
    dingtalk_webhook: str = ""
    dingtalk_secret: str = ""


class DeliverySettings(BaseSettings):
    """任务产物发送配置。"""

    enabled: bool = False
    env_file: str = ""
    backend: str = "wecom_webhook"
    wecom_webhook: str = ""
    serverchan_send_key: str = ""
    serverchan_channel: str = ""
    serverchan_openid: str = ""
    pushplus_token: str = ""
    pushplus_channel: str = ""
    desktop_wechat_target: str = ""
    desktop_wechat_backend: str = "powershell"  # auto / powershell / wxauto
    desktop_wechat_select_mode: str = "current"  # current / bound / exact / legacy-search
    desktop_wechat_profile_path: str = "./data/wechat_targets.json"
    desktop_wechat_app_name: str = ""
    desktop_wechat_pause_sec: float = 0.7
    desktop_wechat_hash_threshold: int = 12
    desktop_wechat_allow_unstable_search: bool = False
    send_message: bool = True
    send_file: bool = True
    file_delivery: str = "auto"  # auto / wecom_file / remote_link
    wecom_file_max_mb: int = 20
    remote_upload_url: str = ""
    remote_upload_token: str = ""
    remote_upload_relative_path: str = "/upload/publicData"
    remote_upload_verify_url: bool = True
    retry_attempts: int = 2
    retry_delay_sec: float = 0.5
    title: str = "spider-gd 采集完成"


class RpaSettings(BaseSettings):
    """RPA 兜底采集：indicator → (页面URL, 导出按钮选择器) 映射。"""

    endpoint_pages: dict[str, tuple[str, str]] = Field(default_factory=dict)


class AgentSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENT__")

    enabled: bool = False
    max_permission: str = "readonly"  # readonly / config / system


class WebSettings(BaseSettings):
    """REST 控制接口 / Web UI 监听配置（走公网网卡）。"""

    host: str = "127.0.0.1"
    port: int = 8800
    enable_mcp: bool = True  # 挂载 MCP server（streamable-http），供 OpenClaw 调用


class ScheduleSettings(BaseSettings):
    """调度策略：每日两次定时 + 高频补漏 + 保留期清理。"""

    crawl_cron: list[str] = Field(default_factory=list)  # 空=不自动注册，由任务配置页面管理
    backfill_cron: str = ""  # 不自动补漏
    retention_cron: str = ""  # 不自动清理
    retention_days: int = 90
    backfill_lookback_days: int = 7  # 缺口检测回看窗口
    backfill_max_items: int = 5  # 单次补漏上限（防雪崩）
    backfill_min_interval_sec: float = 3600.0  # 补漏频率上限（防雪崩）
    task_name: str = "日常采集"


class AppSettings(BaseSettings):
    """根配置：由 yaml 加载，``.env`` 覆盖敏感字段。"""

    model_config = SettingsConfigDict(
        env_nested_delimiter="__",
        env_file=".env",
        extra="ignore",
    )

    deploy_mode: DeployMode = DeployMode.OFFLINE
    modules: ModuleToggle = Field(default_factory=ModuleToggle)
    platform: PlatformSettings = Field(default_factory=PlatformSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    log: LogSettings = Field(default_factory=LogSettings)
    crawler: CrawlerSettings = Field(default_factory=CrawlerSettings)
    ukey: UKeySettings = Field(default_factory=UKeySettings)
    alert: AlertSettings = Field(default_factory=AlertSettings)
    delivery: DeliverySettings = Field(default_factory=DeliverySettings)
    rpa: RpaSettings = Field(default_factory=RpaSettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    schedule: ScheduleSettings = Field(default_factory=ScheduleSettings)
    web: WebSettings = Field(default_factory=WebSettings)
    endpoints_file: str = "configs/endpoints.yaml"


def _load_yaml(path: Path) -> dict[str, Any]:
    """加载 yaml 并递归合并 ``inherits`` 指向的父文件（子覆盖父）。"""
    path = Path(path)
    # Windows 上 git clone 可能产生 GBK 编码的 YAML，先试 UTF-8 再回退 GBK
    data: dict[str, Any] = {}
    for enc in ("utf-8", "gbk", "utf-8-sig"):
        try:
            with path.open("r", encoding=enc) as f:
                data = yaml.safe_load(f) or {}
            break
        except (UnicodeDecodeError, UnicodeError):
            continue

    inherits = data.pop("inherits", None)
    if inherits:
        base = _load_yaml(path.parent / inherits)
        return _deep_merge(base, data)
    return data


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """递归合并字典：override 覆盖 base，嵌套 dict 深度合并。"""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(config_path: str | Path) -> AppSettings:
    """从 yaml 配置文件加载 :class:`AppSettings`。

    若进程带有 ``SPIDER_GD_HOME``，会先把安装目录里的旧用户数据复制过去，
    再把相对的数据路径改到该目录下。未设置时与原来一样，相对当前工作目录。
    """
    from spider_gd.config.user_home import prepare_user_home, rebase_user_paths

    prepare_user_home(Path.cwd())
    raw = _load_yaml(Path(config_path))
    return rebase_user_paths(AppSettings(**raw))


def load_endpoints(path: str | Path) -> list[EndpointConfig]:
    """从单文件 endpoints.yaml 加载端点列表，复用 :class:`EndpointConfig` dataclass。

    阶段 1 用单文件即可（YAGNI），端点超过一定规模再拆目录。
    """
    path = Path(path)
    if not path.exists():
        return []
    raw = _load_yaml(path)
    items = raw.get("endpoints", []) or []
    return [EndpointConfig(**item) for item in items]


def parse_cli(argv: list[str] | None = None) -> AppSettings:
    """解析 ``--config`` 参数并加载配置。"""
    parser = argparse.ArgumentParser(description="spider-gd 配置加载")
    parser.add_argument("--config", required=True, help="yaml 配置文件路径")
    args, _ = parser.parse_known_args(argv)
    return load_config(args.config)
