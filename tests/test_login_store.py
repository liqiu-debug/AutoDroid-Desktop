"""login_store 单测（Node 脚本 + mock electron）经 pytest 驱动。

覆盖 loggedOut 持久化语义：markLoggedOut 置 loggedOut+清 autoLogin、
writeSavedLogin 重置 loggedOut、无记录 no-op。
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node 运行 login_store 单测")
def test_login_store_check() -> None:
    result = subprocess.run(
        ["node", str(ROOT / "tests" / "login_store_check.js")],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "LOGIN STORE CHECK PASSED" in result.stdout
