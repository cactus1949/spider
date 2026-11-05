"""把敏感字符串加密后再写入 SQLite。

上传配置里的敏感字段和 WiFi 密码不能明文落盘。这里只用标准库：主密钥派生出
加密密钥和认证密钥，用 HMAC-SHA256 生成密钥流做异或，再用 HMAC 校验密文。
密钥单独放在数据库旁边的 ``upload_secret.key``，不写进设置 JSON。
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import os
import stat
from pathlib import Path

_VERSION = b"\x01"
_NONCE_LEN = 16
_MAC_LEN = 32


class SecretBoxError(ValueError):
    """密文损坏或密钥不匹配。"""


class SecretBox:
    """用一把 32 字节主密钥加密短字符串。

    Args:
        key: 32 字节主密钥。

    Raises:
        ValueError: 密钥长度不是 32 字节。

    Examples:
        >>> box = SecretBox(b"k" * 32)
        >>> box.decrypt(box.encrypt("token"))
        'token'
    """

    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("上传加密密钥必须是 32 字节")
        self._enc_key = hmac.new(key, b"spider-gd-upload-enc", hashlib.sha256).digest()
        self._mac_key = hmac.new(key, b"spider-gd-upload-mac", hashlib.sha256).digest()

    def encrypt(self, plaintext: str) -> str:
        """加密一段文本。空字符串原样返回空串，不产生密文。

        Args:
            plaintext: 要加密的 token 或密码。

        Returns:
            str: Base64 密文；空输入返回 ``""``。

        Examples:
            >>> SecretBox(b"k" * 32).encrypt("")
            ''
        """
        text = plaintext or ""
        if text == "":
            return ""
        nonce = os.urandom(_NONCE_LEN)
        raw = text.encode("utf-8")
        stream = _keystream(self._enc_key, nonce, len(raw))
        cipher = bytes(left ^ right for left, right in zip(raw, stream, strict=True))
        mac = hmac.new(self._mac_key, _VERSION + nonce + cipher, hashlib.sha256).digest()
        return base64.b64encode(_VERSION + nonce + mac + cipher).decode("ascii")

    def decrypt(self, token: str) -> str:
        """解密 ``encrypt`` 的结果。

        Args:
            token: Base64 密文；空串表示没有秘密。

        Returns:
            str: 原文。

        Raises:
            SecretBoxError: 密文格式不对或认证失败。

        Examples:
            >>> box = SecretBox(b"k" * 32)
            >>> box.decrypt(box.encrypt("密码"))
            '密码'
        """
        text = (token or "").strip()
        if text == "":
            return ""
        try:
            blob = base64.b64decode(text.encode("ascii"), validate=True)
        except Exception as exc:
            raise SecretBoxError("密文不是合法 Base64") from exc
        if len(blob) < 1 + _NONCE_LEN + _MAC_LEN or blob[:1] != _VERSION:
            raise SecretBoxError("密文版本无法识别")
        nonce = blob[1 : 1 + _NONCE_LEN]
        mac = blob[1 + _NONCE_LEN : 1 + _NONCE_LEN + _MAC_LEN]
        cipher = blob[1 + _NONCE_LEN + _MAC_LEN :]
        expected = hmac.new(self._mac_key, _VERSION + nonce + cipher, hashlib.sha256).digest()
        if not hmac.compare_digest(mac, expected):
            raise SecretBoxError("密文认证失败")
        stream = _keystream(self._enc_key, nonce, len(cipher))
        raw = bytes(left ^ right for left, right in zip(cipher, stream, strict=True))
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SecretBoxError("密文无法按 UTF-8 还原") from exc


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    """按计数器生成 HMAC-SHA256 密钥流。

    Args:
        key: 加密子密钥。
        nonce: 每次加密的随机数。
        length: 需要的字节数。

    Returns:
        bytes: 长度恰好为 ``length`` 的密钥流。

    Examples:
        >>> len(_keystream(b"k" * 32, b"n" * 16, 3))
        3
    """
    chunks: list[bytes] = []
    counter = 0
    produced = 0
    while produced < length:
        block = hmac.new(key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest()
        chunks.append(block)
        produced += len(block)
        counter += 1
    return b"".join(chunks)[:length]


def load_or_create_key(path: Path) -> bytes:
    """读取密钥文件；不存在就生成 32 字节并尽量收紧权限。

    Args:
        path: 密钥文件路径。

    Returns:
        bytes: 32 字节主密钥。

    Raises:
        SecretBoxError: 已有文件的长度不是 32 字节。

    Examples:
        >>> isinstance(load_or_create_key(Path("/tmp/spider-gd-doctest-upload.key")), bytes)
        True
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        key = path.read_bytes()
        if len(key) != 32:
            raise SecretBoxError(f"上传加密密钥长度异常: {path}")
        return key
    key = os.urandom(32)
    path.write_bytes(key)
    with contextlib.suppress(OSError):
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return key


def key_path_for_store(store: object) -> Path:
    """密钥放在状态库同目录，内存库则放到临时目录并跟库对象绑定。

    Args:
        store: ``TaskStateStore`` 或带 ``_db_path`` 的对象。

    Returns:
        Path: ``upload_secret.key`` 的路径。

    Examples:
        >>> key_path_for_store(type("S", (), {"_db_path": "/data/spider.db"})()).name
        'upload_secret.key'
    """
    raw = str(getattr(store, "_db_path", "") or "")
    if raw and raw != ":memory:":
        return Path(raw).expanduser().resolve().parent / "upload_secret.key"
    return Path(os.environ.get("TMPDIR") or "/tmp") / f"spider-gd-upload-{id(store)}.key"


def box_for_store(store: object, key_path: Path | None = None) -> SecretBox:
    """打开这个状态库对应的加密盒。

    Args:
        store: 状态库。
        key_path: 测试可指定密钥路径；默认跟库文件放一起。

    Returns:
        SecretBox: 可加密、解密的盒子。

    Examples:
        >>> box = box_for_store(type("S", (), {"_db_path": ":memory:"})())
        >>> box.decrypt(box.encrypt("a"))
        'a'
    """
    path = key_path or key_path_for_store(store)
    return SecretBox(load_or_create_key(path))
