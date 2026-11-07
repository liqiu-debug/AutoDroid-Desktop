"""iPhone/iPad 机型标识 → 市场名（来源：AutoDroid backend/ios_scanner.py IPHONE_MODEL_MAP，补 iPhone 16e / 17 系列）。

优先用 xcrun devicectl 报告的 marketingName（随 Xcode 更新，覆盖新机型），本表兜底。
"""
from typing import Dict

IPHONE_MODEL_MAP: Dict[str, str] = {
    # iPhone 8 / X 系列
    "iPhone10,1": "iPhone 8",
    "iPhone10,2": "iPhone 8 Plus",
    "iPhone10,3": "iPhone X",
    "iPhone10,4": "iPhone 8",
    "iPhone10,5": "iPhone 8 Plus",
    "iPhone10,6": "iPhone X",
    # iPhone XS / XR 系列
    "iPhone11,2": "iPhone XS",
    "iPhone11,4": "iPhone XS Max",
    "iPhone11,6": "iPhone XS Max",
    "iPhone11,8": "iPhone XR",
    # iPhone 11 系列
    "iPhone12,1": "iPhone 11",
    "iPhone12,3": "iPhone 11 Pro",
    "iPhone12,5": "iPhone 11 Pro Max",
    "iPhone12,8": "iPhone SE (2nd)",
    # iPhone 12 系列
    "iPhone13,1": "iPhone 12 mini",
    "iPhone13,2": "iPhone 12",
    "iPhone13,3": "iPhone 12 Pro",
    "iPhone13,4": "iPhone 12 Pro Max",
    # iPhone 13 系列
    "iPhone14,2": "iPhone 13 Pro",
    "iPhone14,3": "iPhone 13 Pro Max",
    "iPhone14,4": "iPhone 13 mini",
    "iPhone14,5": "iPhone 13",
    "iPhone14,6": "iPhone SE (3rd)",
    "iPhone14,7": "iPhone 14",
    "iPhone14,8": "iPhone 14 Plus",
    # iPhone 14 Pro 系列
    "iPhone15,2": "iPhone 14 Pro",
    "iPhone15,3": "iPhone 14 Pro Max",
    "iPhone15,4": "iPhone 15",
    "iPhone15,5": "iPhone 15 Plus",
    # iPhone 15 Pro 系列
    "iPhone16,1": "iPhone 15 Pro",
    "iPhone16,2": "iPhone 15 Pro Max",
    # iPhone 16 系列
    "iPhone17,1": "iPhone 16 Pro",
    "iPhone17,2": "iPhone 16 Pro Max",
    "iPhone17,3": "iPhone 16",
    "iPhone17,4": "iPhone 16 Plus",
    # iPad 常见型号（可按需扩充）
    "iPad13,18": "iPad (10th)",
    "iPad13,19": "iPad (10th)",
    "iPad14,3":  "iPad Pro 11-inch (4th)",
    "iPad14,4":  "iPad Pro 11-inch (4th)",
    "iPad14,5":  "iPad Pro 12.9-inch (6th)",
    "iPad14,6":  "iPad Pro 12.9-inch (6th)",
    # 2025+（本表维护到 iPhone 17 系列；更新机型以 devicectl marketingName 为准）
    "iPhone17,5": "iPhone 16e",
    "iPhone18,1": "iPhone 17 Pro",
    "iPhone18,2": "iPhone 17 Pro Max",
    "iPhone18,3": "iPhone 17",
    "iPhone18,4": "iPhone Air",
}


def marketing_name(product_type: str) -> str:
    return IPHONE_MODEL_MAP.get(str(product_type or ""), str(product_type or ""))
