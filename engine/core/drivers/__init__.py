"""AutoDroid 跨端驱动模块（桌面端 vendor）。

来源：AutoDroid backend/drivers/（原样复制，仅改写内部导入为 engine.core.*）。
剥离点：iOS 驱动依赖 facebook-wda，桌面端可后置；依赖缺失时仅导出 Android 侧。

提供 Android/iOS 双端统一驱动接口，支持策略模式和工厂模式。
"""
from .base_driver import BaseDriver
from .android_driver import AndroidDriver
from .cross_platform_runner import TestCaseRunner, DriverFactory

try:
    from .ios_driver import IOSDriver
except Exception:  # pragma: no cover - facebook-wda 未安装
    IOSDriver = None  # type: ignore[assignment]

__all__ = [
    "BaseDriver",
    "AndroidDriver",
    "IOSDriver",
    "TestCaseRunner",
    "DriverFactory",
]
