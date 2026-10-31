"""LLM 输出解析：模式 A/B 的 JSON 数组契约校验（Task 5）。

client 层只管传输不做解析（client.py 的分工承诺），本模块把 LLM 文本
解析为受校验的元素列表：
- parse_closed_output（模式 A）：元素须含 tag_id/tag_name/relevance/
  confidence/evidence，relevance/confidence ∈ [0,1]（含边界）；
- parse_open_output（模式 B）：元素须含 tag_name/tag_type/description/
  confidence/evidence，confidence ∈ [0,1]；多余键（含 tag_id——模式 B 的
  tag_id 由匹配层赋值，LLM 不产出）静默丢弃。

任何结构偏离抛 LLMParseError，消息含首个坏元素位置（0 起下标）；解析
重试的编排归任务 8 执行器，本模块不做重试。
分层红线：本模块不得 import fastapi（设计文档 §9.4）。
"""

from __future__ import annotations

import json


class LLMParseError(Exception):
    """LLM 输出结构不符合契约（非 JSON / 非数组 / 缺键 / 分值越界）。"""


# 两模式的元素必含键（evidence 恒为列表；元素内结构由映射层 Evidence(**e) 兜底）
_CLOSED_REQUIRED = ("tag_id", "tag_name", "relevance", "confidence", "evidence")
_OPEN_REQUIRED = ("tag_name", "tag_type", "description", "confidence", "evidence")


def _bad_element(index: int, reason: str) -> LLMParseError:
    """带首个坏元素位置（0 起下标）的解析错误。"""
    return LLMParseError(f"LLM 输出第 {index} 个元素非法：{reason}")


def _parse_array(content: str) -> list:
    """json.loads → 顶层必须为数组；否则抛 LLMParseError。"""
    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        raise LLMParseError(f"LLM 输出不是合法 JSON: {e}") from None
    if not isinstance(data, list):
        raise LLMParseError(f"LLM 输出不是 JSON 数组（{type(data).__name__}）")
    return data


def _check_in_unit_range(item: dict, key: str, index: int) -> None:
    """key 须为 [0,1] 内数值（bool 是 int 子类，显式排除）。"""
    value = item[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _bad_element(index, f"{key} 不是数值")
    if not 0.0 <= value <= 1.0:
        raise _bad_element(index, f"{key} 越界 [0,1]: {value}")


def _validate(data: list, required: tuple[str, ...],
              numeric_keys: tuple[str, ...]) -> list[dict]:
    """逐元素校验：对象形状、必含键、数值域、evidence 为列表。"""
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            raise _bad_element(index, f"元素不是对象（{type(item).__name__}）")
        missing = [key for key in required if key not in item]
        if missing:
            raise _bad_element(index, f"缺少键: {missing}")
        for key in numeric_keys:
            _check_in_unit_range(item, key, index)
        if not isinstance(item["evidence"], list):
            raise _bad_element(index, "evidence 不是列表")
    return data


def parse_closed_output(content: str) -> list[dict]:
    """模式 A 解析：目录标签判定结果，返回已校验的元素列表（原样保留键）。"""
    return _validate(_parse_array(content), _CLOSED_REQUIRED,
                     ("relevance", "confidence"))


def parse_open_output(content: str) -> list[dict]:
    """模式 B 解析：开放候选，仅保留契约五键（多余键含 tag_id 静默丢弃）。"""
    data = _validate(_parse_array(content), _OPEN_REQUIRED, ("confidence",))
    return [{key: item[key] for key in _OPEN_REQUIRED} for item in data]
