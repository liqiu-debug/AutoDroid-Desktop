import pytest
"""测试路径：保证 `engine` 包可从仓库根导入。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _isolate_ios_scan(monkeypatch):
    """测试不触碰本机真实 iOS 设备（libimobiledevice 扫描）。"""
    monkeypatch.setenv("AUTODROID_DISABLE_IOS_SCAN", "1")


@pytest.fixture(autouse=True)
def _reset_device_busy():
    """本机占用登记是进程内单例：mock 掉执行线程的用例不会走到释放，逐用例清空。"""
    from engine.core import device_busy

    device_busy.reset_for_tests()
    yield
    device_busy.reset_for_tests()
