"""引擎入口：python -m engine

只绑定 127.0.0.1；端口缺省随机（env AUTODROID_ENGINE_PORT 可指定）。
启动后在 stdout 打印 AUTODROID_ENGINE_PORT=<n>，供 Electron 主进程解析。
"""
from __future__ import annotations

import logging
import os
import socket
import sys

logger = logging.getLogger("engine")


def main() -> int:
    from engine.app import app

    try:
        import uvicorn
    except ImportError:
        print("uvicorn 未安装，请先 pip install -r requirements.txt", file=sys.stderr)
        return 1

    host = "127.0.0.1"
    requested = os.environ.get("AUTODROID_ENGINE_PORT", "").strip()
    port = int(requested) if requested.isdigit() else 0

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
    except OSError as exc:
        print(f"绑定 {host}:{port} 失败: {exc}", file=sys.stderr)
        return 1
    sock.listen(128)
    actual_port = sock.getsockname()[1]

    # 主进程解析这一行后才加载前端
    print(f"AUTODROID_ENGINE_PORT={actual_port}", flush=True)

    logging.basicConfig(
        level=os.environ.get("AUTODROID_ENGINE_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = uvicorn.Config(app, host=host, port=actual_port, log_level="info", ws="auto")
    server = uvicorn.Server(config)
    try:
        server.run(sockets=[sock])
    finally:
        sock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
