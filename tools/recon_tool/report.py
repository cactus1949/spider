"""人可读侦察报告生成（纯函数，可测试）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any


def build_summary_report(
    analysis: dict[str, Any],
    auth: dict[str, Any],
    *,
    platform: str = "pm.gd.csg.cn",
    operator: str = "-",
    network: dict[str, Any] | None = None,
) -> str:
    """根据 HAR 分析与认证信息生成 ``summary_report.md`` 文本。"""
    network = network or {}
    lines: list[str] = []
    lines.append("# 现场侦察报告")
    lines.append(f"- 日期：{datetime.now():%Y-%m-%d %H:%M}")
    lines.append(f"- 平台：{platform}")
    lines.append(f"- 侦察人：{operator}")
    lines.append("")

    lines.append("## 一、网络环境")
    if network:
        for key, value in network.items():
            lines.append(f"- {key}：{value}")
    else:
        lines.append("- （未提供网络侦察数据）")
    lines.append("")

    exports = analysis.get("exports", [])
    deduped = analysis.get("deduped", {})
    lines.append(f"## 二、发现的 API 端点（去重 {len(deduped)} 个，导出类 {len(exports)} 个）")
    lines.append("| Method | Path | 状态 | 数据格式 | 分类 |")
    lines.append("|--------|------|------|---------|------|")
    for key, group in deduped.items():
        sample = group[0]
        method, path = key.split(" ", 1)
        lines.append(
            f"| {method} | {path} | {sample['status']} | "
            f"{sample['content_type'] or '-'} | {sample['category']} |"
        )
    lines.append("")

    lines.append("## 三、认证机制")
    lines.append(f"- Bearer Token：发现 {len(auth.get('bearer_tokens', []))} 个")
    lines.append(f"- Cookie 键：{', '.join(auth.get('cookies', {}).keys()) or '无'}")
    special = auth.get("special_params", {})
    if special:
        for name, values in special.items():
            lines.append(f"- 特殊参数 `{name}`：观察到 {len(values)} 个不同值（疑似动态生成）")
    else:
        lines.append("- 特殊参数：未发现 HdKGccbL 等")
    lines.append("")

    lines.append("## 四、待确认事项")
    lines.append("- [ ] HdKGccbL 生成算法 / 是否每页独立")
    lines.append("- [ ] 验证码类型（数字 / 字母数字 / 滑块）")
    lines.append("- [ ] Token 过期时间与刷新机制")
    lines.append("- [ ] 是否所有数据页面都有导出 API")
    lines.append("")

    return "\n".join(lines)
