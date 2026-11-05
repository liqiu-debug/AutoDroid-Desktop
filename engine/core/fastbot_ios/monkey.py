"""随机事件生成（W3C pointer actions）+ 区域黑名单。

坐标为 WDA 逻辑点（window/size）。每批 5 个事件一次 /actions（>5 不再变快）。
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

BATCH_SIZE = 5
# 默认黑名单（归一化坐标 x0,y0,x1,y1）：顶部状态栏（下拉进通知/控制中心）与底部 Home 条
DEFAULT_BLOCKED_REGIONS: Tuple[Tuple[float, float, float, float], ...] = (
    (0.0, 0.0, 1.0, 0.06),
    (0.0, 0.95, 1.0, 1.0),
)


@dataclass
class EventWeights:
    touch: int = 40
    motion: int = 30
    long_press: int = 10
    back: int = 15     # 左缘右滑（iOS 返回手势），对应 Android majornav
    syskeys: int = 5   # Home 键（离开 App，由守护拉回）

    def choices(self) -> Tuple[List[str], List[int]]:
        names = ["touch", "motion", "long_press", "back", "syskeys"]
        weights = [max(0, int(getattr(self, n))) for n in names]
        if not any(weights):
            weights = [1, 0, 0, 0, 0]
        return names, weights


@dataclass
class EventGenerator:
    width: float
    height: float
    weights: EventWeights = field(default_factory=EventWeights)
    blocked: Sequence[Tuple[float, float, float, float]] = DEFAULT_BLOCKED_REGIONS
    rng: random.Random = field(default_factory=random.Random)
    counts: Dict[str, int] = field(default_factory=dict)

    def _blocked(self, x: float, y: float) -> bool:
        nx, ny = x / self.width, y / self.height
        return any(x0 <= nx <= x1 and y0 <= ny <= y1 for x0, y0, x1, y1 in self.blocked)

    def _point(self) -> Tuple[float, float]:
        for _ in range(20):
            x = self.rng.uniform(4, self.width - 4)
            y = self.rng.uniform(4, self.height - 4)
            if not self._blocked(x, y):
                return round(x, 1), round(y, 1)
        return round(self.width / 2, 1), round(self.height / 2, 1)

    def next_kind(self) -> str:
        names, weights = self.weights.choices()
        return self.rng.choices(names, weights=weights, k=1)[0]

    def pointer_actions(self, kind: str) -> List[Dict[str, Any]]:
        if kind == "touch":
            x, y = self._point()
            return [{"type": "pointerMove", "duration": 0, "x": x, "y": y},
                    {"type": "pointerDown", "button": 0}, {"type": "pause", "duration": 30},
                    {"type": "pointerUp", "button": 0}]
        if kind == "long_press":
            x, y = self._point()
            return [{"type": "pointerMove", "duration": 0, "x": x, "y": y},
                    {"type": "pointerDown", "button": 0}, {"type": "pause", "duration": 800},
                    {"type": "pointerUp", "button": 0}]
        if kind == "back":
            y = self.rng.uniform(self.height * 0.3, self.height * 0.7)
            return [{"type": "pointerMove", "duration": 0, "x": 2, "y": round(y, 1)},
                    {"type": "pointerDown", "button": 0},
                    {"type": "pointerMove", "duration": 250, "x": round(self.width * 0.7, 1), "y": round(y, 1)},
                    {"type": "pointerUp", "button": 0}]
        x1, y1 = self._point()
        x2, y2 = self._point()
        return [{"type": "pointerMove", "duration": 0, "x": x1, "y": y1},
                {"type": "pointerDown", "button": 0},
                {"type": "pointerMove", "duration": 200, "x": x2, "y": y2},
                {"type": "pointerUp", "button": 0}]

    def batch(self, throttle_ms: int = 100) -> Tuple[Optional[List[Dict[str, Any]]], List[str]]:
        """返回 (W3C actions 载荷 | None, 本批事件种类)。syskeys 单独成批（由调用方按 Home）。"""
        kinds: List[str] = []
        acts: List[Dict[str, Any]] = []
        for _ in range(BATCH_SIZE):
            kind = self.next_kind()
            if kind == "syskeys":
                if not kinds:
                    kinds.append(kind)
                    self.counts[kind] = self.counts.get(kind, 0) + 1
                    return None, kinds
                break
            kinds.append(kind)
            self.counts[kind] = self.counts.get(kind, 0) + 1
            acts += self.pointer_actions(kind)
            acts.append({"type": "pause", "duration": max(0, int(throttle_ms))})
        payload = [{"type": "pointer", "id": "finger1", "parameters": {"pointerType": "touch"}, "actions": acts}]
        return payload, kinds
