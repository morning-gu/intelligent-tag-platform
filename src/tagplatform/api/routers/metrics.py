"""`/api/v1/metrics` 质量指标路由（v2 重写，Task 9）。

薄路由层：推导逻辑全部在 tagplatform.metrics 核心模块（纯读、不 import
fastapi），router 只做协议转换——Session 依赖注入 + Pydantic 响应模型
（JSON 形状契约）。登录门槛由 create_app include 时的 protected_deps
统一挂载（同 review/video，本文件零认证代码）。

- GET /metrics/summary：审核推导质量指标总览（pending 计数、review_status
  终态分布三键 approved/rejected/merged、生成产出通过率、evidence 覆盖
  率、任务统计）——承诺二③；占比类字段分母为 0 时为 null（前端显示
  "暂无"）；
- GET /metrics/confidence-distribution：confidence 等宽分桶（buckets
  可调 1-100，默认 10）；
- GET /metrics/dir-coverage：每目录的标签行数与已通过数——承诺二④
  （v1 tag-type-coverage 端点随 tag_type 维度退役，更名换目录维度，
  doc §6.2 指标口径）。
"""

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from tagplatform.api import get_session
from tagplatform.metrics import (
    compute_metrics,
    confidence_distribution,
    tag_dir_coverage,
)

router = APIRouter(prefix="/api/v1")


class ReviewStatusStat(BaseModel):
    """单个审核状态键的计数与占比：approved/rejected 按 pre 行状态计数，
    merged 为变更日志 merge 事件数（不是行数）；占比分母统一 =
    pre_asset_tag 总行数（分母 0 → None）。"""

    count: int
    ratio: float | None


class ReviewStatsOut(BaseModel):
    """review_status 终态分布：approved/rejected/merged 三键各自的计数
    与占比（merged 是事件数、与另两键同分母，口径见 metrics 模块
    docstring）。"""

    approved: ReviewStatusStat
    rejected: ReviewStatusStat
    merged: ReviewStatusStat


class TaskStatsOut(BaseModel):
    """生成任务统计（success_rate 分母 = 全部任务；avg_duration_ms 仅
    终态且两时间戳齐全的任务均值，无 → None）。"""

    total: int
    succeeded: int
    failed: int
    success_rate: float | None
    avg_duration_ms: float | None


class MetricsSummaryOut(BaseModel):
    """质量指标总览响应（承诺二③）。"""

    pending_count: int
    review_stats: ReviewStatsOut
    generated_pass_rate: float | None
    evidence_coverage: float | None
    task_stats: TaskStatsOut


class ConfidenceBucketOut(BaseModel):
    """confidence 分桶单条：桶标签（如 "0.0-0.1"）与落入行数。"""

    bucket: str
    count: int


class DirCoverageOut(BaseModel):
    """目录覆盖单条：目录名、总行数、其中 approved 数（无目录归属的
    候选行归"未归置"桶）。"""

    dir_name: str
    total: int
    approved: int


@router.get("/metrics/summary", response_model=MetricsSummaryOut)
def get_metrics_summary(session: Session = Depends(get_session)) -> dict:
    """质量指标总览：pending / review_status 终态分布 / 生成产出通过率 /
    evidence 覆盖率 / 任务统计（推导语义见
    tagplatform.metrics.compute_metrics）。"""
    return compute_metrics(session)


@router.get("/metrics/confidence-distribution",
            response_model=list[ConfidenceBucketOut])
def get_confidence_distribution(
    buckets: int = Query(10, ge=1, le=100),
    session: Session = Depends(get_session),
) -> list[dict]:
    """confidence 分桶分布：pre_asset_tag 全部行（None confidence 除外）
    等宽分桶，恒返回全部桶（含 0 计数桶）。"""
    return confidence_distribution(session, buckets=buckets)


@router.get("/metrics/dir-coverage", response_model=list[DirCoverageOut])
def get_dir_coverage(session: Session = Depends(get_session)) -> list[dict]:
    """目录覆盖：每目录的总行数与已通过数，按 total 降序（行 tag_id 归属
    优先，候选行按 candidate_dir_id，两者皆无归"未归置"桶）。"""
    return tag_dir_coverage(session)
