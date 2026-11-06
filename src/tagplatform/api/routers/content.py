"""`/api/v1/content`、`/api/v1/assets`、`/api/v1/adapters` 路由：内容导入与资产查询。

薄路由层：适配（get_adapter().adapt）→ 导入（import_content）→ 响应模型，
业务规则全部在 tagplatform.adapters / content_import 与 DB 层，router 只做
协议转换与错误映射（未知 kind / 内容 JSON 校验失败 / video_root 非法 →
422；未知资产/未完成内容识别 → 404）。

v2 合并语义（doc §3.1）：content_json 直写影片级 media_asset 行，context
概念退役——完整 JSON 经 `GET /assets/{asset_id}/content` 取回（解析后的
平台内部 ContentJSON）；资产详情带 recognized 布尔（content_json 非空），
不再有 contexts 数组与 contexts 端点。
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from tagplatform.adapters import get_adapter, list_adapters
from tagplatform.api import get_session
from tagplatform.content_import import import_content as import_content_svc
from tagplatform.content_io import load_content
from tagplatform.db.models import MediaAsset
from tagplatform.schemas import ContentJSON

router = APIRouter(prefix="/api/v1")


# ---------- 响应模型 ----------

class ContentImportRequest(BaseModel):
    """内容导入请求体：源格式 kind + 可选视频根目录 + 原始内容 JSON dict。"""

    kind: str
    video_root: str | None = None
    data: dict


class ContentImportResponse(BaseModel):
    """导入响应：影片 asset_id + 场景 asset_id 列表（context_id 已退役）。"""

    asset_id: str
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


class AssetDetail(AssetOut):
    """资产详情：列表字段之上附内容识别状态（content_json 大字段不出详情）。"""

    recognized: bool


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
    """导入统一内容 JSON：适配 → 落库，返回资产标识。

    同步 def：适配校验与导入落库（服务层内部 commit）为阻塞操作，走线程
    池不占事件循环。错误映射（Task 8/9 约定由本层捕获）：
    - 未知 kind（KeyError，消息已列出可用 kind）→ 422；
    - 内容 JSON 校验失败（pydantic.ValidationError）→ 422 带摘要；
    - video_root 不存在（ValueError）→ 422；
    - parent_id 指向不存在资产（ValueError，新建资产写库前拦截）→ 422。
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
        # 原样入库：库里存的 JSON 与调用方提交的一致（不做字段改名/裁剪）
        result = import_content_svc(content, session, body.video_root,
                                    raw=body.data)
    except ValueError as exc:
        # 服务层在任何数据库写入前校验 video_root，失败零副作用
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return ContentImportResponse(
        asset_id=result.asset_id,
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
    """资产详情 + 识别状态（recognized = content_json 非空）；未知 asset_id → 404。"""
    asset = session.get(MediaAsset, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail=f"资产不存在：{asset_id}")
    return AssetDetail(
        **_asset_out(asset).model_dump(),
        recognized=bool(asset.content_json),
    )


@router.get("/assets/{asset_id}/content", response_model=ContentJSON)
def get_asset_content(
    asset_id: str, session: Session = Depends(get_session)
) -> ContentJSON:
    """资产的完整内容 JSON（解析 content_json 返回平台内部 ContentJSON）。

    资产不存在 → 404（"资产不存在"）；content_json 为空（未完成内容
    识别，如上传占位资产/场景子资产）→ 404（"未完成内容识别"）。
    """
    asset = session.get(MediaAsset, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail=f"资产不存在：{asset_id}")
    if not asset.content_json:
        raise HTTPException(
            status_code=404, detail=f"资产未完成内容识别：{asset_id}")
    return load_content(asset.content_json)


@router.get("/adapters", response_model=list[str])
def list_adapters_api() -> list[str]:
    """已注册内容格式适配器 kind 列表（前端导入表单 kind 下拉数据源）。"""
    return list_adapters()
