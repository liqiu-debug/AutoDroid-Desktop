"""AutoDroid 桌面端本地引擎（FastAPI 网关）。

只绑定 127.0.0.1：对前端暴露 /api/* 与 /ws/*，按 X-AutoDroid-Server 归一化后的
upstream 反向代理到平台服务；并拦截 /api/devices/sync 做本机 adb 设备上报。
"""
