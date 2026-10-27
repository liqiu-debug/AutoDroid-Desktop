"""WebSocket 反代端到端 smoke：真实 uvicorn 引擎进程 + 真实 ws 客户端。

流程：起 echo upstream（websockets.serve）→ 起 engine 子进程 → 客户端连
ws://127.0.0.1:<engine>/ws/echo 断言文本/二进制往返 → 校验 501 HTTP 分支。
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parent.parent


async def main() -> int:
    async def handler(ws, *args):
        async for message in ws:
            await ws.send(message)

    upstream = await websockets.serve(handler, "127.0.0.1", 0)
    upstream_port = upstream.sockets[0].getsockname()[1]

    env = dict(os.environ)
    env["AUTODROID_USER_DATA"] = tempfile.mkdtemp(prefix="autodroid-ws-smoke-")
    env["AUTODROID_CLIENT_VERSION"] = "ws-smoke"
    proc = subprocess.Popen(
        [sys.executable, "-m", "engine"],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        port = None
        for line in proc.stdout:
            line = line.strip()
            if line.startswith("AUTODROID_ENGINE_PORT="):
                port = int(line.split("=", 1)[1])
                break
        if port is None:
            print("FAIL: 引擎未输出 AUTODROID_ENGINE_PORT")
            return 1

        # 设置 upstream（借 auth/token 归一化落盘，再由引擎内存恢复；
        # echo upstream 无 /auth/token，转发失败与否不影响 upstream 记录）
        import urllib.error
        import urllib.request

        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/auth/token",
            data=b"username=u&password=p",
            headers={
                "X-AutoDroid-Server": f"http://127.0.0.1:{upstream_port}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=10)
        except (urllib.error.HTTPError, urllib.error.URLError, OSError):
            pass

        async with websockets.connect(f"ws://127.0.0.1:{port}/ws/echo") as ws:
            await ws.send("hello-ws-smoke")
            got = await asyncio.wait_for(ws.recv(), timeout=10)
            if got != "hello-ws-smoke":
                print(f"FAIL: 文本往返不一致 -> {got!r}")
                return 1
            await ws.send(b"\x00\x01binary-frame")
            got_b = await asyncio.wait_for(ws.recv(), timeout=10)
            if got_b != b"\x00\x01binary-frame":
                print(f"FAIL: 二进制往返不一致 -> {got_b!r}")
                return 1
        print("OK: /ws/echo 文本 + 二进制往返通过真实引擎")

        # 非升级 HTTP 访问 /ws/* → 501
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/ws/echo", timeout=10)
            print("FAIL: /ws/* 普通 HTTP 未返回 501")
            return 1
        except urllib.error.HTTPError as exc:
            if exc.code != 501:
                print(f"FAIL: /ws/* 普通 HTTP 返回 {exc.code}（期望 501）")
                return 1
        print("OK: /ws/* 普通 HTTP 返回 501")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        upstream.close()
        await upstream.wait_closed()

    print("WS SMOKE PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
