"""生成 / 审核输出的解析与清洗。

模型输出不保证是纯 JSON：可能带 ```json 围栏、前后解释、缺字段。这里做
"能救则救、不能救则抛"的处理——解析失败由上层决定重试或判任务失败，
绝不静默产出半截数据。

清洗规则（与提示词契约对齐）：
- 生成项：tag 去空白后非空且 ≤20 字；dimension 归一成五维中文名（未知或无
  则 None，交给合并层统计）；scenes 只留字符串；reason 截断 40 字。
- 审核项：verdict 只认 supported/insufficient/contradicted（其余按
  insufficient 处理——保守，不放行）；confidence 夹到 [0,1]；importance 只认
  三档，未知按 secondary；scenes 只留字符串。
"""
from __future__ import annotations

import json
import re

from tagplatform.generation.mtg.prompts import DIMENSIONS

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
_MAX_TAG_LEN = 20

# 维度别名 → 中文展示名（模型可能回英文键，也可能回中文）
_DIMENSION_ALIASES: dict[str, str] = {
    **{key: name for key, name in DIMENSIONS.items()},
    **{name: name for name in DIMENSIONS.values()},
    "visual": "风格", "emotion": "情绪", "theme": "主题",
    "story": "叙事", "plot": "叙事", "角色特征": "角色",
}

_VERDICTS = ("supported", "insufficient", "contradicted")
_IMPORTANCES = ("core", "secondary", "incidental")


class MTGParseError(ValueError):
    """模型输出无法解析为约定数组（上层决定重试或失败）。"""


def _find_items(node, depth: int = 0):
    """在嵌套结构里找第一个"元素为对象"的列表。

    只接受元素全为对象的列表：`{"标签": {"verdict": ..., "scenes": [...]}}`
    这种以标签为键的对象里也有列表（scenes），但它不是候选列表，不能误取。
    """
    if depth > 4:
        return None
    if isinstance(node, list):
        if node and all(isinstance(item, dict) for item in node):
            return node
        return None
    if isinstance(node, dict):
        for value in node.values():
            found = _find_items(value, depth + 1)
            if found is not None:
                return found
    return None


def extract_json_array(text: str) -> list:
    """从模型文本中取出 JSON 数组（容忍围栏与前后解释文字）。"""
    raw = (text or "").strip()
    fenced = _FENCE_RE.search(raw)
    if fenced:
        raw = fenced.group(1).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("["), raw.rfind("]")
        if start < 0 or end <= start:
            raise MTGParseError("输出中找不到 JSON 数组") from None
        try:
            data = json.loads(raw[start:end + 1])
        except json.JSONDecodeError as exc:
            raise MTGParseError(f"JSON 数组解析失败: {exc}") from None
    if isinstance(data, dict):
        # 容错一：{"tags": [...]} / {"verdicts": [...]} 或更深一层包装
        found = _find_items(data)
        if found is not None:
            return found
        # 容错二：单个判决对象（模型偶发只回一条、不套数组）
        if any(key in data for key in ("verdict", "tag", "tag_name")):
            return [data]
        # 容错三：以标签名为键的对象映射 {"标签": {...}}
        if data and all(isinstance(value, dict) for value in data.values()):
            return [{"tag": key, **value} for key, value in data.items()]
        raise MTGParseError("输出是对象而非数组")
    if not isinstance(data, list):
        raise MTGParseError(f"输出不是数组（{type(data).__name__}）")
    return data


def normalize_dimension(value) -> str | None:
    """维度归一成中文五维名；无法识别返回 None。"""
    if not isinstance(value, str):
        return None
    return _DIMENSION_ALIASES.get(value.strip().lower()) or \
        _DIMENSION_ALIASES.get(value.strip())


# 层级值归一（内部用，不落库）：film/全片/影片/整体；scene/场景/局部/单场
_SCOPE_ALIASES = {
    "film": "film", "movie": "film", "global": "film",
    "全片": "film", "影片": "film", "整体": "film", "全片级": "film",
    "scene": "scene", "local": "scene",
    "场景": "scene", "局部": "scene", "单场": "scene", "场景级": "scene",
}


def normalize_scope(value) -> str | None:
    """层级归一成 film|scene；缺失或无法识别返回 None（按未声明处理）。"""
    if not isinstance(value, str):
        return None
    return _SCOPE_ALIASES.get(value.strip().lower())


def parse_generation_items(text: str, max_items: int = 20) -> list[dict]:
    """生成输出 → [{tag, scope, dimension, dir, scenes, reason}]（非法项丢弃）。"""
    items: list[dict] = []
    seen: set[str] = set()
    for entry in extract_json_array(text):
        if not isinstance(entry, dict):
            continue
        tag = str(entry.get("tag") or entry.get("tag_name") or "").strip()
        if not tag or len(tag) > _MAX_TAG_LEN:
            continue
        key = tag.casefold()
        if key in seen:
            continue
        seen.add(key)
        scenes = [s.strip() for s in entry.get("scenes") or []
                  if isinstance(s, str) and s.strip()]
        reason = str(entry.get("reason") or entry.get("description") or "").strip()
        items.append({
            "tag": tag,
            "scope": normalize_scope(entry.get("scope")),
            "dimension": normalize_dimension(entry.get("dimension")),
            # 模型给的归属目录（id / 全路径 / 目录名都可能，解析阶段只做清洗，
            # 到底指向哪个目录由 pipeline 用标签库解析并校验）
            "dir": _clean_dir(entry.get("dir") or entry.get("dir_id")
                              or entry.get("catalog_id")),
            "scenes": scenes,
            "reason": reason[:40],
        })
        if len(items) >= max_items:
            break
    return items


def _clean_dir(value) -> str | None:
    """目录字段清洗：去空白/引号/尖括号，超长截断；空值返回 None。"""
    text = str(value or "").strip().strip("`'\"<>[]（）()")
    return text[:120] or None


def _clean_tag(value) -> str:
    """判决里的标签名：模型常把整行候选（含维度/依据）抄回来，按分隔符截断。"""
    text = str(value or "").strip()
    for separator in ("｜", "|"):
        if separator in text:
            text = text.split(separator, 1)[0]
    return text.strip()[:40]


def _clamp_confidence(value) -> float:
    """置信度夹到 [0,1]；非数值按 0 处理（保守）。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return min(1.0, max(0.0, number))


def parse_judge_items(text: str) -> list[dict]:
    """审核输出 → [{tag, verdict, importance, confidence, scenes, reason}]。"""
    items: list[dict] = []
    for entry in extract_json_array(text):
        if not isinstance(entry, dict):
            continue
        tag = _clean_tag(entry.get("tag") or entry.get("tag_name"))
        if not tag:
            continue
        verdict = str(entry.get("verdict") or "").strip().lower()
        if verdict not in _VERDICTS:
            verdict = "insufficient"
        importance = str(entry.get("importance") or "").strip().lower()
        if importance not in _IMPORTANCES:
            importance = "secondary"
        scenes = [s.strip() for s in entry.get("scenes") or []
                  if isinstance(s, str) and s.strip()]
        items.append({
            "tag": tag,
            "verdict": verdict,
            "importance": importance,
            "confidence": _clamp_confidence(entry.get("confidence")),
            "scenes": scenes,
            "quote": str(entry.get("quote") or "").strip()[:60],
            "reason": str(entry.get("reason") or "").strip()[:40],
        })
    if not items:
        raise MTGParseError("审核输出为空数组")
    return items
