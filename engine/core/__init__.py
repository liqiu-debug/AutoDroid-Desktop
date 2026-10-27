"""engine.core：从 AutoDroid 平台 vendor 的可移植执行/录制核心。

来源仓库：AutoDroid（backend/ 目录）。本目录为只读 vendor 副本 + 少量剥离改动，
不反向修改平台原文件。剥离点一览：

- backend/utils.py + backend/utils/*          → engine/core/utils/*（element_tools.py）
  （上游 utils.py 与 utils/ 包同级共存，靠 importlib 旁路加载；此处改为常规包内导入）
- backend/step_contract.py                    → engine/core/step_contract.py
- backend/locator_resolution.py               → engine/core/locator_resolution.py
- backend/execution_errors.py                 → engine/core/execution_errors.py
- backend/ocr_service.py                      → engine/core/ocr_service.py
- backend/device_transport_metrics.py         → engine/core/device_transport_metrics.py
- backend/drivers/*                           → engine/core/drivers/*（iOS 驱动改为可选导入）
- backend/api/recording.py 的服务逻辑          → engine/core/recording_service.py
  （DB 会话改为入参注入；设备平台判定改由本机 adb/请求参数决定）
- backend/cross_platform_execution.py 的 DB 依赖（步骤/变量/应用映射）
  → engine/core/case_source.py（改从云端 API 拉取）+ 入参注入

导入路径改写：`from backend.X` → `from engine.core.X`，其余代码语义不变。
"""
