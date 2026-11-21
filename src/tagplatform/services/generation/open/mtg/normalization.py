"""归一化键：标签名 → 跨层比较用的规范键（单一实现）。

合并去重（merge）、judge 判决映射（judge）、管线淘汰对账（pipeline）必须用
**同一套**归一化——否则会出现"合并层认为同形、judge 层对不上键"这类跨层
错配。本模块是唯一实现，三个消费方都从这里引用；改动实现要同时核对三方
（本 docstring 即协议声明）。
"""
from __future__ import annotations


def normalize_key(text: str) -> str:
    """归一化键：只留字母/数字/汉字，casefold，丢弃其余字符（含空白标点）。

    isalnum 是 Unicode 感知的（中文/重音字符保留）；text 为 None 时按空串
    处理（judge 解析层可能给出空值）。
    """
    return "".join(ch for ch in (text or "").casefold() if ch.isalnum())
