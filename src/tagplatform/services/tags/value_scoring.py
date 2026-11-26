"""标签价值三方向评分的权重定义与聚合计算（Judge 与开放评测共用）。

标签价值按三个方向独立打 0–4 整数分：
- presentation（内容呈现）：内容特色 / 情节看点 / 角色画像
- search（内容搜索）：类型 / 风格 / 情绪
- recommendation（内容推荐）：时间 / 强度 / 人员

不同标签种类（内容级 content / 场景级 scene / 人物级 character）采用不同权重，
由程序确定性加权聚合为保留两位小数的 0.00–4.00 价值分，不依赖模型自行做算术。

分层红线：本模块纯计算逻辑，不得 import fastapi / sqlalchemy。
"""
from __future__ import annotations

import math
from collections.abc import Mapping

from tagplatform.services.tags.library import (
    SCOPE_CHARACTER,
    SCOPE_CONTENT,
    SCOPE_SCENE,
)

# 三个价值方向（顺序固定，与 Settings.tag_value_weights_* 逗号配置严格对应）
VALUE_DIRECTIONS: tuple[str, str, str] = (
    "presentation",
    "search",
    "recommendation",
)

VALUE_DIRECTION_LABELS: dict[str, str] = {
    "presentation": "内容呈现",
    "search": "内容搜索",
    "recommendation": "内容推荐",
}

# 模型可能返回中文键或常见别名，解析时统一归一到英文标准键
VALUE_DIRECTION_ALIASES: dict[str, str] = {
    "presentation": "presentation",
    "内容呈现": "presentation",
    "呈现": "presentation",
    "search": "search",
    "内容搜索": "search",
    "搜索": "search",
    "recommendation": "recommendation",
    "内容推荐": "recommendation",
    "推荐": "recommendation",
}

# 默认权重（顺序：内容呈现, 内容搜索, 内容推荐）
DEFAULT_VALUE_WEIGHTS: dict[str, tuple[float, float, float]] = {
    SCOPE_CONTENT: (0.30, 0.40, 0.30),
    SCOPE_SCENE: (0.50, 0.40, 0.10),
    SCOPE_CHARACTER: (0.55, 0.30, 0.15),
}

_TAG_KIND_ALIASES: dict[str, str] = {
    "content": SCOPE_CONTENT,
    "film": SCOPE_CONTENT,
    "内容": SCOPE_CONTENT,
    "内容级": SCOPE_CONTENT,
    "全片": SCOPE_CONTENT,
    "全片级": SCOPE_CONTENT,
    "整片": SCOPE_CONTENT,
    "scene": SCOPE_SCENE,
    "excerpt": SCOPE_SCENE,
    "场景": SCOPE_SCENE,
    "场景级": SCOPE_SCENE,
    "单场景": SCOPE_SCENE,
    "character": SCOPE_CHARACTER,
    "人物": SCOPE_CHARACTER,
    "人物级": SCOPE_CHARACTER,
    "角色": SCOPE_CHARACTER,
    "角色级": SCOPE_CHARACTER,
}

TAG_KIND_LABELS: dict[str, str] = {
    SCOPE_CONTENT: "内容级",
    SCOPE_SCENE: "场景级",
    SCOPE_CHARACTER: "人物级",
}


def normalize_tag_kind(kind: str | None) -> str:
    """标签种类文本 → content / scene / character（未知或空回落 content）。"""
    cleaned = (kind or "").strip().lower()
    return _TAG_KIND_ALIASES.get(cleaned, SCOPE_CONTENT)


def parse_direction_weights(raw_value: str, field_name: str) -> tuple[float, float, float]:
    """逗号分隔三权重字符串 → 归一化三元组（内容呈现, 内容搜索, 内容推荐）。"""
    try:
        raw = [float(part) for part in raw_value.split(",") if part.strip()]
    except ValueError as exc:
        raise ValueError(f"{field_name} 无法解析: {raw_value!r}") from exc
    if len(raw) != 3 or any(w < 0 for w in raw) or sum(raw) <= 0:
        raise ValueError(
            f"{field_name} 需为三个非负数（内容呈现,内容搜索,内容推荐），"
            f"当前: {raw_value!r}"
        )
    total = sum(raw)
    return (raw[0] / total, raw[1] / total, raw[2] / total)


def weights_for_kind(
    tag_kind: str | None,
    weights: Mapping[str, tuple[float, float, float]] | None = None,
) -> tuple[float, float, float]:
    """按标签种类取归一化三方向权重元组。"""
    kind = normalize_tag_kind(tag_kind)
    table = weights if weights is not None else DEFAULT_VALUE_WEIGHTS
    pair = table.get(kind, DEFAULT_VALUE_WEIGHTS[kind])
    total = sum(pair)
    if len(pair) != 3 or total <= 0:
        return DEFAULT_VALUE_WEIGHTS[kind]
    return (pair[0] / total, pair[1] / total, pair[2] / total)


def weights_dict_for_kind(
    tag_kind: str | None,
    weights: Mapping[str, tuple[float, float, float]] | None = None,
) -> dict[str, float]:
    """按标签种类返回 {direction: weight} 字典（审计与报告展示用）。"""
    triple = weights_for_kind(tag_kind, weights)
    return {
        direction: round(weight, 4)
        for direction, weight in zip(VALUE_DIRECTIONS, triple, strict=True)
    }


def aggregate_value_score(
    scores: Mapping[str, int | float | None],
    tag_kind: str | None,
    weights: Mapping[str, tuple[float, float, float]] | None = None,
) -> float | None:
    """将三个价值方向分按标签种类权重聚合为保留两位小数的价值分（任一方向 None → None）。"""
    triple = weights_for_kind(tag_kind, weights)
    total = 0.0
    for direction, weight in zip(VALUE_DIRECTIONS, triple, strict=True):
        val = scores.get(direction)
        if val is None or isinstance(val, bool) or not isinstance(val, (int, float)):
            return None
        total += weight * float(val)
    return round(total, 2)


def score_bucket(score: int | float, max_score: int = 4) -> int:
    """连续分值 → 0..max_score 整数分档（常规四舍五入 half-up，避免银行家舍入偏移）。"""
    bucket = int(math.floor(float(score) + 0.5))
    return min(max_score, max(0, bucket))


def format_score(score: int | float | None) -> str:
    """展示用分值格式化：整数不带小数，浮点固定两位小数，None → null。"""
    if score is None:
        return "null"
    if isinstance(score, float) and not score.is_integer():
        return f"{score:.2f}"
    return str(int(score)) if isinstance(score, float) else str(score)


__all__ = [
    "DEFAULT_VALUE_WEIGHTS",
    "TAG_KIND_LABELS",
    "VALUE_DIRECTIONS",
    "VALUE_DIRECTION_ALIASES",
    "VALUE_DIRECTION_LABELS",
    "aggregate_value_score",
    "format_score",
    "normalize_tag_kind",
    "parse_direction_weights",
    "score_bucket",
    "weights_dict_for_kind",
    "weights_for_kind",
]
