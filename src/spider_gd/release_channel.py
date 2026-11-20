"""显式发布渠道。冻结进程只读取编译进包的身份，不接受环境切换。"""
from __future__ import annotations

import json
import os
import re
import sys
from importlib.resources import files
from pathlib import Path


def channel_config(channel: str = 'stable') -> dict[str, object]:
    if channel not in ('stable', 'beta'):
        raise ValueError(f'Invalid channel: {channel}')
    channels = json.loads(files('spider_gd').joinpath('release-channels.json').read_text(encoding='utf-8'))
    return {**channels[channel], 'channel': channel}


def build_release(base_version: str, channel: str = 'stable', beta_number: int | str | None = None) -> dict[str, object]:
    identity = channel_config(channel)
    if not re.fullmatch(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', base_version):
        raise ValueError('VERSION must be x.y.z')
    has_number = beta_number is not None and beta_number != ''
    if channel == 'beta' and (not has_number or not re.fullmatch(r'[1-9][0-9]*', str(beta_number))):
        raise ValueError('BetaNumber must be a positive integer for beta channel')
    if channel == 'stable' and has_number:
        raise ValueError('BetaNumber is not allowed for stable channel')
    version = f'{base_version}-beta.{beta_number}' if channel == 'beta' else base_version
    return {**identity, 'version': version}


def runtime_release() -> dict[str, object]:
    from spider_gd import __version__
    from spider_gd.build_stamp import RELEASE_CHANNEL, RELEASE_VERSION
    frozen = bool(getattr(sys, 'frozen', False))
    channel = RELEASE_CHANNEL if frozen else os.environ.get('SPIDER_GD_CHANNEL', 'stable')
    return {**channel_config(channel), 'version': RELEASE_VERSION if frozen else __version__}


def default_beta_home() -> Path:
    if sys.platform == 'win32':
        base = Path(os.environ.get('APPDATA') or Path.home() / 'AppData' / 'Roaming')
    elif sys.platform == 'darwin':
        base = Path.home() / 'Library' / 'Application Support'
    else:
        base = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config')
    return base / str(channel_config('beta')['userDataDirName'])
