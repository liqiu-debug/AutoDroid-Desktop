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
