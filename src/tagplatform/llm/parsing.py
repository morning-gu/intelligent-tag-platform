"""LLM 输出解析：模式 A（封闭提取）的 JSON 数组契约校验（Task 5）。

client 层只管传输不做解析（client.py 的分工承诺），本模块把 LLM 文本
解析为受校验的元素列表：
- parse_closed_output（模式 A）：元素须含 tag_id、1–5 分的 applicability/
  evidence_strength，以及包含 scene_id/evidence 摘要的 evidences 数组。

模式 B（开放生成）自 2026-11 起改由 `generation/mtg/parse.py` 解析
（多轮 + 合并 + judge 的契约与本文件不同），旧 `parse_open_output` 已随
旧实现一并删除。

任何结构偏离抛 LLMParseError，消息含首个坏元素位置（0 起下标）；解析
重试的编排归任务 8 执行器，本模块不做重试。
分层红线：本模块不得 import fastapi（设计文档 §9.4）。
"""

from __future__ import annotations

import json
import re

# 模型随机加的 markdown 代码围栏：```json\n...\n``` 或裸 ```（2026-11-20
# 生产实测：Qwen3.8-27B temperature=0.7 下同 payload 一次裸 JSON、一次带
# 围栏，裸 json.loads 在首字符反引号上抛 "char 0"）。非贪婪 + $ 锚定保证
# 只剥首尾整圈围栏，JSON 字符串内部出现的 ``` 不受影响。
_FENCE_RE = re.compile(r"^```[\w-]*[ \t]*\n?(.*?)\n?[ \t]*```[ \t]*$", re.S)


class LLMParseError(Exception):
    """LLM 输出结构不符合契约（非 JSON / 非数组 / 缺键 / 分值越界）。"""


# 模式 A 的新输出契约
_CLOSED_REQUIRED = ("tag_id", "applicability", "evidence_strength", "evidences")


def _bad_element(index: int, reason: str) -> LLMParseError:
    """带首个坏元素位置（0 起下标）的解析错误。"""
    return LLMParseError(f"LLM 输出第 {index} 个元素非法：{reason}")


def _strip_code_fence(content: str) -> str:
    """剥掉包住整个输出的 markdown 围栏（```json ... ```），无围栏原样返回。"""
    m = _FENCE_RE.match(content.strip())
    return m.group(1).strip() if m else content


def _parse_array(content: str, required: tuple[str, ...] = ()) -> list:
    """json.loads → 顶层数组；容忍模型把数组包在单键对象里（{"tags": [...]}）。

    Qwen/DeepSeek 在 JSON 模式下常把结果包成对象；只要对象里恰好有一个
    列表值就按该列表解析，多个列表/无列表才判为结构不符。输入先剥掉模型
    随机加的 markdown 围栏再进 json.loads。

    单元素直返兜底：json_object 模式下模型若只选一个标签，可能直接返回
    该标签对象（而非 [{...}]）。此时对象内唯一的列表值是 evidences，
    旧逻辑会误把 evidences 列表当结果数组，导致校验看到证据卡缺全部键。
    若 dict 本身含任意 required 键 → 判定为单元素直返，包成 [data] 返回。
    空 dict {} 视为"模型未选中任何标签"，返回空列表。
    """
    try:
        data = json.loads(_strip_code_fence(content))
    except json.JSONDecodeError as e:
        raise LLMParseError(f"LLM 输出不是合法 JSON: {e}") from None
    if isinstance(data, dict):
        if required and any(key in data for key in required):
            return [data]
        if not data:
            return []
        lists = [value for value in data.values() if isinstance(value, list)]
        if len(lists) == 1:
            return lists[0]
        keys = list(data)
        raise LLMParseError(
            f"LLM 输出是对象但列表值数量为 {len(lists)}（键: {keys}）")
    if not isinstance(data, list):
        raise LLMParseError(f"LLM 输出不是 JSON 数组（{type(data).__name__}）")
    return data


def _check_five_point_score(item: dict, key: str, index: int) -> None:
    """key 须为 1–5 整数（bool 是 int 子类，显式排除）。"""
    value = item[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise _bad_element(index, f"{key} 不是整数")
    if not 1 <= value <= 5:
        raise _bad_element(index, f"{key} 越界 [1,5]: {value}")


def _validate_closed_items(data: list) -> list[dict]:
    """逐元素校验新闭环契约及场景证据摘要。"""
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            raise _bad_element(index, f"元素不是对象（{type(item).__name__}）")
        missing = [key for key in _CLOSED_REQUIRED if key not in item]
        if missing:
            raise _bad_element(index, f"缺少键: {missing}")
        tag_id = item["tag_id"]
        # LLM 常把数字型 tag_id（如 571）返回为 int 而非 str，自动转换
        if isinstance(tag_id, bool) or not isinstance(tag_id, (str, int)):
            raise _bad_element(index, f"tag_id 不是字符串或整数（{type(tag_id).__name__}）")
        if isinstance(tag_id, int):
            tag_id = str(tag_id)
            item["tag_id"] = tag_id
        if not tag_id.strip():
            raise _bad_element(index, "tag_id 不是非空字符串")
        _check_five_point_score(item, "applicability", index)
        _check_five_point_score(item, "evidence_strength", index)
        evidences = item["evidences"]
        if not isinstance(evidences, list):
            raise _bad_element(index, "evidences 不是列表")
        for evidence_index, evidence in enumerate(evidences):
            if not isinstance(evidence, dict):
                raise _bad_element(
                    index, f"evidences[{evidence_index}] 不是对象")
            if "scene_id" not in evidence or "evidence" not in evidence:
                raise _bad_element(
                    index, f"evidences[{evidence_index}] 缺少 scene_id/evidence")
            if not isinstance(evidence["scene_id"], str) or \
                    not evidence["scene_id"].strip():
                raise _bad_element(
                    index, f"evidences[{evidence_index}].scene_id 不是非空字符串")
            if not isinstance(evidence["evidence"], str) or \
                    not evidence["evidence"].strip():
                raise _bad_element(
                    index, f"evidences[{evidence_index}].evidence 不是非空字符串")
    return data


def parse_closed_output(content: str) -> list[dict]:
    """模式 A 解析：返回通过新评分和证据摘要契约校验的元素。"""
    return _validate_closed_items(_parse_array(content, _CLOSED_REQUIRED))
