"""`/api/v1/asset-tags` 审核操作与 `/api/v1/review` 审核列表薄路由（Task 4/5）。

接入层只做协议转换：请求模型声明校验（action 由 Literal 声明，非法 →
422 action 级失败整请求拒绝）→ 调编排 → 返回。四动作语义、批量逐条独立
执行、审核列表八筛选查询与行装配全部在 tagplatform.orchestration.review
（用例清单见该模块 docstring）。

错误映射随逻辑在编排层原样保留：行不存在 404（编排层先行查库；服务层
ValueError 为直接调用方兜底）、ConflictError（行已被处理/并发抢先/撞部分
唯一索引）→ 409、ValueError（merge 载荷缺坏/modify 空值超长）→ 422。
批量端点逐条独立执行不中断（Review Focus 4）。reviewer 取会话用户名；
登录门槛由 create_app include 时的 protected_deps 统一挂载。响应复用
_shared 的行输出形状（AssetTagOut 及装配三件套——同包内复用，避免形状漂移）。
"""

from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from tagplatform.api import get_session
from tagplatform.api.depends import get_current_user
from tagplatform.capabilities.storage.models import UserAccount
from tagplatform.orchestration import review as review_orch
from tagplatform.orchestration._shared import AssetTagOut
from tagplatform.orchestration.review import (
    BatchReviewResponse,
    ReviewFiltersOut,
    ReviewItemsResponse,
)

router = APIRouter(prefix="/api/v1")


class ReviewRequest(BaseModel):
    """单条审核请求：action 合法取值由 Literal 声明（非法 → 422）。"""

    action: Literal["approve", "reject", "modify", "merge"]
    payload: dict | None = None


class BatchReviewRequest(BaseModel):
    """批量审核请求：ids 非空列表（空列表 → 422）。"""

    ids: list[int] = Field(min_length=1)
    action: Literal["approve", "reject", "modify", "merge"]
    payload: dict | None = None


@router.post("/asset-tags/{asset_tag_id}/review", response_model=AssetTagOut)
def review_asset_tag(
    asset_tag_id: int,
    body: ReviewRequest,
    session: Session = Depends(get_session),
    user: UserAccount = Depends(get_current_user),
) -> AssetTagOut:
    """单条审核：四动作即时落库，返回更新后的行摘要（与 AssetTagOut 同形）。"""
    return review_orch.submit_review(
        session, asset_tag_id, body.action, user.username, body.payload)


@router.post("/asset-tags/batch-review", response_model=BatchReviewResponse)
def batch_review_asset_tags(
    body: BatchReviewRequest,
    session: Session = Depends(get_session),
    user: UserAccount = Depends(get_current_user),
) -> BatchReviewResponse:
    """批量审核：逐条独立执行，失败原因进 per-item detail（Review Focus 4）。"""
    return review_orch.submit_review_batch(
        session, body.ids, body.action, user.username, body.payload)


@router.get("/review/items", response_model=ReviewItemsResponse)
def list_review_items(
    status: str | None = Query(None),
    confidence_min: float | None = Query(None),
    confidence_max: float | None = Query(None),
    tag_type: str | None = Query(None),
    mode: str | None = Query(None),
    asset_id: str | None = Query(None),
    origin: str | None = Query(None),
    dir_id: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1),
    session: Session = Depends(get_session),
) -> ReviewItemsResponse:
    """审核列表：待审核/已审行，八筛选参数自由组合（低分先审）。"""
    return review_orch.list_review_queue(
        session, status, confidence_min, confidence_max, tag_type, mode,
        asset_id, origin, dir_id, page, page_size)


@router.get("/review/filters", response_model=ReviewFiltersOut)
def get_review_filters(
    session: Session = Depends(get_session),
) -> ReviewFiltersOut:
    """筛选下拉选项：tag_types/dirs 来自标签库，statuses/modes 契约枚举。"""
    return review_orch.get_review_filters(session)
