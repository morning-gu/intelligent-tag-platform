"""质量指标推导（Task 7，承诺二③④的数据面）——审核与生成数据的只读聚合。

三个公开函数（session 级纯读，可独立于 API 复用）：
- `compute_metrics`：总览指标——pending 计数、审核动作计数与占比、新标签
  （origin=generated）通过率、evidence 覆盖率、生成任务统计；
- `confidence_distribution`：全部 asset_tag 的 confidence 等宽分桶；
- `tag_type_coverage`：每 tag_type 的标签行数与已通过数。

语义约定：
- 占比/通过率/覆盖率的**分母为 0 一律返回 None**（前端显示"暂无"），
  与计数为 0（正常返回 0，如 success_rate = 0/total）区分开；
- review_stats 计数按 review_record 记录（action 维度），与 asset_tag 行
  当前状态解耦（modify 记录落在仍 pending 的行上照常计数）；
- 新标签通过率分母 = generated 且已终审（approved/rejected/merged）行，
  numerator = 其中 approved 行（pending 不进分母，catalog 行不进分子）；
- avg_duration_ms 只统计终态（succeeded/failed）且两个时间戳齐全的任务。

分层红线：本模块纯读不写、不得 import fastapi（设计文档 §9.4，与
review/service 同规）。
"""

from __future__ import annotations

from bisect import bisect_right
from datetime import datetime, timezone

from sqlalchemy import case, func
from sqlalchemy.orm import Session

from tagplatform.db.models import AssetTag, GenerationTask, ReviewRecord, TagCatalog

# 已终审的审核状态（新标签通过率的分母集合；pending 不计入）
_TERMINAL_REVIEW_STATUSES = ("approved", "rejected", "merged")

# 终态任务状态（时长均值只统计有始有终的任务；queued/running 无 finished_at）
_TERMINAL_TASK_STATUSES = ("succeeded", "failed")

# review_stats 展示键 → review_record.action 取值映射
# （modified = action=="modify" 计数，brief 契约）
_ACTION_KEYS = (
    ("approved", "approve"),
    ("rejected", "reject"),
    ("merged", "merge"),
    ("modified", "modify"),
)


def _ratio(numerator: int, denominator: int | None) -> float | None:
    """占比：分母 0/None → None（"暂无"），否则浮点除。"""
    if not denominator:
        return None
    return numerator / denominator


def _as_naive_utc(dt: datetime | None) -> datetime | None:
    """aware datetime → naive UTC（去 tzinfo）；naive/None 原样返回。

    runner/审核服务落库用 aware（datetime.now(timezone.utc)），SQLite 往返
    后变 naive；混存防御——统一按 UTC 转成 naive 再相减，避免 TypeError。
    """
    if dt is not None and dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def compute_metrics(session: Session) -> dict:
    """审核推导质量指标总览（字段结构与各分母语义见模块 docstring）。"""
    # pending 计数：asset_tag 全部 pending 行（不分 origin）
    pending_count = (
        session.query(func.count(AssetTag.id))
        .filter(AssetTag.review_status == "pending")
        .scalar()
    )

    # 审核动作计数：review_record 按 action 分组（计数与行状态解耦）
    action_counts = dict(
        session.query(ReviewRecord.action, func.count(ReviewRecord.record_id))
        .group_by(ReviewRecord.action)
        .all()
    )
    total_reviews = sum(action_counts.values())
    review_stats = {
        key: {
            "count": action_counts.get(action, 0),
            "ratio": _ratio(action_counts.get(action, 0), total_reviews),
        }
        for key, action in _ACTION_KEYS
    }

    # 新标签通过率：generated 且 approved / generated 已终审（分母 0 → None）
    generated_terminal = (
        session.query(func.count(AssetTag.id))
        .filter(
            AssetTag.origin == "generated",
            AssetTag.review_status.in_(_TERMINAL_REVIEW_STATUSES),
        )
        .scalar()
    )
    generated_approved = (
        session.query(func.count(AssetTag.id))
        .filter(
            AssetTag.origin == "generated",
            AssetTag.review_status == "approved",
        )
        .scalar()
    )
    generated_pass_rate = _ratio(generated_approved, generated_terminal)

    # evidence 覆盖率：evidence_json 非空（非 NULL/""）且非 "[]" 的行占比
    total_tags = session.query(func.count(AssetTag.id)).scalar()
    covered_tags = (
        session.query(func.count(AssetTag.id))
        .filter(
            AssetTag.evidence_json.isnot(None),
            AssetTag.evidence_json != "",
            AssetTag.evidence_json != "[]",
        )
        .scalar()
    )
    evidence_coverage = _ratio(covered_tags, total_tags)

    # 任务统计：success_rate 分母 = 全部任务（含 queued/running）；
    # avg_duration_ms 只统计终态且两时间戳齐全的任务，无则 None
    task_total = session.query(func.count(GenerationTask.task_id)).scalar()
    succeeded = (
        session.query(func.count(GenerationTask.task_id))
        .filter(GenerationTask.status == "succeeded")
        .scalar()
    )
    failed = (
        session.query(func.count(GenerationTask.task_id))
        .filter(GenerationTask.status == "failed")
        .scalar()
    )
    durations_ms = [
        (_as_naive_utc(finished_at) - _as_naive_utc(created_at)).total_seconds()
        * 1000.0
        for created_at, finished_at in (
            session.query(GenerationTask.created_at, GenerationTask.finished_at)
            .filter(
                GenerationTask.status.in_(_TERMINAL_TASK_STATUSES),
                GenerationTask.created_at.isnot(None),
                GenerationTask.finished_at.isnot(None),
            )
            .all()
        )
    ]
    avg_duration_ms = (
        sum(durations_ms) / len(durations_ms) if durations_ms else None
    )

    return {
        "pending_count": pending_count,
        "review_stats": review_stats,
        "generated_pass_rate": generated_pass_rate,
        "evidence_coverage": evidence_coverage,
        "task_stats": {
            "total": task_total,
            "succeeded": succeeded,
            "failed": failed,
            "success_rate": _ratio(succeeded, task_total),
            "avg_duration_ms": avg_duration_ms,
        },
    }


def confidence_distribution(session: Session, buckets: int = 10) -> list[dict]:
    """全部 asset_tag 的 confidence 等宽分桶（None confidence 不计入）。

    桶区间左闭右开 [lo, hi)：边界值归右侧桶（0.5 → "0.5-0.6"），1.0 clamp
    到末桶。返回全部桶（含 0 计数桶），前端可直接作柱状图基线。
    边界比较用 bisect 而非 int(conf*buckets)——后者受浮点乘法误差影响
    （int(0.3*10)==2 会把 0.3 错分进 "0.2-0.3"）；分母边界 i/buckets 与
    REAL 列往返的值逐位一致，等值比较可靠。
    """
    values = [
        row[0]
        for row in session.query(AssetTag.confidence)
        .filter(AssetTag.confidence.isnot(None))
    ]
    edges = [i / buckets for i in range(buckets + 1)]
    counts = [0] * buckets
    for value in values:
        # bisect_right：等于边界时归右桶；越界（<0 / >1）自然 clamp 到首/末桶
        counts[min(bisect_right(edges[1:-1], value), buckets - 1)] += 1
    return [
        {"bucket": f"{edges[i]:.1f}-{edges[i + 1]:.1f}", "count": counts[i]}
        for i in range(buckets)
    ]


def tag_type_coverage(session: Session) -> list[dict]:
    """每 tag_type 的标签行数与已通过数（承诺二④），按 total 降序。

    tag_type 取行语义值（与审核列表 tag_type 同规则）：行带 tag_id
    （catalog 行或 merged 行）→ tag_catalog.tag_type；generated 行（无
    tag_id）→ candidate_type。两者皆缺（generated 行 candidate_type 为空）
    的行无法归类，不计入。同 total 按 tag_type 升序决胜（确定性输出）。
    """
    tag_type_expr = case(
        (AssetTag.tag_id.isnot(None), TagCatalog.tag_type),
        else_=AssetTag.candidate_type,
    ).label("tag_type")
    rows = (
        session.query(
            tag_type_expr,
            func.count(AssetTag.id),
            func.sum(case((AssetTag.review_status == "approved", 1), else_=0)),
        )
        .outerjoin(TagCatalog, AssetTag.tag_id == TagCatalog.tag_id)
        .group_by(tag_type_expr)
        .order_by(func.count(AssetTag.id).desc(), tag_type_expr.asc())
        .all()
    )
    return [
        {"tag_type": tag_type, "total": total, "approved": int(approved or 0)}
        for tag_type, total, approved in rows
        if tag_type is not None  # 丢弃无法归类的 NULL 组
    ]
