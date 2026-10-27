"""PyInstaller 单文件入口（打包收尾）：等价 `python -m engine`。

产物（release-engine/autodroid-engine[.exe]）作为 electron-builder extraResources
（engine-bin/）分发；主进程优先拉起内置引擎，缺省回退 `python -m engine`（开发态）。
只绑 127.0.0.1、stdout 输出 AUTODROID_ENGINE_PORT=<n> 的契约不变。
"""
from __future__ import annotations

import sys

from engine.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
