"""桌面端 ADB 文件传输超时与终止测试。"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from engine.core.fastbot import adb as fastbot_adb


class _HangingProcess:
    def __init__(self) -> None:
        self.returncode = None
        self.terminated = False

    async def communicate(self):
        await asyncio.sleep(60)
        return b"", b""

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.returncode = -9

    async def wait(self):
        return self.returncode


@pytest.mark.asyncio
async def test_desktop_adb_push_and_pull_timeout():
    push_proc = _HangingProcess()
    with patch("engine.core.fastbot.adb.asyncio.create_subprocess_exec", return_value=push_proc):
        with pytest.raises(TimeoutError):
            await fastbot_adb._adb_push("serial-1", "/tmp/a", "/sdcard/a", timeout=0.05)
    assert push_proc.terminated is True

    pull_proc = _HangingProcess()
    with patch("engine.core.fastbot.adb.asyncio.create_subprocess_exec", return_value=pull_proc):
        with pytest.raises(TimeoutError):
            await fastbot_adb._adb_pull("serial-1", "/sdcard/a", "/tmp/a", timeout=0.05)
    assert pull_proc.terminated is True
