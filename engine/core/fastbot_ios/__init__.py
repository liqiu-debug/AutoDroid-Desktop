"""iOS 稳定性测试（主机驱动，对标 Android Fastbot 轻量档）。

链路（2026-11-05 iOS 27.3 真机验证）：
- 发现：libimobiledevice（idevice_id / ideviceinfo）枚举 USB 设备，serial = 裸 UDID（与平台 ios_scanner 一致；``ios:`` 前缀是 App 形态）；
- 事件：WebDriverAgent W3C ``/actions`` 每批 5 个（≈2.35 ev/s），批间 ``/wda/apps/state`` 守护；
- 崩溃：apps/state 掉到 ≤1 判定退出 → lockdown 拉 ``.ips``（idevicecrashreport -k）补堆栈；
- 回放：WDA MJPEG（设备 9100）滚动缓冲，崩溃时截前 30s + 后 5s 写 mp4；
- 守护：WDA 连续失败自动重建会话 / 重启 iproxy / 重新拉起 xcodebuild。
"""
