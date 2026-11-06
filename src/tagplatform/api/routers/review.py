"""`/api/v1/asset-tags` 审核操作与 `/api/v1/review` 审核列表路由（Task 4/5）。

薄路由层：业务规则全部在 tagplatform.review 服务（四动作语义、前置校验、
记录写入、服务内提交），router 只做协议转换与错误映射：
- 行不存在 → 404（router 先行查库；服务层 ValueError 为直接调用方兜底）；
- ConflictError（行已被处理，重复提交/并发抢先；或 merge/modify 落库撞
  部分唯一索引——同资产已挂该目录标签/已有同名候选）→ 409；
- ValueError（merge 载荷缺/坏 merge_to、modify 空值/空串/超长）→ 422；
- 非法 action 由请求模型 Literal 声明校验直接 422（action 级失败整请求
  拒绝，单条与批量一致）。
批量端点逐条独立执行（服务层逐条提交），单条失败（未知行/冲突/载荷非法/
索引冲突）不中断其余条目，失败原因进 per-item detail（Review Focus 4）。
reviewer 取会话用户名；登录门槛由 create_app include 时的 protected_deps
统一挂载。响应复用 generation.py 的行输出形状（AssetTagOut 及其组装
辅助——同包内复用，避免形状漂移）。

审核列表（Task 5，审核中心页面数据源）：
- GET /review/items：join media_asset（asset_title）/tag_catalog（目录名
  与类型）/generation_task（mode），七筛选参数（status/confidence_min/
  confidence_max/tag_type/mode/asset_id/origin），默认 confidence 升序
  （低分先审），page/page_size 分页，响应 {items, total, page}；行
  tag_name/tag_type 展示解析统一为 candidate_* 优先、目录回退——行带
  非空 candidate_name（审核 modify 落在行内的展示字段）取候选名/类型，
  否则带 tag_id（catalog 行或已并入目录的 merged 行）取 tag_catalog 字典
  （merged 行 candidate_* 已清空自然回退目录名）；
- GET /review/filters：前端筛选下拉（tag_types 库内去重 + statuses/modes
  契约固定枚举）。
"""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import nullslast
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tagplatform.api import get_session
from tagplatform.api.depends import get_current_user
from tagplatform.api.routers.generation import (
    AssetTagOut,
    EvidenceOut,
    _catalog_names,
    _parse_evidence,
    _result_out,
)
from tagplatform.db.models import (
    MediaAsset,
    PreAssetTag,
    TagCatalog,
    UserAccount,
)
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

    404 未知行；409 行已被处理（Review Focus 1 并发保护）或 merge/modify
    落库撞部分唯一索引（"该资产已挂此标签"）；422 载荷非法（merge 缺/坏
    merge_to、modify 空值/空串/超长）或非法 action（Literal 声明校验）。
    """
    if session.get(PreAssetTag, asset_tag_id) is None:
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
    """批量审核：逐条独立执行（服务层逐条提交），未知行/冲突/载荷失败/
    索引冲突不中断其余条目——失败原因进 per-item detail（Review Focus 4）；
    action 级非法由请求模型 422 整请求拒绝（零部分执行）。"""
    results: list[BatchReviewItemOut] = []
    for asset_tag_id in body.ids:
        try:
            apply_review(session, asset_tag_id, body.action,
                         user.username, body.payload)
            results.append(BatchReviewItemOut(id=asset_tag_id, ok=True))
        except (ConflictError, ValueError, IntegrityError) as exc:
            # 服务层已把 IntegrityError 转译为 ConflictError（含回滚），
            # 此处再捕 IntegrityError 为防御性冗余——兜底服务层未来改动
            # 直接上抛索引异常的情形，保证批量 per-item 失败不升级 500；
            # rollback 撤销本条可能残留的未提交变更，后续条目继续
            session.rollback()
            results.append(BatchReviewItemOut(id=asset_tag_id, ok=False,
                                              detail=str(exc)))
    return BatchReviewResponse(results=results)


# ---------- 审核列表与筛选（Task 5，审核中心页面数据源） ----------

# 契约固定枚举（statuses/modes 不查库，与数据模型注释的取值集合同源）
_REVIEW_STATUSES = ["pending", "approved", "rejected"]
_GENERATION_MODES = ["closed", "open"]


class ReviewItemOut(BaseModel):
    """审核列表单行。

    tag_name/tag_type 展示解析 candidate_* 优先、目录回退：行带非空
    candidate_name（审核 modify 落在行内的展示字段，改了什么显示什么）
    → 候选名；否则带 tag_id（命中库内标签的行）→ tag_catalog 字典名。
    """

    id: int
    asset_id: str
    asset_title: str | None = None
    tag_name: str
    origin: str
    mode: str | None = None
    tag_type: str | None = None
    confidence: float | None = None
    relevance: float | None = None
    review_status: str
    # 挂载粒度：本期恒为 content
    target_type: str = "content"
    evidence: list[EvidenceOut] = []
    task_id: str | None = None
    reviewer_id: str | None = None
    review_comment: str | None = None


class ReviewItemsResponse(BaseModel):
    """审核列表响应：当前页行 + 过滤后总数 + 页码（前端分页用）。"""

    items: list[ReviewItemOut]
    total: int
    page: int


class ReviewFiltersOut(BaseModel):
    """筛选下拉选项：tag_types 为 tag_catalog 库内去重，statuses/modes
    为契约固定枚举。"""

    tag_types: list[str]
    statuses: list[str]
    modes: list[str]


@router.get("/review/items", response_model=ReviewItemsResponse)
def list_review_items(
    status: str | None = Query(None),
    confidence_min: float | None = Query(None),
    confidence_max: float | None = Query(None),
    tag_type: str | None = Query(None),
    mode: str | None = Query(None),
    asset_id: str | None = Query(None),
    origin: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1),
    session: Session = Depends(get_session),
) -> ReviewItemsResponse:
    """审核列表：待审核/已审行，七筛选参数自由组合。

    join media_asset（asset_title）/tag_catalog（库内标签名与类型）；mode 与
    origin 同义（行的产出来源即任务模式）；默认 confidence 升序（低分先审，
    无置信度行置后）；page/page_size 分页，total 为过滤后总数；未知筛选值
    一律空列表（不报错）。两个 outerjoin 均按对方主键 1:1 关联，无行放大。
    """
    query = (
        session.query(
            PreAssetTag, MediaAsset.title, TagCatalog.tag_type,
        )
        .outerjoin(MediaAsset, PreAssetTag.asset_id == MediaAsset.asset_id)
        .outerjoin(TagCatalog, PreAssetTag.tag_id == TagCatalog.tag_id)
    )
    if status is not None:
        query = query.filter(PreAssetTag.review_status == status)
    if confidence_min is not None:
        query = query.filter(PreAssetTag.confidence >= confidence_min)
    if confidence_max is not None:
        query = query.filter(PreAssetTag.confidence <= confidence_max)
    if tag_type is not None:
        # 本期行内不再存候选类型：类型恒取自库内标签（库外新词无类型）
        query = query.filter(TagCatalog.tag_type == tag_type)
    if mode is not None:
        query = query.filter(PreAssetTag.origin == mode)
    if asset_id is not None:
        query = query.filter(PreAssetTag.asset_id == asset_id)
    if origin is not None:
        query = query.filter(PreAssetTag.origin == origin)

    total = query.count()
    rows = (
        query
        .order_by(nullslast(PreAssetTag.confidence.asc()), PreAssetTag.id.asc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    # 库内标签名批量解析（限本页行集）
    tag_ids = {row.tag_id for row, _, _ in rows if row.tag_id}
    names = (
        {t.tag_id: t.tag_name
         for t in session.query(TagCatalog).filter(TagCatalog.tag_id.in_(tag_ids))}
        if tag_ids else {}
    )

    items = [
        ReviewItemOut(
            id=row.id,
            asset_id=row.asset_id,
            asset_title=asset_title,
            # 展示解析 candidate_name 优先、库内标签回退
            tag_name=row.candidate_name or names.get(row.tag_id, ""),
            origin=row.origin,
            mode=row.origin,
            tag_type=catalog_tag_type,
            confidence=row.confidence,
            relevance=row.relevance_score,
            review_status=row.review_status,
            target_type=row.target_type,
            evidence=_parse_evidence(row.evidence_json),
            task_id=row.task_id,
            reviewer_id=row.reviewer_id,
            review_comment=row.review_comment,
        )
        for row, asset_title, catalog_tag_type in rows
    ]
    return ReviewItemsResponse(items=items, total=total, page=page)


@router.get("/review/filters", response_model=ReviewFiltersOut)
def get_review_filters(
    session: Session = Depends(get_session),
) -> ReviewFiltersOut:
    """筛选下拉选项：tag_types 为 tag_catalog 库内去重（排序保证确定性
    下拉顺序）；statuses/modes 为契约固定枚举，不查库。"""
    tag_types = [
        row[0]
        for row in session.query(TagCatalog.tag_type)
        .distinct()
        .order_by(TagCatalog.tag_type)
    ]
    return ReviewFiltersOut(
        tag_types=tag_types,
        statuses=list(_REVIEW_STATUSES),
        modes=list(_GENERATION_MODES),
    )
