"""生成 / 审核输出的解析与清洗。

模型输出不保证是纯 JSON：可能带 ```json 围栏、前后解释、缺字段。这里做
"能救则救、不能救则抛"的处理——解析失败由上层决定重试或判任务失败，
绝不静默产出半截数据。

清洗规则（与提示词契约对齐）：
- 生成项：tag 去空白后非空且 ≤20 字；dimension 归一成五维中文名（未知或无
  则 None，交给合并层统计）；scenes 只留字符串；reason 去首尾空白后完整保留。
- 审核项：三维分值（relevance/semantics/value）只认 0-4 的整数；显式
  null 是"模型申明无法判断"（仅 relevance 合法），
  漏字段与非法值是**协议错误**——两者不再混成同一个 null，协议错误抛
  MTGParseError 交上层重试，而不是让整批候选按未过线静默淘汰；
  confidence 夹到 [0,1]；scenes 只留字符串。
"""
from __future__ import annotations

import json
import re

from tagplatform.services.generation.open.mtg.prompts import DIMENSIONS
from tagplatform.services.tags.value_scoring import (
    VALUE_DIRECTIONS,
    VALUE_DIRECTION_ALIASES,
)

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
_MAX_TAG_LEN = 20

# 维度别名 → 中文展示名（模型可能回英文键，也可能回中文）
_DIMENSION_ALIASES: dict[str, str] = {
    **{key: name for key, name in DIMENSIONS.items()},
    **{name: name for name in DIMENSIONS.values()},
    "visual": "风格", "emotion": "情绪", "theme": "主题",
    "story": "叙事", "plot": "叙事", "角色特征": "角色",
}

# judge 标量评分轴（与统一评分标准、pipeline 阈值互为协议；value 由三方向分聚合）
_JUDGE_SCORE_AXES = ("relevance", "semantics")
# 材料缺失（relevance）或缺少通用释义（semantics）时允许显式 null。
_NULLABLE_SCORE_AXES = ("relevance", "semantics")


class MTGParseError(ValueError):
    """模型输出无法解析为约定数组（上层决定重试或失败）。

    `items` 携带同一次响应里**已经合格**的判决：末轮重试仍有条目违协议
    时，上层可以保住这些判决，只把违协议的候选按未判淘汰——一条坏判决
    不该让整片产出归零。
    """

    def __init__(self, message: str, *, items: list[dict] | None = None):
        super().__init__(message)
        self.items = items or []


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
        if data.get("tags") == []:
            return []
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


def parse_generation_items(text: str, max_items: int | None = 20, *,
                           exact_names: bool = False) -> list[dict]:
    """生成输出 → [{tag, scope, dimension, dir, scenes, reason, desc}]（非法项丢弃）。"""
    if max_items is not None and max_items <= 0:
        return []
    items: list[dict] = []
    seen: set[str] = set()
    for entry in extract_json_array(text):
        if not isinstance(entry, dict):
            continue
        tag = str(entry.get("tag") or entry.get("tag_name") or "").strip()
        if not tag or len(tag) > _MAX_TAG_LEN:
            continue
        key = tag if exact_names else tag.casefold()
        if key in seen:
            continue
        seen.add(key)
        raw_scenes = entry.get("scenes")
        scenes = [s.strip() for s in raw_scenes if isinstance(s, str) and s.strip()] \
            if isinstance(raw_scenes, list) else []
        reason = str(entry.get("reason") or "").strip()
        items.append({
            "tag": tag,
            "scope": normalize_scope(entry.get("scope")),
            "dimension": normalize_dimension(entry.get("dimension")),
            # 模型给的归属目录（id / 全路径 / 目录名都可能，解析阶段只做清洗，
            # 到底指向哪个目录由 pipeline 用标签库解析并校验）
            "dir": _clean_dir(entry.get("dir") or entry.get("dir_id")
                              or entry.get("catalog_id")),
            "scenes": scenes,
            "reason": reason,
            # 通用释义（旧口径里模型只给 reason；这里兼容 description 字段，
            # 是否"够通用"由 descriptions.is_general 判定，不在解析层猜）
            "desc": str(entry.get("desc") or entry.get("definition") or "").strip(),
        })
        if max_items is not None and len(items) >= max_items:
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


# 协议错误哨兵：与"显式 null"（模型申明无法判断）严格区分。
_MISSING = object()


def _clean_score(value):
    """三维分值清洗：0-4 整数（含整数值浮点）→ int；显式 null → None；
    布尔/越界/非数值 → `_MISSING`（协议错误，交上层重试）。

    绝不猜一个分数放行，也不把"模型没按协议给分"伪装成"模型判不了"。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return _MISSING
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, int) and 0 <= value <= 4:
        return value
    return _MISSING


def _clean_value_scores(entry: dict) -> tuple[dict[str, int] | None, list[str]]:
    """解析标签价值三方向分（presentation/search/recommendation，均为 0-4 整数）。

    支持顶层 `value_scores` 对象，也容忍模型把三方向对象放在 `value` 下或用中文键名。
    任一方向缺失、为 null 或非 0-4 整数均记为协议错误，交上层重试。
    """
    raw = entry.get("value_scores")
    if raw is None and isinstance(entry.get("value"), dict):
        raw = entry["value"]
    if not isinstance(raw, dict):
        if "value_scores" not in entry and "value" not in entry:
            return None, ["value_scores 漏字段"]
        return None, [f"value_scores 必须是三方向分值对象（收到 {raw!r}）"]
    normalized: dict[str, object] = {}
    for key, val in raw.items():
        canonical = VALUE_DIRECTION_ALIASES.get(str(key).strip().lower()) or VALUE_DIRECTION_ALIASES.get(str(key).strip())
        if canonical and canonical not in normalized:
            normalized[canonical] = val
    parsed: dict[str, int] = {}
    errors: list[str] = []
    for direction in VALUE_DIRECTIONS:
        if direction not in normalized:
            errors.append(f"value_scores.{direction} 漏字段")
            continue
        raw_score = normalized[direction]
        if isinstance(raw_score, dict) and "score" in raw_score:
            raw_score = raw_score["score"]
        score = _clean_score(raw_score)
        if score is _MISSING:
            errors.append(f"value_scores.{direction} 非法值 {raw_score!r}")
        elif score is None:
            errors.append(f"value_scores.{direction} 不接受 null")
        else:
            parsed[direction] = score
    if errors:
        return None, errors
    return parsed, []


def parse_judge_items(text: str) -> list[dict]:
    """解析三维评分（含价值三方向分）和证据。

    部分条目的分数或证据字段违协议时，异常仍携带同批可用判决。
    """
    items: list[dict] = []
    problems: list[str] = []
    for entry in extract_json_array(text):
        if not isinstance(entry, dict):
            problems.append("判决条目必须是对象")
            continue
        tag = _clean_tag(entry.get("tag") or entry.get("tag_name"))
        if not tag:
            problems.append("判决缺少有效标签名")
            continue
        scores: dict[str, int | None] = {}
        broken: list[str] = []
        for axis in _JUDGE_SCORE_AXES:
            if axis not in entry:
                broken.append(f"{axis} 漏字段")
                continue
            score = _clean_score(entry[axis])
            if score is _MISSING:
                broken.append(f"{axis} 非法值 {entry[axis]!r}")
            elif score is None and axis not in _NULLABLE_SCORE_AXES:
                broken.append(f"{axis} 不接受 null")
            else:
                scores[axis] = score
        value_scores, value_errors = _clean_value_scores(entry)
        broken.extend(value_errors)
        raw_scenes = entry.get("scenes")
        if raw_scenes is not None and (not isinstance(raw_scenes, list)
                                       or any(not isinstance(s, str) for s in raw_scenes)):
            broken.append("scenes 必须是场景 ID 数组")
        if broken:
            problems.append(f"{tag}：" + "、".join(broken))
            continue
        scenes = [s.strip() for s in raw_scenes or []
                  if isinstance(s, str) and s.strip()]
        candidate_id = entry.get("candidate_id")
        items.append({
            "tag": tag,
            **({"candidate_id": candidate_id.strip()} if isinstance(candidate_id, str)
               and candidate_id.strip() else {}),
            **scores,
            "value_scores": value_scores,
            "confidence": _clamp_confidence(entry.get("confidence")),
            "scenes": scenes,
            "quote": str(entry.get("quote") or "").strip()[:60],
            "reason": str(entry.get("reason") or "").strip()[:40],
        })
    if problems:
        raise MTGParseError("判决不合协议（" + "；".join(problems[:5]) + "）",
                            items=items)
    if not items:
        raise MTGParseError("审核输出为空数组")
    return items
