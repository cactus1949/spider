"""控制接口在首选端口被占用时改用空闲端口。"""

from __future__ import annotations

import socket


def choose_listen_port(host: str, preferred: int) -> int:
    """优先使用指定端口，绑不上时让系统分配一个空闲端口。

    占用首选端口的程序不会被结束。调用方要用返回的端口启动服务，
    并把它告诉前端。

    Args:
        host: 监听地址，例如 ``127.0.0.1``。
        preferred: 希望使用的端口。常用值是 8800。

    Returns:
        int: 实际可以绑定的端口。首选端口空闲时等于 ``preferred``。

    Raises:
        OSError: 连系统分配的端口都绑不上。

    Examples:
        >>> import socket
        >>> held = socket.socket()
        >>> held.bind(("127.0.0.1", 0))
        >>> busy = held.getsockname()[1]
        >>> chosen = choose_listen_port("127.0.0.1", busy)
        >>> chosen != busy and chosen > 0
        True
        >>> held.close()
        >>> probe = socket.socket()
        >>> probe.bind(("127.0.0.1", 0))
        >>> free = probe.getsockname()[1]
        >>> probe.close()
        >>> choose_listen_port("127.0.0.1", free) == free
        True
    """
    if _can_bind(host, preferred):
        return preferred
    fallback = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        fallback.bind((host, 0))
        return int(fallback.getsockname()[1])
    finally:
        fallback.close()


def _can_bind(host: str, port: int) -> bool:
    """试绑一次，判断端口现在能不能用。

    Args:
        host: 监听地址。
        port: 要试的端口。

    Returns:
        bool: 能绑上为 True。试完会立刻关闭套接字。

    Examples:
        >>> _can_bind("127.0.0.1", 1) in (True, False)
        True
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((host, port))
    except OSError:
        return False
    else:
        return True
    finally:
        probe.close()
