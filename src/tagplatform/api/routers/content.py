"""`/api/v1/content`、`/api/v1/assets`、`/api/v1/adapters` 路由：内容导入与资产查询。

薄路由层：适配（get_adapter().adapt）→ 导入（import_content）→ 响应模型，
业务规则全部在 tagplatform.adapters / content_import 与 DB 层，router 只做
协议转换与错误映射（未知 kind / 内容 JSON 校验失败 / video_root 非法 →
422；未知资产/上下文 → 404）。contexts 摘要不含 content_json 大字段，
完整 JSON 经 `GET /assets/{asset_id}/contexts/{context_id}` 取回。
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from tagplatform.adapters import get_adapter, list_adapters
from tagplatform.api import get_session
from tagplatform.content_import import import_content as import_content_svc
from tagplatform.db.models import MediaAsset, MediaContext
from tagplatform.schemas import ContentJSON

router = APIRouter(prefix="/api/v1")


# ---------- 响应模型 ----------

class ContentImportRequest(BaseModel):
    """内容导入请求体：源格式 kind + 可选视频根目录 + 原始内容 JSON dict。"""

    kind: str
    video_root: str | None = None
    data: dict


class ContentImportResponse(BaseModel):
    """导入响应：影片 asset_id + 本次新建 context_id + 场景 asset_id 列表。"""

    asset_id: str
    context_id: str
    scene_asset_ids: list[str]


class AssetOut(BaseModel):
    """资产列表项（media_asset 各列，均非大字段）。"""

    asset_id: str
    asset_type: str
    parent_id: str | None = None
    title: str
    duration_ms: int | None = None
    video_uri: str | None = None
    created_at: datetime | None = None


class ContextSummaryOut(BaseModel):
    """上下文摘要项：不含 content_json 大字段（完整 JSON 走专用端点取回）。"""

    context_id: str
    model_version: str | None = None
    recognition_status: str | None = None
    created_at: datetime | None = None


class AssetDetail(AssetOut):
    """资产详情：在列表字段之上附其全部 media_context 摘要。"""

    contexts: list[ContextSummaryOut]


def _asset_out(asset: MediaAsset) -> AssetOut:
    """MediaAsset 行 → 响应模型（列表/详情共用）。"""
    return AssetOut(
        asset_id=asset.asset_id,
        asset_type=asset.asset_type,
        parent_id=asset.parent_id,
        title=asset.title,
        duration_ms=asset.duration_ms,
        video_uri=asset.video_uri,
        created_at=asset.created_at,
    )


# ---------- 路由 ----------

@router.post("/content/import", response_model=ContentImportResponse)
def import_content(
    body: ContentImportRequest,
    session: Session = Depends(get_session),
) -> ContentImportResponse:
    """导入统一内容 JSON：适配 → 落库，返回资产/上下文标识。

    同步 def：适配校验与导入落库（服务层内部 commit）为阻塞操作，走线程
    池不占事件循环。错误映射（Task 8/9 约定由本层捕获）：
    - 未知 kind（KeyError，消息已列出可用 kind）→ 422；
    - 内容 JSON 校验失败（pydantic.ValidationError）→ 422 带摘要；
    - video_root 不存在（ValueError）→ 422。
    """
    try:
        adapter = get_adapter(body.kind)
    except KeyError as exc:
        # str(exc) 取 KeyError 消息（Task 8 审查注记：勿用 exc.args[0]）
        raise HTTPException(status_code=422, detail=str(exc)) from None
    try:
        content = adapter.adapt(body.data)
    except ValidationError as exc:
        raise HTTPException(
            status_code=422, detail=f"内容 JSON 校验失败: {exc}") from None
    try:
        result = import_content_svc(content, session, body.video_root)
    except ValueError as exc:
        # 服务层在任何数据库写入前校验 video_root，失败零副作用
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return ContentImportResponse(
        asset_id=result.asset_id,
        context_id=result.context_id,
        scene_asset_ids=result.scene_asset_ids,
    )


@router.get("/assets", response_model=list[AssetOut])
def list_assets(
    asset_type: str | None = Query(None),
    q: str | None = Query(None),
    session: Session = Depends(get_session),
) -> list[AssetOut]:
    """资产列表：asset_type 精确过滤；q 对 title 子串匹配；按 asset_id 排序。"""
    query = session.query(MediaAsset)
    if asset_type is not None:
        query = query.filter(MediaAsset.asset_type == asset_type)
    if q:
        # LIKE 通配符按字面转义，保证 q 为纯子串语义（同 tags 路由）
        pattern = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        query = query.filter(MediaAsset.title.like(pattern, escape="\\"))
    rows = query.order_by(MediaAsset.asset_id).all()
    return [_asset_out(a) for a in rows]


@router.get("/assets/{asset_id}", response_model=AssetDetail)
def get_asset(asset_id: str, session: Session = Depends(get_session)) -> AssetDetail:
    """资产详情 + 其全部 media_context 摘要（不含 content_json）；未知 asset_id → 404。"""
    asset = session.get(MediaAsset, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail=f"资产不存在：{asset_id}")
    contexts = (
        session.query(MediaContext)
        .filter(MediaContext.asset_id == asset_id)
        .order_by(MediaContext.created_at, MediaContext.context_id)  # 按导入时间先后
        .all()
    )
    return AssetDetail(
        **_asset_out(asset).model_dump(),
        contexts=[
            ContextSummaryOut(
                context_id=c.context_id,
                model_version=c.model_version,
                recognition_status=c.recognition_status,
                created_at=c.created_at,
            )
            for c in contexts
        ],
    )


@router.get("/assets/{asset_id}/contexts/{context_id}", response_model=ContentJSON)
def get_asset_context(
    asset_id: str, context_id: str, session: Session = Depends(get_session)
) -> ContentJSON:
    """单个 media_context 的完整内容 JSON（解析 content_json 返回）。

    资产不存在、上下文不存在或上下文不属于该资产 → 404。
    """
    asset = session.get(MediaAsset, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail=f"资产不存在：{asset_id}")
    ctx = session.get(MediaContext, context_id)
    if ctx is None or ctx.asset_id != asset_id:
        raise HTTPException(status_code=404, detail=f"上下文不存在：{context_id}")
    return ContentJSON.model_validate_json(ctx.content_json)


@router.get("/adapters", response_model=list[str])
def list_adapters_api() -> list[str]:
    """已注册内容格式适配器 kind 列表（前端导入表单 kind 下拉数据源）。"""
    return list_adapters()
