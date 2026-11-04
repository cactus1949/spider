"""统一入口（链路0）。

``python -m spider_gd --help`` 列出所有可独立运行的子命令。
各子命令的具体实现由对应链路填充。
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from spider_gd.config.delivery_env import (
    DELIVERY_ENV_KEYS,
    DeliveryEnvFileError,
    read_delivery_env_file,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="spider-gd",
        description="广东电力交易数据智能采集系统",
    )
    parser.add_argument(
        "--config",
        default="configs/mode_d_offline.yaml",
        help="yaml 配置文件路径（默认离线模式）",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    p_run = sub.add_parser("run", help="按配置启动主程序（装配各模块）")
    p_run.set_defaults(func=_cmd_run)

    p_crawl = sub.add_parser("crawl", help="链路1：执行一次采集")
    p_crawl.add_argument("--task", default="采集任务", help="任务名（用于目录/台账/状态库）")
    p_crawl.add_argument("--date", help="目标日期 YYYY-MM-DD（默认昨天）")
    p_crawl.add_argument("--resume", metavar="TASK_ID", help="断点续采：指定已存在的 task_id")
    p_crawl.add_argument("--dry-run", action="store_true", help="只打印将采集的端点，不启动浏览器")
    p_crawl.set_defaults(func=_cmd_crawl)

    p_net = sub.add_parser("network", help="链路2：网络监控/配置/验证")
    p_net.add_argument("--monitor", action="store_true")
    p_net.add_argument("--configure", action="store_true")
    p_net.add_argument("--validate", action="store_true")
    p_net.set_defaults(func=_cmd_network)

    p_sched = sub.add_parser("schedule", help="链路3：任务调度")
    p_sched.add_argument("--start", action="store_true")
    p_sched.add_argument("--list", action="store_true")
    p_sched.add_argument("--trigger", metavar="JOB_ID")
    p_sched.set_defaults(func=_cmd_schedule)

    p_alert = sub.add_parser("alert", help="链路4：告警网关")
    p_alert.add_argument("--test", metavar="MESSAGE")
    p_alert.set_defaults(func=_cmd_alert)

    p_health = sub.add_parser("health", help="链路6：健康检查")
    p_health.add_argument("--check", action="store_true")
    p_health.add_argument("--monitor", action="store_true")
    p_health.set_defaults(func=_cmd_health)

    p_agent = sub.add_parser("agent", help="链路5：远程 Agent（可选）")
    p_agent.add_argument("--simulate", metavar="TEXT")
    p_agent.set_defaults(func=_cmd_agent)

    p_delivery = sub.add_parser("delivery-test", help="稳定发送链路自检：企微消息/文件/远程链接")
    p_delivery.add_argument("--file", default="", help="要试发的本地文件；为空则只验证消息配置")
    p_delivery.add_argument(
        "--sample-zip",
        action="store_true",
        help="未传 --file 时自动生成一个样例 zip，真实验证文件/链接发送链路",
    )
    p_delivery.add_argument(
        "--task-id",
        default="",
        help="测试消息中的任务ID；为空时真实文件发送自动生成，避免重复测试被幂等跳过",
    )
    p_delivery.add_argument("--send", action="store_true", help="真实发送；默认只 dry-run")
    p_delivery.add_argument(
        "--delivery-backend",
        choices=["wecom_webhook", "serverchan", "pushplus"],
        default="",
        help="本次测试临时覆盖发送通道；为空时按提供的 token/webhook 自动判断",
    )
    p_delivery.add_argument("--env-file", default="", help="从 env 文件读取本次测试临时凭据")
    p_delivery.add_argument("--wecom-webhook", default="", help="本次测试临时覆盖企业微信机器人 Webhook")
    p_delivery.add_argument("--serverchan-send-key", default="", help="本次测试临时覆盖 Server酱 SendKey")
    p_delivery.add_argument("--serverchan-channel", default="", help="本次测试临时覆盖 Server酱 channel")
    p_delivery.add_argument("--serverchan-openid", default="", help="本次测试临时覆盖 Server酱 openid")
    p_delivery.add_argument("--pushplus-token", default="", help="本次测试临时覆盖 PushPlus Token")
    p_delivery.add_argument("--pushplus-channel", default="", help="本次测试临时覆盖 PushPlus channel")
    p_delivery.add_argument(
        "--file-delivery",
        choices=["auto", "wecom_file", "remote_link"],
        default="",
        help="本次测试临时覆盖文件发送方式",
    )
    p_delivery.add_argument(
        "--force-remote-link",
        action="store_true",
        help="本次测试强制远程上传后发下载链接",
    )
    p_delivery.add_argument("--remote-upload-url", default="", help="本次测试临时覆盖远程上传地址")
    p_delivery.add_argument("--remote-upload-token", default="", help="本次测试临时覆盖远程上传 Token")
    p_delivery.add_argument(
        "--remote-upload-relative-path",
        default="",
        help="本次测试临时覆盖远程上传 relativePath",
    )
    p_delivery.add_argument("--verify", action="store_true", help="真实发送后立即核验回执和 zip/hash")
    p_delivery.add_argument("--no-remote", action="store_true", help="--verify 时不检查远程下载链接")
    p_delivery.add_argument(
        "--download-remote",
        action="store_true",
        help="--verify 时下载 remote_url 并比对大小和 SHA256",
    )
    p_delivery.add_argument(
        "--max-remote-mb",
        type=int,
        default=200,
        help="--download-remote 最大下载核验大小，默认 200MB",
    )
    p_delivery.set_defaults(func=_cmd_delivery_test)

    p_delivery_acceptance = sub.add_parser(
        "delivery-acceptance",
        help="现场一键验收稳定发送：样例 zip、真实发送、回执与远程核验",
    )
    p_delivery_acceptance.add_argument(
        "--file",
        default="",
        help="要验收发送的本地 zip；为空时自动生成样例 zip",
    )
    p_delivery_acceptance.add_argument(
        "--task-id",
        default="",
        help="验收任务ID；为空时每次生成新的验收ID，确保真实重发",
    )
    p_delivery_acceptance.add_argument(
        "--receipt",
        default="",
        help="验收回执 JSON 路径；为空时写入数据目录 _stable_delivery/receipts",
    )
    _add_delivery_cli_override_arguments(p_delivery_acceptance, "验收")
    p_delivery_acceptance.add_argument("--dry-run", action="store_true", help="只体检，不真实发送")
    p_delivery_acceptance.add_argument(
        "--require-remote-url",
        action="store_true",
        help="验收必须产出 remote_url，并自动切到远程链接模式",
    )
    p_delivery_acceptance.add_argument(
        "--download-remote",
        action="store_true",
        help="强制下载 remote_url 并比对大小和 SHA256",
    )
    p_delivery_acceptance.add_argument("--no-remote", action="store_true", help="不检查远程下载链接")
    p_delivery_acceptance.add_argument(
        "--max-remote-mb",
        type=int,
        default=200,
        help="远程下载核验最大大小，默认 200MB",
    )
    p_delivery_acceptance.set_defaults(func=_cmd_delivery_acceptance)

    p_delivery_send = sub.add_parser(
        "delivery-send",
        help="正式发送任务产物 zip：走稳定发送和可靠 outbox",
    )
    p_delivery_send.add_argument("--file", required=True, help="要发送的本地 zip 文件")
    p_delivery_send.add_argument(
        "--task-id",
        default="",
        help="任务ID；为空时按文件路径生成稳定ID，重复执行同一路径不会重复发送已送达附件",
    )
    p_delivery_send.add_argument(
        "--task-dir",
        default="",
        help="任务目录；为空时使用文件所在目录",
    )
    p_delivery_send.add_argument("--total", type=int, default=1, help="任务汇总 total")
    p_delivery_send.add_argument("--success", type=int, default=1, help="任务汇总 success")
    p_delivery_send.add_argument("--failed", type=int, default=0, help="任务汇总 failed")
    p_delivery_send.add_argument(
        "--receipt",
        default="",
        help="发送回执 JSON 路径；为空时写入数据目录 _stable_delivery/receipts",
    )
    p_delivery_send.add_argument("--wecom-webhook", default="", help="本次发送临时覆盖企业微信机器人 Webhook")
    p_delivery_send.add_argument("--env-file", default="", help="从 env 文件读取本次发送临时凭据")
    p_delivery_send.add_argument(
        "--delivery-backend",
        choices=["wecom_webhook", "serverchan", "pushplus"],
        default="",
        help="本次发送临时覆盖发送通道；为空时按提供的 token/webhook 自动判断",
    )
    p_delivery_send.add_argument("--serverchan-send-key", default="", help="本次发送临时覆盖 Server酱 SendKey")
    p_delivery_send.add_argument("--serverchan-channel", default="", help="本次发送临时覆盖 Server酱 channel")
    p_delivery_send.add_argument("--serverchan-openid", default="", help="本次发送临时覆盖 Server酱 openid")
    p_delivery_send.add_argument("--pushplus-token", default="", help="本次发送临时覆盖 PushPlus Token")
    p_delivery_send.add_argument("--pushplus-channel", default="", help="本次发送临时覆盖 PushPlus channel")
    p_delivery_send.add_argument(
        "--file-delivery",
        choices=["auto", "wecom_file", "remote_link"],
        default="",
        help="本次发送临时覆盖文件发送方式",
    )
    p_delivery_send.add_argument(
        "--force-remote-link",
        action="store_true",
        help="本次发送强制远程上传后发下载链接，等价于 --file-delivery remote_link",
    )
    p_delivery_send.add_argument("--remote-upload-url", default="", help="本次发送临时覆盖远程上传地址")
    p_delivery_send.add_argument("--remote-upload-token", default="", help="本次发送临时覆盖远程上传 Token")
    p_delivery_send.add_argument(
        "--remote-upload-relative-path",
        default="",
        help="本次发送临时覆盖远程上传 relativePath",
    )
    p_delivery_send.add_argument("--dry-run", action="store_true", help="只检查配置和发送策略")
    p_delivery_send.add_argument("--verify", action="store_true", help="发送后立即核验回执和 zip/hash")
    p_delivery_send.add_argument("--no-remote", action="store_true", help="--verify 时不检查远程下载链接")
    p_delivery_send.add_argument(
        "--download-remote",
        action="store_true",
        help="发送后下载 remote_url 并比对大小和 SHA256；会自动开启 --verify",
    )
    p_delivery_send.add_argument(
        "--max-remote-mb",
        type=int,
        default=200,
        help="--download-remote 最大下载核验大小，默认 200MB",
    )
    p_delivery_send.set_defaults(func=_cmd_delivery_send)

    p_delivery_doctor = sub.add_parser(
        "delivery-doctor",
        help="稳定发送体检：配置、发送策略和 outbox 汇总",
    )
    p_delivery_doctor.add_argument(
        "--file",
        default="",
        help="按指定文件预估发送策略；为空则只检查消息配置",
    )
    p_delivery_doctor.add_argument("--limit", type=int, default=5, help="每类 outbox 最多展示多少条")
    p_delivery_doctor.add_argument(
        "--delivery-backend",
        choices=["wecom_webhook", "serverchan", "pushplus"],
        default="",
        help="本次体检临时覆盖发送通道；为空时按提供的 token/webhook 自动判断",
    )
    p_delivery_doctor.add_argument("--env-file", default="", help="从 env 文件读取本次体检临时凭据")
    p_delivery_doctor.add_argument("--wecom-webhook", default="", help="本次体检临时覆盖企业微信机器人 Webhook")
    p_delivery_doctor.add_argument("--serverchan-send-key", default="", help="本次体检临时覆盖 Server酱 SendKey")
    p_delivery_doctor.add_argument("--serverchan-channel", default="", help="本次体检临时覆盖 Server酱 channel")
    p_delivery_doctor.add_argument("--serverchan-openid", default="", help="本次体检临时覆盖 Server酱 openid")
    p_delivery_doctor.add_argument("--pushplus-token", default="", help="本次体检临时覆盖 PushPlus Token")
    p_delivery_doctor.add_argument("--pushplus-channel", default="", help="本次体检临时覆盖 PushPlus channel")
    p_delivery_doctor.add_argument(
        "--file-delivery",
        choices=["auto", "wecom_file", "remote_link"],
        default="",
        help="本次体检临时覆盖文件发送方式",
    )
    p_delivery_doctor.add_argument(
        "--force-remote-link",
        action="store_true",
        help="本次体检强制远程上传后发下载链接",
    )
    p_delivery_doctor.add_argument("--remote-upload-url", default="", help="本次体检临时覆盖远程上传地址")
    p_delivery_doctor.add_argument("--remote-upload-token", default="", help="本次体检临时覆盖远程上传 Token")
    p_delivery_doctor.add_argument(
        "--remote-upload-relative-path",
        default="",
        help="本次体检临时覆盖远程上传 relativePath",
    )
    p_delivery_doctor.set_defaults(func=_cmd_delivery_doctor)

    p_delivery_retry = sub.add_parser("delivery-retry", help="补发稳定发送 outbox 中的任务产物")
    p_delivery_retry.add_argument("--limit", type=int, default=20, help="本次最多补发多少条")
    _add_delivery_cli_override_arguments(p_delivery_retry, "补发")
    p_delivery_retry.add_argument(
        "--include-failed",
        action="store_true",
        help="补发前先把 failed 记录恢复为 pending",
    )
    p_delivery_retry.set_defaults(func=_cmd_delivery_retry)

    p_delivery_outbox = sub.add_parser("delivery-outbox", help="查看/维护稳定发送 outbox")
    p_delivery_outbox.add_argument(
        "--status",
        choices=["pending", "sending", "failed", "sent", "all"],
        default="pending",
        help="查看哪类记录，默认 pending；all 查看全部",
    )
    p_delivery_outbox.add_argument("--limit", type=int, default=20, help="最多返回多少条记录")
    p_delivery_outbox.add_argument(
        "--requeue-failed",
        action="store_true",
        help="把 failed 记录恢复为 pending 后再列出",
    )
    p_delivery_outbox.add_argument(
        "--cleanup",
        choices=["sent", "failed"],
        default="",
        help="清理指定状态的历史记录后再列出",
    )
    p_delivery_outbox.set_defaults(func=_cmd_delivery_outbox)

    p_delivery_verify = sub.add_parser(
        "delivery-verify",
        help="核验稳定发送回执：状态、本地 zip hash、远程链接可访问性",
    )
    p_delivery_verify.add_argument("--task-id", default="", help="从 outbox 按任务ID读取回执")
    p_delivery_verify.add_argument("--receipt", default="", help="从 JSON 回执文件读取")
    p_delivery_verify.add_argument(
        "--no-remote",
        action="store_true",
        help="不验证 remote_url 可访问性，只检查回执和本地 zip",
    )
    p_delivery_verify.add_argument(
        "--download-remote",
        action="store_true",
        help="下载 remote_url 并比对大小和 SHA256（默认只验证链接可访问）",
    )
    p_delivery_verify.add_argument(
        "--max-remote-mb",
        type=int,
        default=200,
        help="--download-remote 最大下载核验大小，默认 200MB",
    )
    p_delivery_verify.set_defaults(func=_cmd_delivery_verify)

    p_wechat = sub.add_parser(
        "wechat-poc",
        help="兼容入口：默认走稳定通道；显式指定 backend 才控制本机微信",
    )
    p_wechat.add_argument(
        "--target",
        default="",
        help="目标会话名称，如 文件传输助手 或测试群名",
    )
    p_wechat.add_argument("--message", default="", help="要发送的文本消息")
    p_wechat.add_argument("--file", default="", help="要发送的本地文件路径")
    p_wechat.add_argument(
        "--app-name",
        default="",
        help="微信应用/窗口名，默认自动尝试 WeChat/微信",
    )
    p_wechat.add_argument("--pause", type=float, default=0.7, help="每一步 UI 操作间隔秒数")
    p_wechat.add_argument(
        "--backend",
        choices=["auto", "wxauto", "powershell", "wecom-webhook"],
        default="auto",
        help=(
            "发送后端：auto 只使用已配置的稳定发送通道；"
            "wecom-webhook 走企业微信机器人稳定接口；"
            "本机 PoC 需显式指定 powershell 或 wxauto"
        ),
    )
    p_wechat.add_argument("--webhook", default="", help="企业微信群机器人 Webhook")
    p_wechat.add_argument("--env-file", default="", help="稳定通道从 env 文件读取临时凭据")
    p_wechat.add_argument(
        "--task-id",
        default="",
        help="稳定通道带文件时使用的发送任务ID；为空按文件路径生成",
    )
    p_wechat.add_argument(
        "--receipt",
        default="",
        help="稳定通道带文件时输出的回执 JSON 路径；为空写入数据目录",
    )
    p_wechat.add_argument(
        "--file-delivery",
        choices=["auto", "wecom_file", "remote_link"],
        default="",
        help="稳定通道带文件时临时覆盖文件发送方式",
    )
    p_wechat.add_argument(
        "--force-remote-link",
        action="store_true",
        help="稳定通道带文件时强制远程上传后发下载链接",
    )
    p_wechat.add_argument("--remote-upload-url", default="", help="稳定通道带文件时临时覆盖远程上传地址")
    p_wechat.add_argument("--remote-upload-token", default="", help="稳定通道带文件时临时覆盖远程上传 Token")
    p_wechat.add_argument(
        "--remote-upload-relative-path",
        default="",
        help="稳定通道带文件时临时覆盖远程上传 relativePath",
    )
    p_wechat.add_argument(
        "--verify",
        action="store_true",
        help="稳定通道带文件真实发送后立即核验回执和 zip/hash",
    )
    p_wechat.add_argument(
        "--no-remote",
        action="store_true",
        help="稳定通道 --verify 时不检查远程下载链接",
    )
    p_wechat.add_argument(
        "--download-remote",
        action="store_true",
        help="稳定通道带文件发送后下载 remote_url 并比对大小和 SHA256",
    )
    p_wechat.add_argument(
        "--max-remote-mb",
        type=int,
        default=200,
        help="--download-remote 最大下载核验大小，默认 200MB",
    )
    p_wechat.add_argument(
        "--select-mode",
        choices=["exact", "current", "bound", "legacy-search"],
        default="current",
        help=(
            "目标会话选择方式：current 只校验当前会话（默认，不搜索）；bound 使用绑定指纹；"
            "exact 精确找名称（搜索式，仅临时 PoC）；"
            "legacy-search 兼容旧参数，但已改为分组安全选择，不再方向键回车"
        ),
    )
    p_wechat.add_argument(
        "--profile-path",
        default="./data/wechat_targets.json",
        help="绑定目标配置文件",
    )
    p_wechat.add_argument("--hash-threshold", type=int, default=12, help="bound 模式标题指纹容差")
    p_wechat.add_argument("--bind", action="store_true", help="把当前微信会话绑定为固定目标")
    p_wechat.add_argument(
        "--input-x-ratio",
        type=float,
        default=0.70,
        help="绑定模式输入框横向比例",
    )
    p_wechat.add_argument(
        "--input-y-ratio",
        type=float,
        default=0.90,
        help="绑定模式输入框纵向比例",
    )
    p_wechat.add_argument("--title-x-ratio", type=float, default=0.38, help="绑定标题区域横向比例")
    p_wechat.add_argument("--title-y-ratio", type=float, default=0.04, help="绑定标题区域纵向比例")
    p_wechat.add_argument(
        "--title-width-ratio",
        type=float,
        default=0.45,
        help="绑定标题区域宽度比例",
    )
    p_wechat.add_argument(
        "--title-height-ratio",
        type=float,
        default=0.08,
        help="绑定标题区域高度比例",
    )
    p_wechat.add_argument(
        "--search-down",
        type=int,
        default=2,
        help="兼容旧参数；legacy-search 现不再使用方向键回车，故该值会被忽略",
    )
    p_wechat.add_argument(
        "--allow-unstable-search",
        action="store_true",
        help="允许真实发送使用 exact/legacy-search 搜索式选会话；仅临时 PoC 使用",
    )
    p_wechat.add_argument("--inspect", action="store_true", help="只打印微信窗口控件树，不发送")
    p_wechat.add_argument("--inspect-depth", type=int, default=4, help="控件树检查深度")
    p_wechat.add_argument("--inspect-filter", default="", help="控件树名称/类型过滤关键字")
    p_wechat.add_argument("--send", action="store_true", help="真实操作微信发送；默认只 dry-run")
    p_wechat.set_defaults(func=_cmd_wechat_poc)

    return parser


def _add_delivery_cli_override_arguments(parser: argparse.ArgumentParser, scope: str) -> None:
    parser.add_argument(
        "--delivery-backend",
        choices=["wecom_webhook", "serverchan", "pushplus"],
        default="",
        help=f"本次{scope}临时覆盖发送通道；为空时按提供的 token/webhook 自动判断",
    )
    parser.add_argument("--env-file", default="", help=f"从 env 文件读取本次{scope}临时凭据")
    parser.add_argument("--wecom-webhook", default="", help=f"本次{scope}临时覆盖企业微信机器人 Webhook")
    parser.add_argument("--serverchan-send-key", default="", help=f"本次{scope}临时覆盖 Server酱 SendKey")
    parser.add_argument("--serverchan-channel", default="", help=f"本次{scope}临时覆盖 Server酱 channel")
    parser.add_argument("--serverchan-openid", default="", help=f"本次{scope}临时覆盖 Server酱 openid")
    parser.add_argument("--pushplus-token", default="", help=f"本次{scope}临时覆盖 PushPlus Token")
    parser.add_argument("--pushplus-channel", default="", help=f"本次{scope}临时覆盖 PushPlus channel")
    parser.add_argument(
        "--file-delivery",
        choices=["auto", "wecom_file", "remote_link"],
        default="",
        help=f"本次{scope}临时覆盖文件发送方式",
    )
    parser.add_argument(
        "--force-remote-link",
        action="store_true",
        help=f"本次{scope}强制远程上传后发下载链接",
    )
    parser.add_argument("--remote-upload-url", default="", help=f"本次{scope}临时覆盖远程上传地址")
    parser.add_argument("--remote-upload-token", default="", help=f"本次{scope}临时覆盖远程上传 Token")
    parser.add_argument(
        "--remote-upload-relative-path",
        default="",
        help=f"本次{scope}临时覆盖远程上传 relativePath",
    )


def _cmd_run(args: argparse.Namespace) -> int:
    from spider_gd.app import run_app
    from spider_gd.config.settings import load_config

    settings = load_config(args.config)
    return run_app(settings)


def _cmd_crawl(args: argparse.Namespace) -> int:
    from datetime import datetime, timedelta

    from spider_gd.app import run_crawl
    from spider_gd.config.settings import load_config

    settings = load_config(args.config)
    target_date = args.date or (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    return run_crawl(
        settings,
        target_date=target_date,
        task_name=args.task,
        task_id=args.resume,
        dry_run=args.dry_run,
    )


def _cmd_network(args: argparse.Namespace) -> int:
    from spider_gd.config.settings import load_config

    settings = load_config(args.config)
    try:
        if args.validate or args.monitor:
            from spider_gd.infrastructure.network.network_validator import NetworkValidator

            report = NetworkValidator().validate(settings.platform.domain, [])
            print(f"[network] 连通性：public={report.public_ok} private={report.private_ok} "
                  f"routes={report.routes_ok} passed={report.passed}")
        elif args.configure:
            print("[network] 路由配置为破坏性操作，请使用 deploy/setup_routes.ps1（需管理员）")
        else:
            print("[network] 用法：--monitor | --configure | --validate")
    except Exception as exc:  # 网络模块强依赖 Windows/PowerShell，非目标环境优雅降级
        print(f"[network] 执行失败（可能非 Windows 环境）：{exc}")
        return 1
    return 0


def _cmd_schedule(args: argparse.Namespace) -> int:
    import asyncio

    from spider_gd.app import assemble, register_jobs
    from spider_gd.config.settings import load_config

    settings = load_config(args.config)
    ctx = assemble(settings)

    async def _run() -> int:
        await register_jobs(ctx)
        if args.list:
            for spec in await ctx.scheduler.list_jobs():
                print(f"  {spec.job_id:16} cron={spec.cron} enabled={spec.enabled}")
        elif args.trigger:
            ok = await ctx.scheduler.trigger_now(args.trigger)
            print(f"[schedule] 触发 {args.trigger}：{'成功' if ok else '失败（未知 job）'}")
            return 0 if ok else 1
        elif args.start:
            await ctx.scheduler.start()
            print("[schedule] 调度器已启动（Ctrl+C 退出）")
            await asyncio.Event().wait()
        else:
            print("[schedule] 用法：--start | --list | --trigger JOB_ID")
        return 0

    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        return 0
    finally:
        ctx.state_store.close()


def _cmd_alert(args: argparse.Namespace) -> int:
    import asyncio

    from spider_gd.app import assemble
    from spider_gd.config.settings import load_config
    from spider_gd.interfaces.alert import AlertLevel

    settings = load_config(args.config)
    ctx = assemble(settings)
    try:
        message = args.test or "spider-gd 告警测试"
        result = asyncio.run(ctx.alert.send_alert(AlertLevel.INFO, message, event_type="test"))
        print(f"[alert] 发送：sent={result.sent} suppressed={result.suppressed} "
              f"err={result.error_msg}")
        return 0
    finally:
        ctx.state_store.close()


def _cmd_health(args: argparse.Namespace) -> int:
    import asyncio
    import json

    from spider_gd.app import assemble
    from spider_gd.config.settings import load_config
    from spider_gd.gateway.control_service import ControlService

    settings = load_config(args.config)
    ctx = assemble(settings)
    try:
        control = ctx.control
        assert isinstance(control, ControlService)
        status = asyncio.run(control.health())
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 0
    finally:
        ctx.state_store.close()


def _cmd_agent(args: argparse.Namespace) -> int:
    import asyncio

    from spider_gd.app import assemble
    from spider_gd.config.settings import load_config

    settings = load_config(args.config)
    if not (settings.modules.agent and settings.agent.enabled):
        print("[agent] 未启用：请在配置中开启 modules.agent 与 agent.enabled")
        return 1
    from spider_gd.gateway.agent.agent_adapter import AgentAdapter

    ctx = assemble(settings)
    try:
        agent = ctx.agent
        assert isinstance(agent, AgentAdapter)
        text = args.simulate or "查询状态"
        response = asyncio.run(agent.handle_text(text))
        print(f"[agent] {text!r} → success={response.success} {response.message}")
        return 0 if response.success else 1
    finally:
        ctx.state_store.close()


def _cmd_delivery_test(args: argparse.Namespace) -> int:
    import asyncio
    import json
    from datetime import datetime
    from pathlib import Path

    from spider_gd.app import (
        _build_delivery_hook,
        _build_reliable_delivery_hook,
        assemble,
        build_delivery_test_plan,
        create_delivery_sample_zip,
        delivery_outbox_receipt,
        send_delivery_test_message,
        write_delivery_receipt,
    )
    from spider_gd.config.settings import load_config

    settings = load_config(args.config)
    ctx = assemble(settings)
    try:
        overrides = _apply_delivery_cli_overrides(ctx, args)
        file_path = Path(args.file).expanduser().resolve() if args.file else None
        if file_path is None and args.sample_zip:
            file_path = create_delivery_sample_zip(ctx.settings.storage.data_dir)
        hook = _build_delivery_hook(ctx)
        if hook is None:
            plan = build_delivery_test_plan(ctx, file_path=file_path, dry_run=not args.send)
            if overrides:
                plan["overrides"] = _public_delivery_overrides(overrides)
            plan["hint"] = "在配置中设置 delivery.enabled=true 并配置 wecom_webhook"
            print(json.dumps(plan, ensure_ascii=False, indent=2))
            return 1

        plan = build_delivery_test_plan(ctx, file_path=file_path, dry_run=not args.send)
        if overrides:
            plan["overrides"] = _public_delivery_overrides(overrides)
        if not plan.get("ok"):
            print(json.dumps(plan, ensure_ascii=False, indent=2))
            return 1

        if not args.send:
            print(json.dumps(plan, ensure_ascii=False, indent=2))
            return 0

        if file_path is None:
            ok = asyncio.run(send_delivery_test_message(ctx))
        else:
            ok = True
            receipt_path = None
            reliable_hook = _build_reliable_delivery_hook(ctx)
            task_id = (
                args.task_id.strip()
                or f"DELIVERY_TEST_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
            )
            plan["task_id"] = task_id

            async def _send() -> None:
                await reliable_hook(
                    task_id,
                    str(file_path.parent),
                    str(file_path),
                    {"total": 1, "success": 1, "failed": 0},
                )

            try:
                asyncio.run(_send())
            except Exception as exc:
                ok = False
                plan["error"] = str(exc)
            reader = getattr(ctx.state_store, "get_delivery_outbox", None)
            if callable(reader):
                receipt = delivery_outbox_receipt(reader(task_id))
                plan["outbox"] = receipt
                if receipt:
                    receipt_path = write_delivery_receipt(
                        ctx.settings.storage.data_dir,
                        receipt,
                        command="delivery-test",
                        extra={
                            "summary": {"total": 1, "success": 1, "failed": 0},
                            "sample_zip": bool(args.sample_zip),
                        },
                    )
                    plan["receipt_path"] = str(receipt_path)
            if _should_verify_delivery(args):
                verification = _run_delivery_verification(
                    ctx,
                    task_id=task_id,
                    receipt_path=receipt_path,
                    args=args,
                )
                plan["verification"] = verification
                if not verification.get("ok"):
                    ok = False

        plan["dry_run"] = False
        plan["sent"] = ok
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0 if ok else 1
    finally:
        ctx.state_store.close()


def _default_delivery_send_task_id(file_path: "Path") -> str:
    import hashlib
    import re

    path_text = str(file_path.expanduser().resolve())
    digest = hashlib.sha1(path_text.encode("utf-8")).hexdigest()[:10]
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", file_path.stem).strip("._-")
    if not slug:
        slug = "FILE"
    return f"DELIVERY_SEND_{slug[:36]}_{digest}"


def _should_verify_delivery(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "verify", False) or getattr(args, "download_remote", False))


_SENSITIVE_DELIVERY_OVERRIDE_KEYS = {
    "deliveryWecomWebhook",
    "deliveryServerchanSendKey",
    "deliveryServerchanOpenid",
    "deliveryPushplusToken",
    "deliveryRemoteUploadToken",
}


def _redact_delivery_secret(value: str) -> str:
    text = str(value)
    if not text:
        return ""
    if "://" in text:
        from urllib.parse import urlsplit, urlunsplit

        parts = urlsplit(text)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    if len(text) <= 8:
        return "***"
    return f"{text[:4]}...***"


def _public_delivery_overrides(overrides: dict[str, str]) -> dict[str, str]:
    return {
        key: _redact_delivery_secret(value)
        if key in _SENSITIVE_DELIVERY_OVERRIDE_KEYS
        else value
        for key, value in overrides.items()
    }


def _delivery_env_file_values(args: argparse.Namespace) -> dict[str, str]:
    path = str(getattr(args, "env_file", "") or "").strip()
    if not path:
        return {}
    cache_key = "_spider_gd_delivery_env_file_values"
    cached = getattr(args, cache_key, None)
    if isinstance(cached, dict):
        return cached
    values = read_delivery_env_file(path)
    setattr(args, cache_key, values)
    return values


def _delivery_env_file_diagnostics(args: argparse.Namespace) -> dict[str, object]:
    from pathlib import Path

    path_text = str(getattr(args, "env_file", "") or "").strip()
    if not path_text:
        return {
            "configured": False,
            "path": "",
            "loaded": False,
            "known_keys_present": [],
            "ignored_key_count": 0,
        }
    values = _delivery_env_file_values(args)
    known = set(DELIVERY_ENV_KEYS)
    known_keys_present = [
        key for key in DELIVERY_ENV_KEYS
        if values.get(key, "").strip()
    ]
    return {
        "configured": True,
        "path": str(Path(path_text).expanduser().resolve()),
        "loaded": True,
        "known_keys_present": known_keys_present,
        "ignored_key_count": len([key for key in values if key not in known]),
    }


def _delivery_credential_diagnostics(
    args: argparse.Namespace,
    *,
    plan: dict[str, object],
    overrides: dict[str, str],
) -> dict[str, object]:
    backend = str(plan.get("backend") or "").strip().lower().replace("-", "_")
    channel_configured = {
        "wecom_webhook": bool(plan.get("wecom_webhook_configured")),
        "serverchan": bool(plan.get("serverchan_configured")),
        "pushplus": bool(plan.get("pushplus_configured")),
    }
    return {
        "env_file": _delivery_env_file_diagnostics(args),
        "backend": backend,
        "selected_channel_configured": bool(channel_configured.get(backend)),
        "channels": channel_configured,
        "send_message": bool(plan.get("send_message")),
        "send_file": bool(plan.get("send_file")),
        "file_delivery": str(plan.get("file_delivery") or ""),
        "remote_upload_configured": bool(plan.get("remote_upload_configured")),
        "override_keys": sorted(_public_delivery_overrides(overrides)),
    }


def _delivery_cli_value(args: argparse.Namespace, attr: str, env_name: str) -> str:
    import os

    value = str(getattr(args, attr, "") or "").strip()
    if value:
        return value
    file_value = _delivery_env_file_values(args).get(env_name, "").strip()
    if file_value:
        return file_value
    return os.environ.get(env_name, "").strip()


def _apply_delivery_cli_overrides(
    ctx,
    args: argparse.Namespace,
    *,
    wecom_webhook: str = "",
    enable_wecom: bool = False,
) -> dict[str, str]:
    overrides: dict[str, str] = {}
    backend = _delivery_cli_value(args, "delivery_backend", "SPIDER_GD_DELIVERY_BACKEND").replace("-", "_")
    webhook = wecom_webhook.strip() or _delivery_cli_value(
        args,
        "wecom_webhook",
        "SPIDER_GD_WECOM_WEBHOOK",
    )
    serverchan_send_key = _delivery_cli_value(
        args,
        "serverchan_send_key",
        "SPIDER_GD_SERVERCHAN_SEND_KEY",
    )
    pushplus_token = _delivery_cli_value(args, "pushplus_token", "SPIDER_GD_PUSHPLUS_TOKEN")
    if not backend:
        if serverchan_send_key:
            backend = "serverchan"
        elif pushplus_token:
            backend = "pushplus"
        elif webhook or enable_wecom:
            backend = "wecom_webhook"
    if backend:
        overrides["deliveryEnabled"] = "true"
        overrides["deliveryBackend"] = backend
    if webhook:
        overrides["deliveryWecomWebhook"] = webhook
    if serverchan_send_key:
        overrides["deliveryServerchanSendKey"] = serverchan_send_key
    serverchan_channel = _delivery_cli_value(
        args,
        "serverchan_channel",
        "SPIDER_GD_SERVERCHAN_CHANNEL",
    )
    if serverchan_channel:
        overrides["deliveryServerchanChannel"] = serverchan_channel
    serverchan_openid = _delivery_cli_value(
        args,
        "serverchan_openid",
        "SPIDER_GD_SERVERCHAN_OPENID",
    )
    if serverchan_openid:
        overrides["deliveryServerchanOpenid"] = serverchan_openid
    if pushplus_token:
        overrides["deliveryPushplusToken"] = pushplus_token
    pushplus_channel = _delivery_cli_value(args, "pushplus_channel", "SPIDER_GD_PUSHPLUS_CHANNEL")
    if pushplus_channel:
        overrides["deliveryPushplusChannel"] = pushplus_channel
    if getattr(args, "force_remote_link", False):
        overrides["deliveryFileDelivery"] = "remote_link"
    else:
        file_delivery = _delivery_cli_value(args, "file_delivery", "SPIDER_GD_FILE_DELIVERY")
        if file_delivery:
            overrides["deliveryFileDelivery"] = file_delivery
    remote_upload_url = _delivery_cli_value(
        args,
        "remote_upload_url",
        "SPIDER_GD_REMOTE_UPLOAD_URL",
    )
    if remote_upload_url:
        overrides["deliveryRemoteUploadUrl"] = remote_upload_url
    remote_upload_token = _delivery_cli_value(
        args,
        "remote_upload_token",
        "SPIDER_GD_REMOTE_UPLOAD_TOKEN",
    )
    if remote_upload_token:
        overrides["deliveryRemoteUploadToken"] = remote_upload_token
    relative_path = _delivery_cli_value(
        args,
        "remote_upload_relative_path",
        "SPIDER_GD_REMOTE_UPLOAD_RELATIVE_PATH",
    )
    if relative_path:
        overrides["deliveryRemoteUploadRelativePath"] = relative_path
    if overrides:
        current = getattr(ctx, "delivery_overrides", None) or {}
        ctx.delivery_overrides = {**current, **overrides}
    return overrides


def _run_delivery_verification(
    ctx,
    *,
    task_id: str,
    receipt_path,
    args: argparse.Namespace,
) -> dict[str, object]:
    import asyncio

    from spider_gd.app import verify_delivery_receipt

    return asyncio.run(
        verify_delivery_receipt(
            ctx,
            task_id="" if receipt_path else task_id,
            receipt_path=receipt_path,
            verify_remote=not getattr(args, "no_remote", False),
            download_remote=bool(getattr(args, "download_remote", False)),
            max_remote_bytes=max(1, int(getattr(args, "max_remote_mb", 200))) * 1024 * 1024,
        )
    )


def _cmd_delivery_acceptance(args: argparse.Namespace) -> int:
    import asyncio
    import json
    from datetime import datetime
    from pathlib import Path

    from spider_gd.app import (
        _build_reliable_delivery_hook,
        assemble,
        build_delivery_test_plan,
        create_delivery_sample_zip,
        delivery_outbox_receipt,
        write_delivery_receipt,
    )
    from spider_gd.config.settings import load_config

    settings = load_config(args.config)
    ctx = assemble(settings)
    try:
        auto_force_remote_link = False
        if getattr(args, "require_remote_url", False) and not getattr(args, "force_remote_link", False):
            args.force_remote_link = True
            auto_force_remote_link = True
        overrides = _apply_delivery_cli_overrides(ctx, args)
        sample_zip = not bool(str(args.file or "").strip())
        file_path = (
            create_delivery_sample_zip(ctx.settings.storage.data_dir)
            if sample_zip
            else Path(args.file).expanduser().resolve()
        )
        task_id = args.task_id.strip() or f"DELIVERY_ACCEPTANCE_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
        plan = build_delivery_test_plan(ctx, file_path=file_path, dry_run=args.dry_run)
        if overrides:
            plan["overrides"] = _public_delivery_overrides(overrides)

        errors: list[str] = []
        result: dict[str, object] = {
            "ok": bool(plan.get("ok")) and bool(args.dry_run),
            "command": "delivery-acceptance",
            "acceptance": True,
            "dry_run": bool(args.dry_run),
            "sample_zip": sample_zip,
            "task_id": task_id,
            "file": str(file_path),
            "sent": False,
            "auto_force_remote_link": auto_force_remote_link,
            "plan": plan,
        }
        if overrides:
            result["overrides"] = _public_delivery_overrides(overrides)
        if not plan.get("ok"):
            result["errors"] = list(plan.get("errors") or [])
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 1
        if args.dry_run:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0

        reliable_hook = _build_reliable_delivery_hook(ctx)
        summary = {
            "total": 1,
            "success": 1,
            "failed": 0,
            "source": "delivery_acceptance",
            "acceptance": True,
        }

        sent = True

        async def _send() -> None:
            await reliable_hook(task_id, str(file_path.parent), str(file_path), summary)

        try:
            asyncio.run(_send())
        except Exception as exc:
            sent = False
            errors.append(str(exc))

        receipt_path = None
        receipt: dict[str, object] = {}
        reader = getattr(ctx.state_store, "get_delivery_outbox", None)
        if callable(reader):
            receipt = delivery_outbox_receipt(reader(task_id))
            if receipt:
                receipt_path = write_delivery_receipt(
                    ctx.settings.storage.data_dir,
                    receipt,
                    command="delivery-acceptance",
                    output_path=args.receipt.strip() or None,
                    extra={
                        "summary": summary,
                        "sample_zip": sample_zip,
                        "acceptance": True,
                    },
                )
                result["receipt_path"] = str(receipt_path)
        if not receipt:
            errors.append("未生成稳定发送 outbox 回执")
        result["outbox"] = receipt

        remote_url = str(receipt.get("remote_url") or "").strip()
        if args.require_remote_url and not remote_url:
            errors.append("验收要求 remote_url，但本次发送没有生成远程下载链接")

        verify_args = argparse.Namespace(**vars(args))
        verify_args.download_remote = bool(
            not getattr(args, "no_remote", False)
            and (getattr(args, "download_remote", False) or remote_url)
        )
        verification = _run_delivery_verification(
            ctx,
            task_id=task_id,
            receipt_path=receipt_path,
            args=verify_args,
        )
        result["verification"] = verification
        if not verification.get("ok"):
            errors.extend(str(item) for item in verification.get("errors") or [])

        result["sent"] = sent
        if errors:
            result["errors"] = errors
        result["ok"] = sent and not errors
        if receipt and receipt_path is not None:
            final_extra: dict[str, object] = {
                "summary": summary,
                "sample_zip": sample_zip,
                "acceptance": True,
                "acceptance_ok": bool(result["ok"]),
                "sent": sent,
                "auto_force_remote_link": auto_force_remote_link,
                "verification": verification,
                "errors": errors,
                "plan": plan,
            }
            if overrides:
                final_extra["overrides"] = _public_delivery_overrides(overrides)
            receipt_path = write_delivery_receipt(
                ctx.settings.storage.data_dir,
                receipt,
                command="delivery-acceptance",
                output_path=receipt_path,
                extra=final_extra,
            )
            result["receipt_path"] = str(receipt_path)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 1
    finally:
        ctx.state_store.close()


def _cmd_delivery_send(args: argparse.Namespace) -> int:
    import asyncio
    import json
    from pathlib import Path

    from spider_gd.app import (
        _build_reliable_delivery_hook,
        assemble,
        build_delivery_test_plan,
        delivery_outbox_receipt,
        write_delivery_receipt,
    )
    from spider_gd.config.settings import load_config

    settings = load_config(args.config)
    ctx = assemble(settings)
    try:
        overrides = _apply_delivery_cli_overrides(ctx, args)
        file_path = Path(args.file).expanduser().resolve()
        task_dir = Path(args.task_dir).expanduser().resolve() if args.task_dir else file_path.parent
        task_id = args.task_id.strip() or _default_delivery_send_task_id(file_path)
        summary = {
            "total": max(0, int(args.total)),
            "success": max(0, int(args.success)),
            "failed": max(0, int(args.failed)),
        }
        if summary["total"] < summary["success"] + summary["failed"]:
            summary["total"] = summary["success"] + summary["failed"]

        result = build_delivery_test_plan(ctx, file_path=file_path, dry_run=args.dry_run)
        result["command"] = "delivery-send"
        result["task_id"] = task_id
        result["task_dir"] = str(task_dir)
        result["summary"] = summary
        if overrides:
            result["overrides"] = _public_delivery_overrides(overrides)
        if not result.get("ok"):
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 1
        if args.dry_run:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0

        reliable_hook = _build_reliable_delivery_hook(ctx)

        async def _send() -> None:
            await reliable_hook(task_id, str(task_dir), str(file_path), summary)

        ok = True
        receipt_path = None
        try:
            asyncio.run(_send())
        except Exception as exc:
            ok = False
            result["error"] = str(exc)

        reader = getattr(ctx.state_store, "get_delivery_outbox", None)
        if callable(reader):
            receipt = delivery_outbox_receipt(reader(task_id))
            result["outbox"] = receipt
            if receipt:
                receipt_path = write_delivery_receipt(
                    ctx.settings.storage.data_dir,
                    receipt,
                    command="delivery-send",
                    output_path=args.receipt.strip() or None,
                    extra={
                        "task_dir": str(task_dir),
                        "summary": summary,
                    },
                )
                result["receipt_path"] = str(receipt_path)
        if _should_verify_delivery(args):
            verification = _run_delivery_verification(
                ctx,
                task_id=task_id,
                receipt_path=receipt_path,
                args=args,
            )
            result["verification"] = verification
            if not verification.get("ok"):
                ok = False
        result["dry_run"] = False
        result["sent"] = ok
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if ok else 1
    finally:
        ctx.state_store.close()


def _cmd_delivery_doctor(args: argparse.Namespace) -> int:
    import json
    from pathlib import Path

    from spider_gd.app import assemble, build_delivery_test_plan
    from spider_gd.config.settings import load_config

    settings = load_config(args.config)
    ctx = assemble(settings)
    try:
        overrides = _apply_delivery_cli_overrides(ctx, args)
        file_path = Path(args.file).expanduser().resolve() if args.file else None
        limit = max(1, args.limit)
        plan = build_delivery_test_plan(ctx, file_path=file_path, dry_run=True)
        if overrides:
            plan["overrides"] = _public_delivery_overrides(overrides)
        summarizer = getattr(ctx.state_store, "get_delivery_outbox_summary", None)
        lister = getattr(ctx.state_store, "list_delivery_outbox", None)
        summary = summarizer() if callable(summarizer) else {}
        pending = lister(status="pending", limit=limit) if callable(lister) else []
        failed = lister(status="failed", limit=limit) if callable(lister) else []
        sending = lister(status="sending", limit=limit) if callable(lister) else []

        recommendations: list[str] = []
        if not plan.get("ok"):
            recommendations.append("先修复 plan.errors 中的配置问题")
        warnings = plan.get("warnings")
        if isinstance(warnings, list):
            recommendations.extend(str(item) for item in warnings)
        if int(summary.get("failed", 0) or 0) > 0:
            recommendations.append("修复配置/网络后运行 delivery-retry --include-failed")
        if int(summary.get("pending", 0) or 0) > 0:
            recommendations.append("运行 delivery-retry 补发 pending 队列")
        if int(summary.get("sending", 0) or 0) > 0:
            recommendations.append("若 sending 超过 10 分钟，运行 delivery-retry 或前端“检查发送中”会自动恢复并补发")
        queue_clear = (
            int(summary.get("pending", 0) or 0) == 0
            and int(summary.get("failed", 0) or 0) == 0
            and int(summary.get("sending", 0) or 0) == 0
        )
        if plan.get("ok") and queue_clear:
            recommendations.append(
                "配置体检通过；用 delivery-acceptance 做一次真实发送验收；"
                "正式发送任务 zip 用 delivery-send --file"
            )

        ok = (
            bool(plan.get("ok"))
            and queue_clear
        )
        result = {
            "ok": ok,
            "plan": plan,
            "credentials": _delivery_credential_diagnostics(
                args,
                plan=plan,
                overrides=overrides,
            ),
            "overrides": _public_delivery_overrides(overrides),
            "outbox_summary": summary,
            "samples": {
                "pending": pending,
                "failed": failed,
                "sending": sending,
            },
            "recommendations": recommendations,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if ok else 1
    finally:
        ctx.state_store.close()


def _cmd_delivery_retry(args: argparse.Namespace) -> int:
    import asyncio
    import json

    from spider_gd.app import assemble, retry_pending_deliveries
    from spider_gd.config.settings import load_config

    settings = load_config(args.config)
    ctx = assemble(settings)
    try:
        overrides = _apply_delivery_cli_overrides(ctx, args)
        requeued = 0
        if args.include_failed:
            requeuer = getattr(ctx.state_store, "requeue_delivery_outbox", None)
            if callable(requeuer):
                requeued = requeuer(status="failed", limit=max(1, args.limit))
        result = asyncio.run(retry_pending_deliveries(ctx, limit=max(1, args.limit)))
        result["requeued_failed"] = requeued
        if overrides:
            result["overrides"] = _public_delivery_overrides(overrides)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1
    finally:
        ctx.state_store.close()


def _cmd_delivery_outbox(args: argparse.Namespace) -> int:
    import json

    from spider_gd.app import assemble
    from spider_gd.config.settings import load_config

    settings = load_config(args.config)
    ctx = assemble(settings)
    try:
        limit = max(1, args.limit)
        requeued = 0
        deleted = 0
        if args.requeue_failed:
            requeuer = getattr(ctx.state_store, "requeue_delivery_outbox", None)
            if callable(requeuer):
                requeued = requeuer(status="failed", limit=limit)
        if args.cleanup:
            cleaner = getattr(ctx.state_store, "cleanup_delivery_outbox", None)
            if callable(cleaner):
                deleted = cleaner(status=args.cleanup, limit=limit)

        status = "" if args.status == "all" else args.status
        lister = getattr(ctx.state_store, "list_delivery_outbox", None)
        summarizer = getattr(ctx.state_store, "get_delivery_outbox_summary", None)
        items = lister(status=status, limit=limit) if callable(lister) else []
        summary = summarizer() if callable(summarizer) else {}
        result = {
            "ok": True,
            "status": args.status,
            "limit": limit,
            "requeued_failed": requeued,
            "deleted": deleted,
            "summary": summary,
            "items": items,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    finally:
        ctx.state_store.close()


def _cmd_delivery_verify(args: argparse.Namespace) -> int:
    import asyncio
    import json

    from spider_gd.app import assemble, verify_delivery_receipt
    from spider_gd.config.settings import load_config

    settings = load_config(args.config)
    ctx = assemble(settings)
    try:
        result = asyncio.run(
            verify_delivery_receipt(
                ctx,
                task_id=args.task_id,
                receipt_path=args.receipt or None,
                verify_remote=not args.no_remote,
                download_remote=args.download_remote,
                max_remote_bytes=max(1, int(args.max_remote_mb)) * 1024 * 1024,
            )
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1
    finally:
        ctx.state_store.close()


def _cmd_wechat_webhook_stable_file(args: argparse.Namespace) -> int:
    import asyncio
    import json
    from pathlib import Path

    from spider_gd.app import (
        _build_reliable_delivery_hook,
        assemble,
        build_delivery_test_plan,
        delivery_outbox_receipt,
        write_delivery_receipt,
    )
    from spider_gd.config.settings import load_config

    settings = load_config(args.config)
    settings.delivery.enabled = True
    settings.delivery.backend = "wecom_webhook"
    settings.delivery.wecom_webhook = args.webhook.strip()
    settings.delivery.send_message = True
    settings.delivery.send_file = True
    if settings.delivery.title == "spider-gd 采集完成":
        settings.delivery.title = "spider-gd 稳定发送"

    ctx = assemble(settings)
    try:
        overrides = _apply_delivery_cli_overrides(
            ctx,
            args,
            wecom_webhook=args.webhook,
            enable_wecom=True,
        )
        file_path = Path(args.file).expanduser().resolve()
        task_id = args.task_id.strip() or _default_delivery_send_task_id(file_path)
        result = build_delivery_test_plan(ctx, file_path=file_path, dry_run=not args.send)
        result["command"] = "wechat-poc wecom-webhook"
        result["stable_delivery"] = True
        result["task_id"] = task_id
        result["message"] = args.message.strip()
        if overrides:
            result["overrides"] = _public_delivery_overrides(overrides)
        if not result.get("ok"):
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 1
        if not args.send:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0

        reliable_hook = _build_reliable_delivery_hook(ctx)
        summary = {
            "total": 1,
            "success": 1,
            "failed": 0,
            "source": "wechat_poc_wecom_webhook",
            "operator_message": args.message.strip(),
        }

        async def _send() -> None:
            await reliable_hook(task_id, str(file_path.parent), str(file_path), summary)

        ok = True
        receipt_path = None
        try:
            asyncio.run(_send())
        except Exception as exc:
            ok = False
            result["error"] = str(exc)

        reader = getattr(ctx.state_store, "get_delivery_outbox", None)
        if callable(reader):
            receipt = delivery_outbox_receipt(reader(task_id))
            result["outbox"] = receipt
            if receipt:
                receipt_path = write_delivery_receipt(
                    ctx.settings.storage.data_dir,
                    receipt,
                    command="wechat-poc wecom-webhook",
                    output_path=args.receipt.strip() or None,
                    extra={
                        "message": args.message.strip(),
                        "summary": summary,
                    },
                )
                result["receipt_path"] = str(receipt_path)
        if _should_verify_delivery(args):
            verification = _run_delivery_verification(
                ctx,
                task_id=task_id,
                receipt_path=receipt_path,
                args=args,
            )
            result["verification"] = verification
            if not verification.get("ok"):
                ok = False
        result["dry_run"] = False
        result["sent"] = ok
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if ok else 1
    finally:
        ctx.state_store.close()


_DELIVERY_SENDER_OVERRIDE_KEYS = {
    "deliveryBackend",
    "deliveryWecomWebhook",
    "deliveryServerchanSendKey",
    "deliveryPushplusToken",
}


async def _send_stable_delivery_text(settings, message: str) -> bool:
    backend = settings.backend.strip().lower().replace("-", "_")
    if backend == "wecom_webhook":
        if not settings.wecom_webhook.strip():
            raise RuntimeError("delivery.wecom_webhook 未配置")
        from spider_gd.gateway.alert.webhook_sender import send_wecom

        return await send_wecom(
            settings.wecom_webhook,
            message,
            title=settings.title,
            retry_attempts=settings.retry_attempts,
            retry_delay_sec=settings.retry_delay_sec,
        )
    if backend == "serverchan":
        if not settings.serverchan_send_key.strip():
            raise RuntimeError("delivery.serverchan_send_key 未配置")
        from spider_gd.gateway.alert.webhook_sender import send_serverchan

        return await send_serverchan(
            settings.serverchan_send_key,
            message,
            title=settings.title,
            channel=settings.serverchan_channel,
            openid=settings.serverchan_openid,
            retry_attempts=settings.retry_attempts,
            retry_delay_sec=settings.retry_delay_sec,
        )
    if backend == "pushplus":
        if not settings.pushplus_token.strip():
            raise RuntimeError("delivery.pushplus_token 未配置")
        from spider_gd.gateway.alert.webhook_sender import send_pushplus

        return await send_pushplus(
            settings.pushplus_token,
            message,
            title=settings.title,
            channel=settings.pushplus_channel,
            retry_attempts=settings.retry_attempts,
            retry_delay_sec=settings.retry_delay_sec,
        )
    raise RuntimeError(f"不支持的产物发送后端: {settings.backend}")


def _cmd_wechat_auto_stable(args: argparse.Namespace) -> int:
    import asyncio
    import json
    from pathlib import Path

    from spider_gd.app import (
        _build_reliable_delivery_hook,
        _effective_delivery_settings,
        assemble,
        build_delivery_test_plan,
        delivery_outbox_receipt,
        write_delivery_receipt,
    )
    from spider_gd.config.settings import load_config

    settings = load_config(args.config)
    ctx = assemble(settings)
    try:
        overrides = _apply_delivery_cli_overrides(ctx, args)
        file_path = Path(args.file).expanduser().resolve() if args.file else None
        plan = build_delivery_test_plan(ctx, file_path=file_path, dry_run=not args.send)
        effective = _effective_delivery_settings(ctx)
        result: dict[str, object] = {
            "ok": bool(plan.get("ok")) and bool(not args.send),
            "dry_run": not args.send,
            "platform": "stable",
            "command": "wechat-poc auto-stable",
            "stable_delivery": True,
            "auto_selected": True,
            "target": args.target.strip(),
            "message": args.message.strip(),
            "actions": [
                "auto 检测到稳定发送配置",
                f"使用稳定通道: {effective.backend}",
                "不控制本机微信窗口、不使用搜索结果",
            ],
            "plan": plan,
        }
        if args.target.strip():
            result["target_note"] = "稳定通道的接收方由 Webhook/Token 配置决定，--target 仅保留为操作备注"
        if overrides:
            result["overrides"] = _public_delivery_overrides(overrides)
        if not plan.get("ok"):
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 1
        if not args.send:
            result["ok"] = True
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0

        ok = True
        receipt_path = None
        if file_path is not None:
            task_id = args.task_id.strip() or _default_delivery_send_task_id(file_path)
            result["task_id"] = task_id
            reliable_hook = _build_reliable_delivery_hook(ctx)
            summary = {
                "total": 1,
                "success": 1,
                "failed": 0,
                "source": "wechat_poc_auto_stable",
                "operator_message": args.message.strip(),
                "target": args.target.strip(),
            }

            async def _send_file() -> None:
                await reliable_hook(task_id, str(file_path.parent), str(file_path), summary)

            try:
                asyncio.run(_send_file())
            except Exception as exc:
                ok = False
                result["error"] = str(exc)

            reader = getattr(ctx.state_store, "get_delivery_outbox", None)
            if callable(reader):
                receipt = delivery_outbox_receipt(reader(task_id))
                result["outbox"] = receipt
                if receipt:
                    receipt_path = write_delivery_receipt(
                        ctx.settings.storage.data_dir,
                        receipt,
                        command="wechat-poc auto-stable",
                        output_path=args.receipt.strip() or None,
                        extra={
                            "message": args.message.strip(),
                            "target": args.target.strip(),
                            "summary": summary,
                            "auto_selected": True,
                        },
                    )
                    result["receipt_path"] = str(receipt_path)
            if _should_verify_delivery(args):
                verification = _run_delivery_verification(
                    ctx,
                    task_id=task_id,
                    receipt_path=receipt_path,
                    args=args,
                )
                result["verification"] = verification
                if not verification.get("ok"):
                    ok = False
        else:
            try:
                ok = asyncio.run(_send_stable_delivery_text(effective, args.message.strip()))
            except Exception as exc:
                ok = False
                result["error"] = str(exc)

        result["dry_run"] = False
        result["sent"] = ok
        result["ok"] = ok
        if ok:
            result["message"] = "稳定通道发送完成"
        else:
            result["error_msg"] = result.get("error", "稳定通道发送失败")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if ok else 1
    finally:
        ctx.state_store.close()


def _wechat_auto_stable_available(args: argparse.Namespace) -> bool:
    from pathlib import Path

    from spider_gd.app import _effective_delivery_settings, assemble, build_delivery_test_plan
    from spider_gd.config.settings import load_config

    if not str(args.message or "").strip() and not str(args.file or "").strip():
        return False

    settings = load_config(args.config)
    ctx = assemble(settings)
    try:
        overrides = _apply_delivery_cli_overrides(ctx, args)
        effective = _effective_delivery_settings(ctx)
        explicit_sender_override = any(key in overrides for key in _DELIVERY_SENDER_OVERRIDE_KEYS)
        if not effective.enabled and not explicit_sender_override:
            return False
        file_path = Path(args.file).expanduser().resolve() if args.file else None
        plan = build_delivery_test_plan(ctx, file_path=file_path, dry_run=not args.send)
        return bool(plan.get("ok") or effective.enabled or explicit_sender_override)
    finally:
        ctx.state_store.close()


def _cmd_wechat_auto_missing_stable(args: argparse.Namespace) -> int:
    import json

    has_content = bool(str(args.message or "").strip() or str(args.file or "").strip())
    if has_content:
        error_msg = (
            "未检测到可用稳定发送配置，auto 不会退回本机微信窗口。"
            "请配置 --env-file / 环境变量 / 设置页里的企业微信、Server酱或 PushPlus 凭据；"
            "确实要使用桌面 PoC 时，显式指定 --backend powershell 或 --backend wxauto。"
        )
    else:
        error_msg = "缺少发送内容：请传 --message 或 --file"
    result = {
        "ok": False,
        "dry_run": not args.send,
        "platform": "stable",
        "command": "wechat-poc auto",
        "stable_delivery": False,
        "auto_selected": False,
        "target": args.target.strip(),
        "message": args.message.strip(),
        "file": str(args.file or "").strip(),
        "error_msg": error_msg,
        "recommendations": [
            'spider-gd delivery-doctor --env-file "/c/secure/spider-gd-delivery.env"',
            'spider-gd wechat-poc --env-file "/c/secure/spider-gd-delivery.env" --message "hello" --send',
            "spider-gd --config configs/mode_a_wired.yaml delivery-acceptance --env-file \"/c/secure/spider-gd-delivery.env\" --require-remote-url",
            "仅临时桌面 PoC：spider-gd wechat-poc --backend powershell --select-mode current --target \"文件传输助手\" --message \"hello\" --send",
        ],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1


def _cmd_wechat_poc(args: argparse.Namespace) -> int:
    import json
    from dataclasses import asdict

    from spider_gd.gateway.delivery.desktop_wechat import (
        DesktopWechatOptions,
        DesktopWechatResult,
        DesktopWechatSender,
    )

    sender = DesktopWechatSender()
    if args.backend == "auto" and _wechat_auto_stable_available(args):
        return _cmd_wechat_auto_stable(args)

    if args.backend == "wecom-webhook":
        webhook = _delivery_cli_value(args, "webhook", "SPIDER_GD_WECOM_WEBHOOK")
        args.webhook = webhook
        if not webhook:
            result = DesktopWechatResult(
                ok=False,
                dry_run=not args.send,
                platform="webhook",
                actions=["企业微信机器人稳定发送"],
                error_msg="缺少 --webhook 或 SPIDER_GD_WECOM_WEBHOOK",
            )
        elif not args.message and not args.file:
            result = DesktopWechatResult(
                ok=False,
                dry_run=not args.send,
                platform="webhook",
                actions=["企业微信机器人稳定发送"],
                error_msg="缺少发送内容：请传 --message 或 --file",
            )
        elif args.file:
            return _cmd_wechat_webhook_stable_file(args)
        elif not args.send:
            actions = ["企业微信机器人稳定发送"]
            if args.message:
                actions.append(f"发送 markdown 消息: {len(args.message)} 字符")
            if args.file:
                actions.append(f"发送文件: {args.file}")
            result = DesktopWechatResult(
                ok=True,
                dry_run=True,
                platform="webhook",
                actions=actions,
                message="dry-run：未调用企业微信接口；加 --send 才会真实发送",
            )
        else:
            import asyncio

            from spider_gd.gateway.alert.webhook_sender import send_wecom, send_wecom_file

            async def _send_wecom_webhook() -> bool:
                ok = True
                if args.message:
                    ok = await send_wecom(webhook, args.message, title="spider-gd")
                if args.file:
                    ok = await send_wecom_file(webhook, args.file) and ok
                return ok

            ok = asyncio.run(_send_wecom_webhook())
            result = DesktopWechatResult(
                ok=ok,
                dry_run=False,
                platform="webhook",
                actions=["企业微信机器人稳定发送"],
                message="企业微信机器人发送完成" if ok else "",
                error_msg="" if ok else "企业微信机器人发送失败",
            )
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
        return 0 if result.ok else 1

    if args.inspect:
        result = sender.inspect(
            app_name=args.app_name,
            max_depth=args.inspect_depth,
            name_filter=args.inspect_filter,
        )
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
        return 0 if result.ok else 1

    if args.bind:
        result = sender.bind(
            target=args.target,
            app_name=args.app_name,
            profile_path=args.profile_path,
            input_x_ratio=args.input_x_ratio,
            input_y_ratio=args.input_y_ratio,
            title_x_ratio=args.title_x_ratio,
            title_y_ratio=args.title_y_ratio,
            title_width_ratio=args.title_width_ratio,
            title_height_ratio=args.title_height_ratio,
        )
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
        return 0 if result.ok else 1

    if args.backend == "auto":
        return _cmd_wechat_auto_missing_stable(args)

    result = sender.send(
        DesktopWechatOptions(
            target=args.target,
            message=args.message,
            file_path=args.file,
            app_name=args.app_name,
            pause_sec=args.pause,
            backend=args.backend,
            select_mode=args.select_mode,
            search_down_steps=args.search_down,
            profile_path=args.profile_path,
            hash_threshold=args.hash_threshold,
            allow_unstable_search=args.allow_unstable_search,
            dry_run=not args.send,
        )
    )
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
    return 0 if result.ok else 1


def main(argv: Sequence[str] | None = None) -> int:
    import json

    parser = _build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    try:
        if str(getattr(args, "env_file", "") or "").strip():
            _delivery_env_file_values(args)
        return int(args.func(args))
    except DeliveryEnvFileError as exc:
        result = {
            "ok": False,
            "command": getattr(args, "command", ""),
            "env_file": str(getattr(args, "env_file", "") or ""),
            "error_msg": str(exc),
            "recommendations": [
                "确认 --env-file 路径存在且为 UTF-8 文本",
                "Git Bash 路径可写成 /c/secure/spider-gd-delivery.env",
                "也可以改用 SPIDER_GD_WECOM_WEBHOOK / SPIDER_GD_PUSHPLUS_TOKEN / SPIDER_GD_SERVERCHAN_SEND_KEY 环境变量",
            ],
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    sys.exit(main())
