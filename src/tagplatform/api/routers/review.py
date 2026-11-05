"""`/api/v1/asset-tags` 审核操作与 `/api/v1/review` 审核列表路由（v2 行即日志）。

薄路由层：业务规则全部在 tagplatform.review 服务（四动作行操作语义、前置
校验、服务内提交），router 只做协议转换与错误映射：
- pre 行不存在 → 404（router 先行查库；服务层 ValueError 为直接调用方兜底）；
- ConflictError（行已终态——重复提交/并发抢先；或挂载/并入落库撞唯一
  索引——同资产已挂该标签）→ 409；
- ValueError（merge 载荷缺/坏/两支混用、modify 字段空/超长/类型错、
  open 自由候选直接 approve/modify）→ 422；
- 非法 action 由请求模型 Literal 声明校验直接 422（action 级失败整请求
  拒绝，单条与批量一致）。
批量端点仅 approve/reject（modify/merge 载荷逐行各异，无批量语义），逐条
独立执行（服务层逐条提交），单条失败（未知行/冲突/载荷非法/索引冲突）
不中断其余条目，失败原因进 per-item detail（Review Focus 4）。reviewer 取
会话用户名。响应复用 generation.py 的 pre 行输出形状（PreTagOut 及其组装
辅助——同包内复用，避免形状漂移）。

审核列表（审核中心页面数据源，doc §3.5 pre_asset_tag）：
- GET /review/items：过滤参数 status/confidence_min/confidence_max/dir/
  origin/asset_id/page/page_size（dir=目录名精确匹配，define 目录或候选
  目录任一命中；v1 mode/tag_type 过滤随词表退役）。outerjoin
  MediaAsset/TagDefine/TagCatalog（define 目录与候选目录两个别名）——
  展示解析：tag_name = candidate_name 或 define 名，dir_name = define
  目录名 or candidate_dir 目录名 or None；排序 confidence 升序
  nulls-last（低分先审，无置信度行置后）+ id 决胜。v1 的
  TagGenerationTask 外连不再需要（mode 列退役，task_id 直取行内列）；
- GET /review/filters：{statuses, origins, dirs}——statuses/origins 契约
  固定枚举，dirs 为 tag_catalog 库内目录名去重升序。
"""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import nullslast
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from tagplatform.api import get_session
from tagplatform.api.depends import get_current_user
from tagplatform.api.routers.generation import (
    EvidenceOut,
    PreTagOut,
    _define_names,
    _parse_evidence,
    _pre_tag_out,
)
from tagplatform.db.models import (
    MediaAsset,
    PreAssetTag,
    TagCatalog,
    TagDefine,
    UserAccount,
)
from tagplatform.review import ConflictError, apply_review

router = APIRouter(prefix="/api/v1")


class ReviewRequest(BaseModel):
    """单条审核请求：action 合法取值由 Literal 声明（非法 → 422）。

    payload 按动作（doc §6.2）：approve/reject `{comment?}`；modify
    `{candidate_name?, candidate_description?, comment?}`（更新后直接
    approve）；merge `{merge_to_tag_id}` 或 `{new_tag_name, new_dir_id,
    new_description?, comment?}`（两支互斥）。
    """

    action: Literal["approve", "reject", "modify", "merge"]
    payload: dict | None = None


class BatchReviewRequest(BaseModel):
    """批量审核请求：ids 非空列表（空列表 → 422）；仅 approve/reject
    （modify/merge 载荷逐行各异，无批量语义）。"""

    ids: list[int] = Field(min_length=1)
    action: Literal["approve", "reject"]
    payload: dict | None = None


class BatchReviewItemOut(BaseModel):
    """批量结果单条：ok=False 时 detail 携带失败原因（逐条独立不中断）。"""

    id: int
    ok: bool
    detail: str | None = None


class BatchReviewResponse(BaseModel):
    """批量审核响应：与请求 ids 等长、同序。"""

    results: list[BatchReviewItemOut]


@router.post("/asset-tags/{asset_tag_id}/review", response_model=PreTagOut)
def review_asset_tag(
    asset_tag_id: int,
    body: ReviewRequest,
    session: Session = Depends(get_session),
    user: UserAccount = Depends(get_current_user),
) -> PreTagOut:
    """单条审核：四动作行操作即时落库，返回更新后的 pre 行摘要（PreTagOut）。

    404 未知行；409 行已终态（Review Focus 1 并发保护）或挂载/并入落库撞
    唯一索引（"该资产已挂此标签"）；422 载荷非法（merge 缺/坏/两支混用、
    modify 字段空/超长、open 自由候选直接 approve/modify）或非法 action
    （Literal 声明校验）。
    """
    if session.get(PreAssetTag, asset_tag_id) is None:
        raise HTTPException(status_code=404,
                            detail=f"标签行不存在：{asset_tag_id}")
    try:
        row = apply_review(session, asset_tag_id, body.action,
                           user.username, body.payload)
    except ConflictError as exc:
        # 已终态/唯一索引冲突：409，detail 即服务层消息
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        # merge 载荷非法 / modify 字段校验 / 自由候选直接通过：422
        raise HTTPException(status_code=422, detail=str(exc))
    return _pre_tag_out(row, _define_names(session, [row]))


@router.post("/asset-tags/batch-review", response_model=BatchReviewResponse)
def batch_review_asset_tags(
    body: BatchReviewRequest,
    session: Session = Depends(get_session),
    user: UserAccount = Depends(get_current_user),
) -> BatchReviewResponse:
    """批量审核（仅 approve/reject）：逐条独立执行（服务层逐条提交），未知
    行/冲突/载荷失败不中断其余条目——失败原因进 per-item detail（Review
    Focus 4）；action 级非法由请求模型 422 整请求拒绝（零部分执行）。"""
    results: list[BatchReviewItemOut] = []
    for pre_tag_id in body.ids:
        try:
            apply_review(session, pre_tag_id, body.action,
                         user.username, body.payload)
            results.append(BatchReviewItemOut(id=pre_tag_id, ok=True))
        except (ConflictError, ValueError, IntegrityError) as exc:
            # 服务层已把 IntegrityError 转译为 ConflictError（含回滚），
            # 此处再捕 IntegrityError 为防御性冗余——兜底服务层未来改动
            # 直接上抛索引异常的情形，保证批量 per-item 失败不升级 500；
            # rollback 撤销本条可能残留的未提交变更，后续条目继续
            session.rollback()
            results.append(BatchReviewItemOut(id=pre_tag_id, ok=False,
                                              detail=str(exc)))
    return BatchReviewResponse(results=results)


# ---------- 审核列表与筛选（审核中心页面数据源） ----------

# 契约固定枚举（statuses/origins 不查库，与数据模型 CHECK 注记的取值集合同源）
_REVIEW_STATUSES = ["pending", "approved", "rejected"]
_REVIEW_ORIGINS = ["closed", "open"]


class ReviewItemOut(BaseModel):
    """审核列表单行（pre_asset_tag）。

    展示解析：tag_name = candidate_name（open 候选/审核 modify 落在行内的
    展示字段）或 tag_define.tag_name（closed 行/已合并行）；dir_name =
    define 目录名 or candidate_dir 目录名 or None（tag_id 优先——合并入库
    后目录归属以 define 为准）。
    """

    id: int
    asset_id: str
    asset_title: str | None = None
    tag_name: str
    tag_id: str | None = None
    candidate_name: str | None = None
    origin: str
    dir_name: str | None = None
    confidence: float | None = None
    relevance: float | None = None
    review_status: str
    evidence: list[EvidenceOut] = []
    task_id: str | None = None


class ReviewItemsResponse(BaseModel):
    """审核列表响应：当前页行 + 过滤后总数 + 页码（前端分页用）。"""

    items: list[ReviewItemOut]
    total: int
    page: int


class ReviewFiltersOut(BaseModel):
    """筛选下拉选项：statuses/origins 为契约固定枚举，dirs 为 tag_catalog
    库内目录名去重（排序保证确定性下拉顺序）。"""

    statuses: list[str]
    origins: list[str]
    dirs: list[str]


@router.get("/review/items", response_model=ReviewItemsResponse)
def list_review_items(
    status: str | None = Query(None),
    confidence_min: float | None = Query(None),
    confidence_max: float | None = Query(None),
    dir: str | None = Query(None),
    origin: str | None = Query(None),
    asset_id: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1),
    session: Session = Depends(get_session),
) -> ReviewItemsResponse:
    """审核列表：待审核/已审 pre 行，过滤参数自由组合。

    outerjoin media_asset（asset_title）/tag_define（定义名）/tag_catalog
    两个别名（define 目录与候选目录——dir_name 解析与 dir 过滤共用）；
    默认 confidence 升序（低分先审，无置信度行置后——不挡低分行审核）、
    id 决胜保证确定性；page/page_size 分页，total 为过滤后总数；未知筛选
    值一律空列表（不报错）。各 outerjoin 均按对方主键 1:1 关联，无行放大，
    count 直接可用。
    """
    define_dir = aliased(TagCatalog)  # tag_id → define → 所在目录
    candidate_dir = aliased(TagCatalog)  # candidate_dir_id → 候选目录
    query = (
        session.query(
            PreAssetTag,
            MediaAsset.title,
            TagDefine.tag_name,
            define_dir.dir_name,
            candidate_dir.dir_name,
        )
        .outerjoin(MediaAsset, PreAssetTag.asset_id == MediaAsset.asset_id)
        .outerjoin(TagDefine, PreAssetTag.tag_id == TagDefine.tag_id)
        .outerjoin(define_dir, TagDefine.dir_id == define_dir.dir_id)
        .outerjoin(candidate_dir,
                   PreAssetTag.candidate_dir_id == candidate_dir.dir_id)
    )
    if status is not None:
        query = query.filter(PreAssetTag.review_status == status)
    if confidence_min is not None:
        query = query.filter(PreAssetTag.confidence >= confidence_min)
    if confidence_max is not None:
        query = query.filter(PreAssetTag.confidence <= confidence_max)
    if dir is not None:
        # 目录名精确匹配：define 目录或候选目录任一命中（行形态二选一）
        query = query.filter(
            (define_dir.dir_name == dir) | (candidate_dir.dir_name == dir))
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

    items = [
        ReviewItemOut(
            id=row.id,
            asset_id=row.asset_id,
            asset_title=asset_title,
            # tag_name：candidate_name 优先（open 候选/审核 modify 落在行内
            # 的展示字段），否则回退 define 名（closed 行/已合并行）
            tag_name=row.candidate_name or define_name or "",
            tag_id=row.tag_id,
            candidate_name=row.candidate_name,
            origin=row.origin,
            # dir_name：tag_id → define → 目录名优先，候选目录回退
            dir_name=define_dir_name or candidate_dir_name,
            confidence=row.confidence,
            relevance=row.relevance_score,
            review_status=row.review_status,
            evidence=_parse_evidence(row.evidence_json),
            task_id=row.task_id,
        )
        for row, asset_title, define_name, define_dir_name, candidate_dir_name
        in rows
    ]
    return ReviewItemsResponse(items=items, total=total, page=page)


@router.get("/review/filters", response_model=ReviewFiltersOut)
def get_review_filters(
    session: Session = Depends(get_session),
) -> ReviewFiltersOut:
    """筛选下拉选项：dirs 为 tag_catalog 库内目录名去重升序（真实数据推导，
    空库空列表）；statuses/origins 为契约固定枚举，不查库。"""
    dirs = [
        row[0]
        for row in session.query(TagCatalog.dir_name)
        .distinct()
        .order_by(TagCatalog.dir_name)
    ]
    return ReviewFiltersOut(
        statuses=list(_REVIEW_STATUSES),
        origins=list(_REVIEW_ORIGINS),
        dirs=dirs,
    )
