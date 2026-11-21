"""审核域编排：审核操作（单条/批量）与审核列表/筛选。

用例清单（原 api/routers/review.py 的内嵌查询装配与批量循环，Task 11 原样
下沉；列表分页/筛选本就 SQL 侧完成，行批键 task_id 列在库已久——本域端点
不在四层重构 Task 12 白名单内，零改动）：

- `submit_review`       单条审核四动作（apply_review 服务调用 + 行摘要装配）
- `submit_review_batch` 批量审核逐条独立执行（失败不中断、per-item detail）
- `list_review_queue`   审核中心列表（七筛选参数、confidence 升序、分页）
- `get_review_filters`  筛选下拉（库内 tag_types/dirs + 契约固定枚举）

错误映射随逻辑下沉本层原样保留：单条 404（行不存在）/409（ConflictError：
已被处理或 merge/modify 撞部分唯一索引）/422（ValueError：merge 载荷缺坏、
modify 空值超长；非法 action 由请求模型 Literal 在接入层 422）；批量
per-item 失败不升级 500（防御性 IntegrityError 兜底 + rollback）。
"""

from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import nullslast, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tagplatform.capabilities.storage.models import MediaAsset, PreAssetTag
from tagplatform.orchestration._shared import (
    AssetTagOut,
    EvidenceOut,
    _parse_evidence,
    _result_out,
    _tag_info,
)
from tagplatform.services.generation.persist import SYSTEM_REVIEWER
from tagplatform.services.generation.review import ConflictError, apply_review
from tagplatform.services.tags.library import try_load_library


# ---------- 请求/响应模型 ----------

class BatchReviewItemOut(BaseModel):
    """批量结果单条：ok=False 时 detail 携带失败原因（逐条独立不中断）。"""

    id: int
    ok: bool
    detail: str | None = None


class BatchReviewResponse(BaseModel):
    """批量审核响应：与请求 ids 等长、同序。"""

    results: list[BatchReviewItemOut]


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
    # 挂载粒度与归属目录：目录的 applicable_scope 决定挂载层级
    target_type: str = "content"
    dir_id: str | None = None
    dir_name: str | None = None
    dir_path: str | None = None
    evidence: list[EvidenceOut] = []
    task_id: str | None = None
    reviewer_id: str | None = None
    review_comment: str | None = None


class ReviewItemsResponse(BaseModel):
    """审核列表响应：当前页行 + 过滤后总数 + 页码（前端分页用）。"""

    items: list[ReviewItemOut]
    total: int
    page: int


class ReviewDirOption(BaseModel):
    """筛选/选择用的目录选项（来自 JSON 标签库）。"""

    dir_id: str
    dir_name: str
    path: str
    dir_type: str
    applicable_scope: str


class ReviewFiltersOut(BaseModel):
    """筛选下拉选项：tag_types 为标签库内去重，dirs 为目录树，
    statuses/modes 为契约固定枚举。"""

    tag_types: list[str]
    statuses: list[str]
    modes: list[str]
    dirs: list[ReviewDirOption] = []


# ---------- 用例 ----------

def submit_review(
    session: Session,
    asset_tag_id: int,
    action: Literal["approve", "reject", "modify", "merge"],
    reviewer: str,
    payload: dict | None,
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
        row = apply_review(session, asset_tag_id, action,
                           reviewer, payload)
    except ConflictError as exc:
        # 已被处理（重复提交/并发抢先）：409，detail 即服务层消息
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        # merge 载荷非法 / modify 空值等：422
        raise HTTPException(status_code=422, detail=str(exc))
    info = _tag_info([row])
    return AssetTagOut(**_result_out(row, info).model_dump(),
                       created_at=row.created_at)


def submit_review_batch(
    session: Session,
    ids: list[int],
    action: Literal["approve", "reject", "modify", "merge"],
    reviewer: str,
    payload: dict | None,
) -> BatchReviewResponse:
    """批量审核：逐条独立执行（服务层逐条提交），未知行/冲突/载荷失败/
    索引冲突不中断其余条目——失败原因进 per-item detail（Review Focus 4）；
    action 级非法由请求模型 422 整请求拒绝（零部分执行）。"""
    results: list[BatchReviewItemOut] = []
    for asset_tag_id in ids:
        try:
            apply_review(session, asset_tag_id, action,
                         reviewer, payload)
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


def list_review_queue(
    session: Session,
    status: str | None,
    confidence_min: float | None,
    confidence_max: float | None,
    tag_type: str | None,
    mode: str | None,
    asset_id: str | None,
    origin: str | None,
    dir_id: str | None,
    page: int,
    page_size: int,
) -> ReviewItemsResponse:
    """审核列表：待审核/已审行，七筛选参数自由组合。

    join media_asset（asset_title）/tag_catalog（库内标签名与类型）；mode 与
    origin 同义（行的产出来源即任务模式）；默认 confidence 升序（低分先审，
    无置信度行置后）；page/page_size 分页，total 为过滤后总数；未知筛选值
    一律空列表（不报错）。两个 outerjoin 均按对方主键 1:1 关联，无行放大。
    """
    library = try_load_library()
    query = (
        session.query(PreAssetTag, MediaAsset.title)
        .outerjoin(MediaAsset, PreAssetTag.asset_id == MediaAsset.asset_id)
    )
    # 审核中心只放"人要看"的行：库内命中（同名 / 疑似重复被判重）由后端自动
    # 判定——同名的直接忽略，判重的把模型原词记成库内标签别名——这两类都不
    # 该出现在这里（它们不是待审候选，也没有可审的标签属性）。留在表里是因为
    # `GET /assets/{id}/tags` 的"已通过标签"要读这些行。
    query = query.filter(or_(
        PreAssetTag.reviewer_id.is_(None),
        PreAssetTag.reviewer_id != SYSTEM_REVIEWER,
    ))
    if status is not None:
        query = query.filter(PreAssetTag.review_status == status)
    if confidence_min is not None:
        query = query.filter(PreAssetTag.confidence >= confidence_min)
    if confidence_max is not None:
        query = query.filter(PreAssetTag.confidence <= confidence_max)
    if tag_type is not None:
        # 类型取自 JSON 标签库（库外新词在库内没有类型，必然筛不中）
        if library is not None:
            tag_ids = [t.tag_id for t in library.tags() if t.tag_type == tag_type]
            query = query.filter(PreAssetTag.tag_id.in_(tag_ids or [""]))
        else:
            query = query.filter(PreAssetTag.tag_id.is_(None))
    if mode is not None:
        query = query.filter(PreAssetTag.origin == mode)
    if asset_id is not None:
        query = query.filter(PreAssetTag.asset_id == asset_id)
    if origin is not None:
        query = query.filter(PreAssetTag.origin == origin)
    if dir_id is not None:
        # 目录筛选按"含子目录"匹配：选中大类也能看到子目录下的行。
        # 库内行没有 candidate_dir_id，要按标签自身目录算——先把这两个目录集
        # 下的标签 id 找出来，与候选目录做 OR，避免在 Python 里二次过滤破坏分页。
        if library is not None:
            allowed_dirs = library.descendants([dir_id])
            library_tag_ids = [t.tag_id for t in library.tags_in_dirs([dir_id])]
            query = query.filter(or_(
                PreAssetTag.candidate_dir_id.in_(allowed_dirs or [""]),
                PreAssetTag.tag_id.in_(library_tag_ids or [""]),
            ))
        else:
            query = query.filter(PreAssetTag.candidate_dir_id == dir_id)

    total = query.count()
    rows = (
        query
        .order_by(nullslast(PreAssetTag.confidence.asc()), PreAssetTag.id.asc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    page_rows = [row for row, _title in rows]
    info = _tag_info(page_rows)

    items = []
    for row, asset_title in rows:
        definition = info.get(row.tag_id or "", {})
        dir_id = row.candidate_dir_id or definition.get("dir_id")
        directory = library.directory(dir_id) if library is not None else None
        path = library.path_of(dir_id) if (library is not None and dir_id) else []
        items.append(ReviewItemOut(
            id=row.id,
            asset_id=row.asset_id,
            asset_title=asset_title,
            # 展示解析 candidate_name 优先、库内标签回退
            # 三级回退同 _shared._result_out：历史行的 tag_id 不在 JSON
            # 库里时显示 id，而不是空白
            tag_name=(row.candidate_name or definition.get("tag_name")
                      or row.tag_id or ""),
            origin=row.origin,
            mode=row.origin,
            tag_type=definition.get("tag_type"),
            confidence=row.confidence,
            relevance=row.relevance_score,
            review_status=row.review_status,
            target_type=row.target_type,
            dir_id=dir_id,
            dir_name=directory.dir_name if directory else None,
            dir_path="/".join(path) if path else None,
            evidence=_parse_evidence(row.evidence_json),
            task_id=row.task_id,
            reviewer_id=row.reviewer_id,
            review_comment=row.review_comment,
        ))
    return ReviewItemsResponse(items=items, total=total, page=page)


def get_review_filters(session: Session) -> ReviewFiltersOut:
    """筛选下拉选项：tag_types/dirs 来自 JSON 标签库（排序保证确定性）；
    statuses/modes 为契约固定枚举，不查库。"""
    library = try_load_library()
    tag_types = sorted({t.tag_type for t in library.tags()}) if library else []
    dirs = []
    if library is not None:
        dirs = [
            ReviewDirOption(
                dir_id=directory.dir_id,
                dir_name=directory.dir_name,
                path="/".join(library.path_of(directory.dir_id)),
                dir_type=directory.dir_type,
                applicable_scope=directory.applicable_scope,
            )
            for directory in library.dirs()
        ]
    return ReviewFiltersOut(
        tag_types=tag_types,
        statuses=list(_REVIEW_STATUSES),
        modes=list(_GENERATION_MODES),
        dirs=dirs,
    )
