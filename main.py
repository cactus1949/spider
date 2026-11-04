"""统一入口 shim：将 ``src`` 加入路径后委托给 :mod:`spider_gd.__main__`。

支持文档中的运行方式：``python main.py --config configs/mode_d_offline.yaml``。
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).parent / "src"
if _SRC.exists() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from spider_gd.__main__ import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
