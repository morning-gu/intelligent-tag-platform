"""生成输出的解析与清洗（judge 判决解析已收敛到 mtg.verdicts.parse_verdicts）。

模型输出不保证是纯 JSON：可能带 ```json 围栏、前后解释、缺字段。这里做
"能救则救、不能救则抛"的处理——解析失败由上层决定重试或判任务失败，
绝不静默产出半截数据。

清洗规则（与提示词契约对齐）：
- 生成项：tag 去空白后非空且 ≤20 字；dimension 归一成五维中文名（未知或无
  则 None，交给合并层统计）；scenes 只留字符串；reason 去首尾空白后完整保留。
- 审核项（judge 判决）：评审归一 Task 2 起由 `mtg.verdicts.parse_verdicts`
  统一承载（生产 judge 与开放评测评审共用的融合解析器，两套容错家族取并
  集），本模块不再保留 judge 解析副本。
"""
from __future__ import annotations

import json
import re

from tagplatform.services.generation.open.mtg.prompts import DIMENSIONS

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
_MAX_TAG_LEN = 20

# 维度别名 → 中文展示名（模型可能回英文键，也可能回中文）
_DIMENSION_ALIASES: dict[str, str] = {
    **{key: name for key, name in DIMENSIONS.items()},
    **{name: name for name in DIMENSIONS.values()},
    "visual": "风格", "emotion": "情绪", "theme": "主题",
    "story": "叙事", "plot": "叙事", "角色特征": "角色",
}


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
