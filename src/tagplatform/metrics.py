"""质量指标推导（v2 重写，Task 9，承诺二③④的数据面）——审核与生成数据的只读聚合。

三个公开函数（session 级纯读，可独立于 API 复用）：
- `compute_metrics`：总览指标——pending 计数、review_status 终态分布
  （approved/rejected 按行计数、merged 取变更日志 merge 事件数）、生成
  产出通过率、evidence 覆盖率、生成任务统计；
- `confidence_distribution`：pre_asset_tag 的 confidence 等宽分桶；
- `tag_dir_coverage`：每目录的标签行数与已通过数（v1 tag_type_coverage
  更名——目录/标签拆双表后 tag_type 维度退役，目录成为覆盖统计的维度）。

语义约定（doc §6.2 指标口径 + v1 沿革）：
- 占比/通过率/覆盖率的**分母为 0 一律返回 None**（前端显示"暂无"），
  与计数为 0（正常返回 0.0，如 success_rate = 0/total）区分开；
- review_stats 按 review_status 终态分布（行即日志，doc §6.2——v1 按
  review_record.action 的动作分布退役）：approved/rejected 按
  pre_asset_tag.review_status 行数计；merged 取 tag_change_log
  (action='merge') 事件数（一次合入新建落一条日志；并入既有不落日志、
  行终态与 approve 相同均为 approved——merged 是事件数、不是行数）。
  三键 ratio 的分母**统一为 pre_asset_tag 总行数**：merged 是事件数、
  不入分母构成，与另两键同分母保证三键占比可直接对比；
- 生成产出通过率分母 = pre 已终审（approved/rejected）行，numerator =
  其中 approved 行（pending 不进分母；v2 行状态枚举无 merged——merge
  动作的终态归 approved；pre 表整体即生成产出，closed/open 都是生成
  模式，v1 的 origin='generated' 过滤随源表切换退役）；
- evidence 覆盖率分母 = pre 总行数（evidence_json 非空且非 "[]" 计入
  分子）；
- avg_duration_ms 只统计终态（succeeded/failed）且两个时间戳齐全的任务。

分层红线：本模块纯读不写、不得 import fastapi（设计文档 §9.4，与
review/service 同规）。
"""

from __future__ import annotations

from bisect import bisect_right
from datetime import datetime, timezone

from sqlalchemy import case, func
from sqlalchemy.orm import Session

from tagplatform.db.models import (
    PreAssetTag,
    TagCatalog,
    TagChangeLog,
    TagDefine,
    TagGenerationTask,
)

# 已终审的行状态（生成产出通过率的分母集合；pending 不计入。v2 行状态
# 枚举只有 pending/approved/rejected——v1 的 merged 行状态退役：merge 是
# 审核动作而非行状态，其行终态归 approved，doc §6.2）
_TERMINAL_REVIEW_STATUSES = ("approved", "rejected")

# 终态任务状态（时长均值只统计有始有终的任务；queued/running 无 finished_at）
_TERMINAL_TASK_STATUSES = ("succeeded", "failed")

# review_stats 展示键（approved/rejected = pre 行状态计数；merged = 变更
# 日志 merge 事件计数——计数源差异见模块 docstring 口径注释）
_REVIEW_STAT_KEYS = ("approved", "rejected", "merged")

# tag_dir_coverage：tag_id 与 candidate_dir_id 都解析不出目录时的兜底桶
# （生成端自由候选尚未归置——candidate_dir_id 审核合并时才赋值，doc §6.4）
_UNPLACED_DIR_NAME = "未归置"


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
    # pre 总行数：review_stats 三键 ratio 的统一分母（merged 事件数不构成
    # 独立分母、也不加入分母，见模块 docstring 口径注释）
    total_pre = session.query(func.count(PreAssetTag.id)).scalar()

    # pending 计数：pre 表全部 pending 行（closed/open 都是生成产出，不分源）
    pending_count = (
        session.query(func.count(PreAssetTag.id))
        .filter(PreAssetTag.review_status == "pending")
        .scalar()
    )

    # review_status 终态分布（doc §6.2）：行状态分组一次取出（approved/
    # rejected 行数），merged 另取变更日志 merge 事件数；三键 ratio 分母
    # 统一 = total_pre
    status_counts = dict(
        session.query(PreAssetTag.review_status, func.count(PreAssetTag.id))
        .group_by(PreAssetTag.review_status)
        .all()
    )
    merge_events = (
        session.query(func.count(TagChangeLog.log_id))
        .filter(TagChangeLog.action == "merge")
        .scalar()
    )
    counts = {
        "approved": status_counts.get("approved", 0),
        "rejected": status_counts.get("rejected", 0),
        "merged": merge_events,
    }
    review_stats = {
        key: {"count": counts[key], "ratio": _ratio(counts[key], total_pre)}
        for key in _REVIEW_STAT_KEYS
    }

    # 生成产出通过率：pre 已终审行中 approved 占比（公式沿 v1，分母 0 → None）
    generated_terminal = sum(
        status_counts.get(status, 0) for status in _TERMINAL_REVIEW_STATUSES
    )
    generated_pass_rate = _ratio(counts["approved"], generated_terminal)

    # evidence 覆盖率：evidence_json 非空（非 NULL/""）且非 "[]" 的行占比
    covered_tags = (
        session.query(func.count(PreAssetTag.id))
        .filter(
            PreAssetTag.evidence_json.isnot(None),
            PreAssetTag.evidence_json != "",
            PreAssetTag.evidence_json != "[]",
        )
        .scalar()
    )
    evidence_coverage = _ratio(covered_tags, total_pre)

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
            session.query(
                TagGenerationTask.created_at, TagGenerationTask.finished_at
            )
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


def tag_dir_coverage(session: Session) -> list[dict]:
    """每目录的标签行数与已通过数（承诺二④），按 total 降序。

    目录归属三级（doc §6.4 归置时序）：行带 tag_id → tag_define.dir_id →
    目录名（tag_id 优先——合入新建的行 tag_id/candidate_dir_id 并存时按
    并入的定义目录归属）；候选行（tag_id 空）→ candidate_dir_id → 目录名
    （审核 merge 入库时赋值）；两者皆无（生成端尚未归置的自由候选）→
    "未归置"桶。approved = 行 review_status=='approved' 计数。同 total
    按 dir_name 升序决胜（确定性输出）。

    计数在 SQL 按 (tag_id, candidate_dir_id) 分组聚合（组合数有限），目录
    名解析在应用层做（tag_id→define→dir 两跳映射不值得三表 join）。
    """
    grouped = (
        session.query(
            PreAssetTag.tag_id,
            PreAssetTag.candidate_dir_id,
            func.count(PreAssetTag.id),
            func.sum(case((PreAssetTag.review_status == "approved", 1),
                          else_=0)),
        )
        .group_by(PreAssetTag.tag_id, PreAssetTag.candidate_dir_id)
        .all()
    )
    if not grouped:  # 空库 → 空列表（"未归置"桶仅在有未归属行时出现）
        return []
    tag_to_dir = dict(session.query(TagDefine.tag_id, TagDefine.dir_id).all())
    dir_names = dict(session.query(TagCatalog.dir_id, TagCatalog.dir_name).all())
    totals: dict[str, int] = {}
    approveds: dict[str, int] = {}
    for tag_id, candidate_dir_id, total, approved in grouped:
        if tag_id is not None:
            dir_id = tag_to_dir.get(tag_id)
        else:
            dir_id = candidate_dir_id
        # dir_id 解析不出目录名（孤儿外键防御）与两者皆无同样落"未归置"
        if dir_id is not None:
            dir_name = dir_names.get(dir_id, _UNPLACED_DIR_NAME)
        else:
            dir_name = _UNPLACED_DIR_NAME
        totals[dir_name] = totals.get(dir_name, 0) + total
        approveds[dir_name] = approveds.get(dir_name, 0) + int(approved or 0)
    return [
        {"dir_name": name, "total": total, "approved": approveds[name]}
        for name, total in sorted(totals.items(),
                                  key=lambda item: (-item[1], item[0]))
    ]
