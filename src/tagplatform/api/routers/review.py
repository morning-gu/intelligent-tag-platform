"""`/api/v1/asset-tags` 审核操作路由：单条审核与批量审核（Task 4）。

薄路由层：业务规则全部在 tagplatform.review 服务（四动作语义、前置校验、
记录写入、服务内提交），router 只做协议转换与错误映射：
- 行不存在 → 404（router 先行查库；服务层 ValueError 为直接调用方兜底）；
- ConflictError（行已被处理，重复提交/并发抢先）→ 409；
- ValueError（merge 载荷缺/坏 merge_to、modify 空值）→ 422；
- 非法 action 由请求模型 Literal 声明校验直接 422（action 级失败整请求
  拒绝，单条与批量一致）。
批量端点逐条独立执行（服务层逐条提交），单条失败（未知行/冲突/载荷非法）
不中断其余条目，失败原因进 per-item detail（Review Focus 4）。reviewer
取会话用户名；登录门槛由 create_app include 时的 protected_deps 统一挂载。
响应复用 generation.py 的行输出形状（AssetTagOut 及其组装辅助——同包内
复用，避免形状漂移）。
"""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from tagplatform.api import get_session
from tagplatform.api.depends import get_current_user
from tagplatform.api.routers.generation import (
    AssetTagOut,
    _catalog_names,
    _result_out,
)
from tagplatform.db.models import AssetTag, UserAccount
from tagplatform.review import ConflictError, apply_review

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


class BatchReviewItemOut(BaseModel):
    """批量结果单条：ok=False 时 detail 携带失败原因（逐条独立不中断）。"""

    id: int
    ok: bool
    detail: str | None = None


class BatchReviewResponse(BaseModel):
    """批量审核响应：与请求 ids 等长、同序。"""

    results: list[BatchReviewItemOut]


@router.post("/asset-tags/{asset_tag_id}/review", response_model=AssetTagOut)
def review_asset_tag(
    asset_tag_id: int,
    body: ReviewRequest,
    session: Session = Depends(get_session),
    user: UserAccount = Depends(get_current_user),
) -> AssetTagOut:
    """单条审核：四动作即时落库，返回更新后的行摘要（与 AssetTagOut 同形）。

    404 未知行；409 行已被处理（Review Focus 1 并发保护）；422 载荷非法
    （merge 缺/坏 merge_to、modify 空值）或非法 action（Literal 声明校验）。
    """
    if session.get(AssetTag, asset_tag_id) is None:
        raise HTTPException(status_code=404,
                            detail=f"标签行不存在：{asset_tag_id}")
    try:
        row = apply_review(session, asset_tag_id, body.action,
                           user.username, body.payload)
    except ConflictError as exc:
        # 已被处理（重复提交/并发抢先）：409，detail 即服务层消息
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        # merge 载荷非法 / modify 空值等：422
        raise HTTPException(status_code=422, detail=str(exc))
    names = _catalog_names(session, [row])
    return AssetTagOut(
        **_result_out(row, names).model_dump(),
        tag_id=row.tag_id,
        candidate_name=row.candidate_name,
    )


@router.post("/asset-tags/batch-review", response_model=BatchReviewResponse)
def batch_review_asset_tags(
    body: BatchReviewRequest,
    session: Session = Depends(get_session),
    user: UserAccount = Depends(get_current_user),
) -> BatchReviewResponse:
    """批量审核：逐条独立执行（服务层逐条提交），未知行/冲突/载荷失败
    不中断其余条目——失败原因进 per-item detail（Review Focus 4）；
    action 级非法由请求模型 422 整请求拒绝（零部分执行）。"""
    results: list[BatchReviewItemOut] = []
    for asset_tag_id in body.ids:
        try:
            apply_review(session, asset_tag_id, body.action,
                         user.username, body.payload)
            results.append(BatchReviewItemOut(id=asset_tag_id, ok=True))
        except (ConflictError, ValueError) as exc:
            # 回滚保险：服务层校验前置，正常路径无未提交变更；此处防御
            # 提交中途异常（如唯一索引冲突）污染后续条目的事务
            session.rollback()
            results.append(BatchReviewItemOut(id=asset_tag_id, ok=False,
                                              detail=str(exc)))
    return BatchReviewResponse(results=results)
