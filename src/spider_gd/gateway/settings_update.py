"""应用设置的部分更新。

界面只提交改过的键。这里丢掉掩码占位符，避免把 ``******`` 写回数据库盖掉真密码。
没提交的键保持原值。
"""

from __future__ import annotations

MASK = "******"

# 这些键出现掩码时表示「沿用已保存的值」，不能当新密码写入。
SECRET_KEYS = frozenset({
    "certPassword",
    "accountPassword",
    "wifiPassword",
    "workshopUploadToken",
    "deliveryWecomWebhook",
    "deliveryServerchanSendKey",
    "deliveryPushplusToken",
    "deliveryRemoteUploadToken",
    "deliveryDesktopWechatTarget",
    "webhook",
    "alertWecomBackupWebhook",
})


def filter_settings_update(incoming: dict[str, object]) -> dict[str, str]:
    """只保留本次提交的键，并去掉敏感字段上的掩码。

    空字符串会保留，用来表达用户主动清空。值为 ``******`` 的敏感键直接丢弃。

    Args:
        incoming (dict[str, object]): 请求体里的设置。非字符串会转成字符串。

    Returns:
        dict[str, str]: 可以写入数据库的键值。未出现的键不在结果里。

    Examples:
        >>> filter_settings_update({"siteUrl": "https://pm.gd.csg.cn", "certPassword": "******"})
        {'siteUrl': 'https://pm.gd.csg.cn'}
        >>> filter_settings_update({"certPassword": ""})
        {'certPassword': ''}
    """
    kept: dict[str, str] = {}
    for key, value in incoming.items():
        name = str(key)
        text = "" if value is None else str(value)
        if name in SECRET_KEYS and text.strip() == MASK:
            continue
        kept[name] = text
    return kept
