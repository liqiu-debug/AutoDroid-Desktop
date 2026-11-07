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


# 可点击的控件类型（WDA /source?format=json 的 type 字段，iOS 27 不带 XCUIElementType 前缀）
INTERACTIVE_TYPES = frozenset({
    "Button", "Key", "Cell", "Switch", "Link", "SearchField", "TextField", "SecureTextField",
    "Slider", "SegmentedControl", "Tab", "TabBar", "MenuItem", "Stepper", "PickerWheel", "Toggle",
})
# 点击 / 长按落在真实控件上的概率（其余随机坐标，保留探索空白区域的可能）
TARGETED_TAP_RATIO = 0.7


@dataclass
class EventWeights:
    """智能默认以点击为主：随机坐标点击大多落在空白处、肉眼看不出效果，而滑动
    每次都有明显的滚动——旧配比（点击 40 / 滑动 30 / 返回手势 15）实际观感是
    「大部分在滑」。"""

    touch: int = 60
    motion: int = 20
    long_press: int = 5
    back: int = 10     # 左缘右滑（iOS 返回手势），对应 Android majornav
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
    # 当前页面可点击控件的中心点（逻辑坐标），由主循环定期从 /source 刷新
    targets: List[Tuple[float, float]] = field(default_factory=list)
    targeted_taps: int = 0

    def set_targets_from_source(self, source: Any) -> int:
        self.targets = extract_tap_targets(source, self.width, self.height, self._blocked)
        return len(self.targets)

    def _tap_point(self) -> Tuple[float, float]:
        if self.targets and self.rng.random() < TARGETED_TAP_RATIO:
            self.targeted_taps += 1
            return self.rng.choice(self.targets)
        return self._point()

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
            x, y = self._tap_point()
            return [{"type": "pointerMove", "duration": 0, "x": x, "y": y},
                    {"type": "pointerDown", "button": 0}, {"type": "pause", "duration": 30},
                    {"type": "pointerUp", "button": 0}]
        if kind == "long_press":
            x, y = self._tap_point()
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


def extract_tap_targets(source: Any, width: float, height: float, blocked=None) -> List[Tuple[float, float]]:
    """从 WDA JSON 层级取可用、在屏内的控件中心点（去重，最多 200 个）。

    层级默认不带 isVisible（见 WdaSession.source_json）；带了就过滤掉不可见的。
    """
    found: List[Tuple[float, float]] = []
    seen = set()

    def visit(node: Any) -> None:
        if not isinstance(node, dict) or len(found) >= 200:
            return
        kind = str(node.get("type") or "")
        if kind.startswith("XCUIElementType"):
            kind = kind[len("XCUIElementType"):]
        rect = node.get("rect") or {}
        enabled = str(node.get("isEnabled", "1")) not in ("0", "false", "False")
        visible = str(node.get("isVisible", "1")) not in ("0", "false", "False")
        if kind in INTERACTIVE_TYPES and enabled and visible:
            try:
                x = float(rect.get("x", 0)) + float(rect.get("width", 0)) / 2
                y = float(rect.get("y", 0)) + float(rect.get("height", 0)) / 2
                w, h = float(rect.get("width", 0)), float(rect.get("height", 0))
            except (TypeError, ValueError):
                w = h = 0
            if w > 2 and h > 2 and 0 < x < width and 0 < y < height and not (blocked and blocked(x, y)):
                key = (round(x), round(y))
                if key not in seen:
                    seen.add(key)
                    found.append((round(x, 1), round(y, 1)))
        for child in node.get("children") or []:
            visit(child)

    visit(source)
    return found
