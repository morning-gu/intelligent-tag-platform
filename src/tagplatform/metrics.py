"""质量指标推导（Task 7，承诺二③④的数据面）——审核与生成数据的只读聚合。

三个公开函数（session 级纯读，可独立于 API 复用）：
- `compute_metrics`：总览指标——pending 计数、审核结果计数与占比、新标签
  （库外新词，tag_id 为空）通过率、evidence 覆盖率、生成任务统计；
- `confidence_distribution`：全部 pre_asset_tag 的 confidence 等宽分桶；
- `tag_type_coverage`：每 tag_type 的标签行数与已通过数。

语义约定：
- 占比/通过率/覆盖率的**分母为 0 一律返回 None**（前端显示"暂无"），
  与计数为 0（正常返回 0，如 success_rate = 0/total）区分开；
- v2 起审核不再写流水表（review_record），review_stats 改按 pre 行状态
  统计：approved / rejected 有值，merged（并入既有）与 modified（修改后
  通过）与 approve 同终态、无法从行上区分，恒为 0；
- 新标签通过率分母 = 库外新词（tag_id 为空）且已终审（approved/rejected）
  行，numerator = 其中 approved 行（pending 不进分母，命中库内标签的行
  不进分子）；
- avg_duration_ms 只统计终态（succeeded/failed）且两个时间戳齐全的任务。

分层红线：本模块纯读不写、不得 import fastapi（设计文档 §9.4，与
review/service 同规）。
"""

from __future__ import annotations

from bisect import bisect_right
from datetime import datetime, timezone

from sqlalchemy import case, func
from sqlalchemy.orm import Session

from tagplatform.db.models import PreAssetTag, TagCatalog, TagGenerationTask

# 已终审的审核状态（新标签通过率的分母集合；pending 不计入）
_TERMINAL_REVIEW_STATUSES = ("approved", "rejected")

# 终态任务状态（时长均值只统计有始有终的任务；queued/running 无 finished_at）
_TERMINAL_TASK_STATUSES = ("succeeded", "failed")

# review_stats 展示键（v2 无审核流水表，只按行终态计数；merged/modified
# 与 approved 同终态，行上不可区分，恒为 0）
_REVIEW_KEYS = ("approved", "rejected", "merged", "modified")


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
    # pending 计数：pre_asset_tag 全部待审行（不分 origin）
    pending_count = (
        session.query(func.count(PreAssetTag.id))
        .filter(PreAssetTag.review_status == "pending")
        .scalar()
    )

    # 审核结果计数：按行终态统计（v2 起不再写审核流水表）
    approved_count = (
        session.query(func.count(PreAssetTag.id))
        .filter(PreAssetTag.review_status == "approved")
        .scalar()
    )
    rejected_count = (
        session.query(func.count(PreAssetTag.id))
        .filter(PreAssetTag.review_status == "rejected")
        .scalar()
    )
    status_counts = {"approved": approved_count, "rejected": rejected_count}
    total_reviews = approved_count + rejected_count
    review_stats = {
        key: {
            "count": status_counts.get(key, 0),
            "ratio": (_ratio(status_counts.get(key, 0), total_reviews)
                      if key in status_counts else None),
        }
        for key in _REVIEW_KEYS
    }

    # 新标签通过率：库外新词（tag_id 为空）已终审 / 其中 approved
    generated_terminal = (
        session.query(func.count(PreAssetTag.id))
        .filter(
            PreAssetTag.tag_id.is_(None),
            PreAssetTag.review_status.in_(_TERMINAL_REVIEW_STATUSES),
        )
        .scalar()
    )
    generated_approved = (
        session.query(func.count(PreAssetTag.id))
        .filter(
            PreAssetTag.tag_id.is_(None),
            PreAssetTag.review_status == "approved",
        )
        .scalar()
    )
    generated_pass_rate = _ratio(generated_approved, generated_terminal)

    # evidence 覆盖率：evidence_json 非空（非 NULL/""）且非 "[]" 的行占比
    total_tags = session.query(func.count(PreAssetTag.id)).scalar()
    covered_tags = (
        session.query(func.count(PreAssetTag.id))
        .filter(
            PreAssetTag.evidence_json.isnot(None),
            PreAssetTag.evidence_json != "",
            PreAssetTag.evidence_json != "[]",
        )
        .scalar()
    )
    evidence_coverage = _ratio(covered_tags, total_tags)

    # 任务统计：success_rate 分母 = 全部任务（含 queued/running）；
    # avg_duration_ms 只统计终态且两时间戳齐全的任务，无则 None
    task_total = session.query(func.count(TagGenerationTask.task_id)).scalar()
    succeeded = (
        session.query(func.count(TagGenerationTask.task_id))
        .filter(TagGenerationTask.status == "succeeded")
        .scalar()
    )
    failed = (
        session.query(func.count(TagGenerationTask.task_id))
        .filter(TagGenerationTask.status == "failed")
        .scalar()
    )
    durations_ms = [
        (_as_naive_utc(finished_at) - _as_naive_utc(created_at)).total_seconds()
        * 1000.0
        for created_at, finished_at in (
            session.query(TagGenerationTask.created_at, TagGenerationTask.finished_at)
            .filter(
                TagGenerationTask.status.in_(_TERMINAL_TASK_STATUSES),
                TagGenerationTask.created_at.isnot(None),
                TagGenerationTask.finished_at.isnot(None),
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
    """全部 pre_asset_tag 的 confidence 等宽分桶（None confidence 不计入）。

    桶区间左闭右开 [lo, hi)：边界值归右侧桶（0.5 → "0.5-0.6"），1.0 clamp
    到末桶。返回全部桶（含 0 计数桶），前端可直接作柱状图基线。
    边界比较用 bisect 而非 int(conf*buckets)——后者受浮点乘法误差影响
    （int(0.3*10)==2 会把 0.3 错分进 "0.2-0.3"）；分母边界 i/buckets 与
    REAL 列往返的值逐位一致，等值比较可靠。
    """
    values = [
        row[0]
        for row in session.query(PreAssetTag.confidence)
        .filter(PreAssetTag.confidence.isnot(None))
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

    tag_type 取行语义值（与审核列表同规则）：行带 tag_id → tag_catalog.tag_type；
    库外新词（tag_id 为空）在库内尚无类型，无法归类，不计入。同 total 按
    tag_type 升序决胜（确定性输出）。
    """
    rows = (
        session.query(
            TagCatalog.tag_type,
            func.count(PreAssetTag.id),
            func.sum(case((PreAssetTag.review_status == "approved", 1), else_=0)),
        )
        .join(TagCatalog, PreAssetTag.tag_id == TagCatalog.tag_id)
        .group_by(TagCatalog.tag_type)
        .order_by(func.count(PreAssetTag.id).desc(), TagCatalog.tag_type.asc())
        .all()
    )
    return [
        {"tag_type": tag_type, "total": total, "approved": int(approved or 0)}
        for tag_type, total, approved in rows
        if tag_type is not None  # 丢弃无法归类的 NULL 组
    ]
