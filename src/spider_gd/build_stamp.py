"""冻结进当前进程的构建身份。

运行中的进程只读这个常量。升级覆盖磁盘上的新文件之后，旧进程不能靠
再去读那个文件来假装自己是新构建。
"""

from __future__ import annotations

BUILD_ID = "source"
RELEASE_CHANNEL = "stable"
RELEASE_VERSION = ""
