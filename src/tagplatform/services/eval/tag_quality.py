"""标签质量三维评分的五档汇总指标（开放评测报告复用）。

生产任务附带的质量评估（分流快照、评估队列与单片重试）已移除：开放生成
与闭环生成同一收尾流程，质量评估统一走独立开放评测页（/eval-open）。
本模块只保留评分汇总指标——`open_report` 组装报告时复用同一套五档
0–4 量表口径（MAX_SCORE / DIMENSIONS / quality_metrics）。评分 rubric
文案驻 capabilities/prompts/open_review.py。

分层红线：本模块不 import fastapi（设计文档 §9.4）。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

MAX_SCORE = 4
DIMENSIONS = ("relevance", "semantics", "value")


class DimensionMetrics(BaseModel):
    evaluated_tags: int
    average: float | None
    high_rate: float | None
    distribution: list[int]
    undetermined: int
    undetermined_rate: float | None


class QualityMetrics(BaseModel):
    total_tags: int
    evaluated_tags: int
    max_score: Literal[4]
    coverage: float | None
    joint_high_rate: float | None
    three_dimension_high_rate: float | None
    undetermined_tags: int
    undetermined_rate: float | None
    relevance: DimensionMetrics
    semantics: DimensionMetrics
    value: DimensionMetrics


def _ratio(count: int, total: int) -> float | None:
    return count / total if total else None


def _dimension_metrics(values: list[int | None]) -> DimensionMetrics:
    distribution = [values.count(value) for value in range(MAX_SCORE + 1)]
    judged = sum(distribution)
    unknown = values.count(None)
    return DimensionMetrics(
        evaluated_tags=len(values),
        average=_ratio(sum(score * count for score, count in enumerate(distribution)), judged),
        high_rate=_ratio(distribution[-1], judged),
        distribution=distribution,
        undetermined=unknown, undetermined_rate=_ratio(unknown, len(values)),
    )


def _joint_high_rate(rows: list[dict[str, int | None]], axes: tuple[str, ...]) -> float | None:
    ratings = [tuple(row.get(axis) for axis in axes) for row in rows]
    return _ratio(ratings.count((MAX_SCORE,) * len(axes)),
                  sum(None not in rating for rating in ratings))


def quality_metrics(total: int, scores: list[dict]) -> QualityMetrics:
    """逐维均分/满分率/分布 + 联合满分率 + 无法判断占比。

    统一五档 0–4 量表（tag-quality-v4 起）。
    """
    # 个别记录缺少 value 对象（如评估中断）；显式的 score=null 才计入无法判断。
    rows = [{axis: result["score"] for axis in DIMENSIONS
             if (result := score.get(axis)) is not None} for score in scores]
    unknown = sum(None in row.values() for row in rows)
    return QualityMetrics(
        total_tags=total, evaluated_tags=len(rows), coverage=_ratio(len(rows), total),
        max_score=MAX_SCORE,
        joint_high_rate=_joint_high_rate(rows, ("relevance", "semantics")),
        three_dimension_high_rate=_joint_high_rate(rows, DIMENSIONS),
        undetermined_tags=unknown, undetermined_rate=_ratio(unknown, len(rows)),
        **{axis: _dimension_metrics([row[axis] for row in rows if axis in row])
           for axis in DIMENSIONS},
    )
