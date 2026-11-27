"""Scrcpy 子进程管道排空与超时强杀单元测试。"""
from __future__ import annotations

import subprocess
import sys

from engine.core.device_stream.manager import (
    _read_process_stderr_tail,
    _start_process_output_drainer,
    _terminate_scrcpy_process,
)


def test_drainer_prevents_pipe_deadlock_on_large_output_and_keeps_stderr_tail():
    script = (
        "import sys; "
        "sys.stdout.buffer.write(b'O' * 300000); sys.stdout.buffer.flush(); "
        "sys.stderr.buffer.write(b'E' * 300000 + b'TAIL_MARKER\\n'); sys.stderr.buffer.flush(); "
        "sys.exit(2)"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        stderr_tail = _start_process_output_drainer(proc, "serial-drain")
        rc = proc.wait(timeout=5)
        assert rc == 2
        tail_text = _read_process_stderr_tail(proc, stderr_tail)
        assert "TAIL_MARKER" in tail_text
        assert len(tail_text.encode("utf-8")) <= 8192
    finally:
        _terminate_scrcpy_process(proc)


def test_terminate_scrcpy_process_escalates_to_kill_when_sigterm_ignored():
    script = (
        "import signal, sys, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "sys.stdout.write('ready\\n'); sys.stdout.flush(); "
        "time.sleep(30)"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        ready = proc.stdout.readline()
        assert b"ready" in ready
        _terminate_scrcpy_process(proc)
        assert proc.poll() is not None
        assert proc.stdout.closed
        assert proc.stderr.closed
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)
