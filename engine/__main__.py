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

# 文件日志滚动策略：桌面端 stderr 由 Electron 主进程转手丢掉（process.stderr.write），
# 出问题时现场没了；落盘一份才能事后查「启动异常回放录制失败」这类 warning
ENGINE_LOG_MAX_BYTES = 2 * 1024 * 1024
ENGINE_LOG_BACKUP_COUNT = 5


def _setup_file_logging() -> logging.Handler | None:
    """日志同时落盘 userData/logs/engine.log（与 stderr 并存）。

    写不进去只告警、返回 None：日志设施坏了也不能挡住引擎启动。
    """
    try:
        from logging.handlers import RotatingFileHandler

        from engine import config_store

        log_dir = config_store.user_data_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            log_dir / "engine.log",
            maxBytes=ENGINE_LOG_MAX_BYTES,
            backupCount=ENGINE_LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logging.getLogger().addHandler(handler)
        return handler
    except Exception as exc:
        print(f"引擎日志落盘不可用（仅 stderr）: {exc}", file=sys.stderr)
        return None


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
    _setup_file_logging()
    config = uvicorn.Config(app, host=host, port=actual_port, log_level="info", ws="auto")
    server = uvicorn.Server(config)
    try:
        server.run(sockets=[sock])
    finally:
        sock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
