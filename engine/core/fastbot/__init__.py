# 来源：vendor 自 AutoDroid backend/fastbot/（adb/monkey/logcat/perf_monitor/summary）。
"""Fastbot 本地轻量档采集（P3-2 边界）。

vendor 自平台 backend/fastbot/ 的自包含模块（仅改 import 路径与资源目录）：
- adb          ADB 协程工具
- monkey       Fastbot 资源部署与 Monkey 命令拼接
- logcat       崩溃/ANR logcat 监控
- perf_monitor CPU/内存采集
- summary      报告汇总（去掉 perfetto/jank 依赖，字段形状保持）

未 vendor（重量级，见 open_issues）：perfetto 取证/分析、framestats 卡顿帧监控、
本地回放录像、startup/fluency 专项、jank_analyzer/jank_ai。
"""
