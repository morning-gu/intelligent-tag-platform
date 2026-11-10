"""`/api/v1/content`、`/api/v1/assets`、`/api/v1/adapters` 路由：内容导入与资产查询。

薄路由层：适配（get_adapter().adapt）→ 导入（import_content）→ 响应模型，
业务规则全部在 tagplatform.adapters / content_import 与 DB 层，router 只做
协议转换与错误映射（未知 kind / 内容 JSON 校验失败 / video_root 非法 →
422；未知资产 → 404）。内容只有一份：`media_asset.content_json`，完整 JSON
走 `GET /assets/{asset_id}/content`；v1 的 contexts 数组与 contexts 端点已
随 media_context 表退役，不再保留兼容路由。
"""

from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from tagplatform.adapters import get_adapter, list_adapters
from tagplatform.api import get_session
import tagplatform.movie_import as movie_import
from tagplatform.content_import import import_content as import_content_svc
from tagplatform.content_template import template_from_stored
from tagplatform.db.models import MediaAsset

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
    # 是否已有内容（影片行 content_json 非空；场景行恒 False）
    has_content: bool = False
    created_at: datetime | None = None
    # 最近一次写入内容的时间（没内容时等于上传时间）
    updated_at: datetime | None = None


class AssetDetail(AssetOut):
    """资产详情：列表字段 + 内容状态（content_json 大字段不出详情）。

    `recognized` 是 main 分支同名字段的兼容别名（等价于 `has_content`）：
    两边前端都读详情判断"有没有内容"，保留双名字段避免任一侧 404/KeyError。
    """

    recognized: bool = False


def _asset_out(asset: MediaAsset) -> AssetOut:
    """MediaAsset 行 → 响应模型（列表/详情共用）。"""
    return AssetOut(
        asset_id=asset.asset_id,
        asset_type=asset.asset_type,
        parent_id=asset.parent_id,
        title=asset.title,
        duration_ms=asset.duration_ms,
        video_uri=asset.video_uri,
        has_content=asset.content_json is not None,
        created_at=asset.created_at,
        updated_at=asset.updated_at,
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
    """资产详情（不含 content_json）；未知 asset_id → 404。"""
    asset = session.get(MediaAsset, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail=f"资产不存在：{asset_id}")
    return AssetDetail(**_asset_out(asset).model_dump(),
                       recognized=asset.content_json is not None)


@router.get("/assets/{asset_id}/content")
def get_asset_content(asset_id: str, session: Session = Depends(get_session)) -> dict:
    """资产主内容 JSON（`media_asset.content_json`，上游模板形态）。

    这是 v2 的内容唯一数据源：生成任务与资产详情页都读这里（v1 的
    media_context 已并入本表）。资产不存在 → 404；资产无内容 → 404。
    """
    asset = session.get(MediaAsset, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail=f"资产不存在：{asset_id}")
    if not asset.content_json:
        raise HTTPException(status_code=404, detail=f"资产无识别内容：{asset_id}")
    return template_from_stored(asset.content_json)


@router.get("/adapters", response_model=list[str])
def list_adapters_api() -> list[str]:
    """已注册内容格式适配器 kind 列表（前端导入表单 kind 下拉数据源）。"""
    return list_adapters()


# ---------- 目录导入：把已备好的影片资产目录直接注册成资产 ----------

class MovieDirOut(BaseModel):
    """扫描到的一部影片目录（前端列表一行）。"""

    dir_name: str
    title: str
    imdb_id: str | None = None
    asset_id: str | None = None
    scenes: int = 0
    frames: int = 0
    duration_ms: int | None = None
    size_bytes: int = 0
    # 库里已有同 asset_id 的资产（前端置灰用）
    exists: bool = False
    has_content: bool = False
    has_video: bool = False
    # content.json 读不了时的原因（该行不可导入）
    error: str | None = None


class DirEntryOut(BaseModel):
    """目录浏览的一项：可能本身就是影片目录，也可能还套着几部片。"""

    name: str
    path: str
    is_movie: bool = False
    movies: int = 0
    scenes: int = 0
    frames: int = 0
    asset_id: str | None = None
    # 库里已有同 asset_id 的资产（前端默认不勾、置灰）
    exists: bool = False
    error: str | None = None


class DirBrowseResponse(BaseModel):
    """目录浏览响应：当前目录、上级目录、子项清单。"""

    path: str
    parent: str | None = None
    entries: list[DirEntryOut]


class MovieScanResponse(BaseModel):
    root: str
    total: int
    items: list[MovieDirOut]


class MovieImportRequest(BaseModel):
    """目录导入请求：不传 dir_names 表示全部；overwrite 覆盖已有内容。"""

    dir_names: list[str] | None = None
    overwrite: bool = False
    root: str | None = None


class MovieImportItem(BaseModel):
    dir_name: str
    status: str          # imported | skipped | failed
    asset_id: str | None = None
    title: str | None = None
    scenes: int = 0
    frames: int = 0
    reason: str | None = None


class MovieImportResponse(BaseModel):
    imported: int
    skipped: int
    failed: int
    items: list[MovieImportItem]


@router.get("/content/import/browse", response_model=DirBrowseResponse)
def browse_dirs(
    path: str | None = Query(None, description="要浏览的绝对路径，缺省用配置的根目录"),
    session: Session = Depends(get_session),
) -> DirBrowseResponse:
    """浏览服务器目录（只列目录，不返回文件内容）：逐层点进目标影片目录。

    不传 path 时落在配置的默认根（`settings.movie_asset_root`）。子项里带
    `content.json` 的即影片目录，`movies` 大于 0 的普通目录说明里面还有片。
    """
    try:
        current, parent, entries = movie_import.browse(path)
    except movie_import.MovieAssetError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    rows: list[DirEntryOut] = []
    for entry in entries:
        asset_id = None
        exists = False
        if entry.is_movie:
            # 影片目录：顺手把"库里是否已有"也算出来，前端直接置灰
            try:
                data = movie_import._read_payload(Path(entry.path) / "content.json")
                asset_id = (data.get("asset") or {}).get("asset_id")
            except movie_import.MovieAssetError:
                asset_id = None
            exists = bool(asset_id and session.get(MediaAsset, asset_id))
        rows.append(DirEntryOut(name=entry.name, path=entry.path,
                                is_movie=entry.is_movie, movies=entry.movies,
                                scenes=entry.scenes, frames=entry.frames,
                                asset_id=asset_id, exists=exists,
                                error=entry.error))
    return DirBrowseResponse(path=str(current),
                             parent=str(parent) if parent else None,
                             entries=rows)


@router.get("/content/import/movie-scan", response_model=MovieScanResponse)
def scan_movie_asset_dirs(
    root: str | None = Query(None, description="影片资产目录根，缺省用配置值"),
    session: Session = Depends(get_session),
) -> MovieScanResponse:
    """扫描影片资产目录：每部片一条（标题/场景数/帧数/库里是否已有）。"""
    try:
        base = movie_import.resolve_root(root)
    except movie_import.MovieAssetError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    items: list[MovieDirOut] = []
    for item in movie_import.scan_movie_dirs(base):
        existing = (session.get(MediaAsset, item.asset_id)
                    if item.asset_id else None)
        items.append(MovieDirOut(
            dir_name=item.dir_name, title=item.title, imdb_id=item.imdb_id,
            asset_id=item.asset_id, scenes=item.scenes, frames=item.frames,
            duration_ms=item.duration_ms, size_bytes=item.size_bytes,
            exists=existing is not None,
            has_content=bool(existing is not None and existing.content_json),
            has_video=bool(existing is not None and existing.video_uri),
            error=item.error))
    return MovieScanResponse(root=str(base), total=len(items), items=items)


@router.post("/content/import/movie-bulk", response_model=MovieImportResponse)
def import_movie_asset_dirs(
    body: MovieImportRequest,
    session: Session = Depends(get_session),
) -> MovieImportResponse:
    """把选中的影片目录注册成资产（不经过上传视频；已存在的按 overwrite 处理）。"""
    try:
        base = movie_import.resolve_root(body.root)
    except movie_import.MovieAssetError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    outcome = movie_import.import_many(
        session, base, body.dir_names, overwrite=body.overwrite)
    counts = outcome.counts
    rows = [MovieImportItem(
        dir_name=o.dir_name, status=o.status, asset_id=o.asset_id,
        title=o.title, scenes=o.scenes, frames=o.frames, reason=o.reason)
        for o in (*outcome.imported, *outcome.skipped, *outcome.failed)]
    return MovieImportResponse(imported=counts["imported"],
                               skipped=counts["skipped"],
                               failed=counts["failed"], items=rows)
