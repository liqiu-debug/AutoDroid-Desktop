# engine/core/utils/__init__.py
# 来源：AutoDroid backend/utils/__init__.py（原样 re-export backend/utils.py）。
# 剥离点：上游用 importlib 旁路加载同级 utils.py（utils.py 与 utils/ 包共存的历史布局）；
# 本副本改为常规同包模块导入（element_tools.py），公开 API 与上游保持一致。
from .element_tools import (
    calculate_element_from_coordinates,
    evaluate_page_text_assertion,
    parse_bounds,
)

__all__ = [
    "parse_bounds",
    "calculate_element_from_coordinates",
    "evaluate_page_text_assertion",
]
