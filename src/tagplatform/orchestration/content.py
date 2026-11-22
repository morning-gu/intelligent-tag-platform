"""内容域编排：内容导入、资产查询与影片目录导入——orchestration/content 域。

用例清单（原 api/routers/content.py 的读路径 SQL 与装配逻辑，Task 11 原样
下沉；SQL 与装配零语义变化）：

- `import_content`     统一内容 JSON 导入（适配 → 落库，错误映射集中处）
- `list_assets`        资产列表（asset_type 精确过滤 + title 子串匹配）
- `get_asset`          资产详情（不含 content_json 大字段）
- `get_asset_content`  资产主内容 JSON（media_asset.content_json 唯一数据源）
- `list_adapters`      已注册内容格式适配器 kind 列表
- `browse_dirs`        浏览影片资产目录（只列目录 + 库内同片匹配）
- `scan_movie_dirs`    扫描影片资产目录（每部片一条）
- `import_movie_dirs`  选中的影片目录注册成资产（overwrite 覆盖）

错误映射随逻辑下沉本层原样保留（422：未知 kind/内容 JSON 校验失败/
video_root 不存在/parent 资产不存在/目录根非法；404：未知资产/资产无内容）。
"""

from fastapi import HTTPException
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from tagplatform.orchestration._shared import UtcDatetime
from tagplatform.capabilities.adapters import get_adapter
from tagplatform.capabilities.adapters import list_adapters as list_registered_adapters
from tagplatform.capabilities.storage.models import MediaAsset
from tagplatform.services.content import movie_import
from tagplatform.services.content.content_import import import_content as import_content_svc
from tagplatform.services.content.content_template import template_from_stored


# ---------- 响应模型 ----------

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
    created_at: UtcDatetime | None = None
    # 最近一次写入内容的时间（没内容时等于上传时间）
    updated_at: UtcDatetime | None = None


class AssetDetail(AssetOut):
    """资产详情：列表字段 + 内容状态（content_json 大字段不出详情）。

    `recognized` 是 main 分支同名字段的兼容别名（等价于 `has_content`）：
    两边前端都读详情判断"有没有内容"，保留双名字段避免任一侧 404/KeyError。
    """

    recognized: bool = False


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
    #: content.json 里声明的 asset_id（目录这一侧的主键）
    asset_id: str | None = None
    title: str | None = None
    #: 库里认出来的同一部片：asset_id（同主键）/ imdb / title（上传那侧主键）
    matched_by: str | None = None
    existing_asset_id: str | None = None
    #: 库里已有该片（含按 imdb / 片名认出来的），前端默认不勾、置灰
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


# ---------- 内部辅助 ----------

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


# ---------- 用例 ----------

def import_content(
    session: Session, kind: str, video_root: str | None, data: dict
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
        adapter = get_adapter(kind)
    except KeyError as exc:
        # str(exc) 取 KeyError 消息（Task 8 审查注记：勿用 exc.args[0]）
        raise HTTPException(status_code=422, detail=str(exc)) from None
    try:
        content = adapter.adapt(data)
    except ValidationError as exc:
        raise HTTPException(
            status_code=422, detail=f"内容 JSON 校验失败: {exc}") from None
    try:
        # 原样入库：库里存的 JSON 与调用方提交的一致（不做字段改名/裁剪）
        result = import_content_svc(content, session, video_root, raw=data)
    except ValueError as exc:
        # 服务层在任何数据库写入前校验 video_root，失败零副作用
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return ContentImportResponse(
        asset_id=result.asset_id,
        scene_asset_ids=result.scene_asset_ids,
    )


def list_assets(
    session: Session, asset_type: str | None, q: str | None
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


def get_asset(session: Session, asset_id: str) -> AssetDetail:
    """资产详情（不含 content_json）；未知 asset_id → 404。"""
    asset = session.get(MediaAsset, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail=f"资产不存在：{asset_id}")
    return AssetDetail(**_asset_out(asset).model_dump(),
                       recognized=asset.content_json is not None)


def get_asset_content(session: Session, asset_id: str) -> dict:
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


def list_adapters() -> list[str]:
    """已注册内容格式适配器 kind 列表（前端导入表单 kind 下拉数据源）。"""
    return list_registered_adapters()


# ---------- 用例：影片目录导入 ----------

def browse_dirs(session: Session, path: str | None) -> DirBrowseResponse:
    """浏览服务器目录（只列目录，不返回文件内容）：逐层点进目标影片目录。

    不传 path 时落在配置的默认根（`settings.movie_asset_root`）。子项里带
    `content.json` 的即影片目录，`movies` 大于 0 的普通目录说明里面还有片。
    """
    try:
        current, parent, entries = movie_import.browse(path)
    except movie_import.MovieAssetError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    rows: list[DirEntryOut] = []
    index = movie_import.AssetIndex(session)
    for entry in entries:
        match = None
        if entry.is_movie and entry.error is None:
            # 影片目录：按 asset_id / imdb / 片名 认库里已有的同一部片。
            # 上传那侧主键是 asset_xxx，目录这侧是 movie_ttXXXX，只比 asset_id
            # 会把同一部片当成"没导入"，再导一遍就出现两条同名片。
            match = index.match(entry.asset_id, movie_import.imdb_of_dir(entry.name),
                                entry.title)
        rows.append(DirEntryOut(
            name=entry.name, path=entry.path, is_movie=entry.is_movie,
            movies=entry.movies, scenes=entry.scenes, frames=entry.frames,
            asset_id=entry.asset_id, title=entry.title,
            matched_by=match.matched_by if match else None,
            existing_asset_id=match.asset_id if match else None,
            exists=match is not None, error=entry.error))
    return DirBrowseResponse(path=str(current),
                             parent=str(parent) if parent else None,
                             entries=rows)


def scan_movie_dirs(session: Session, root: str | None) -> MovieScanResponse:
    """扫描影片资产目录：每部片一条（标题/场景数/帧数/库里是否已有）。"""
    try:
        base = movie_import.resolve_root(root)
    except movie_import.MovieAssetError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    items: list[MovieDirOut] = []
    index = movie_import.AssetIndex(session)
    for item in movie_import.scan_movie_dirs(base):
        # 与 browse 同一套匹配（asset_id → imdb → 片名），两个端点口径一致
        match = (index.match(item.asset_id, item.imdb_id, item.title)
                 if item.error is None else None)
        items.append(MovieDirOut(
            dir_name=item.dir_name, title=item.title, imdb_id=item.imdb_id,
            asset_id=item.asset_id, scenes=item.scenes, frames=item.frames,
            duration_ms=item.duration_ms, size_bytes=item.size_bytes,
            exists=match is not None,
            has_content=bool(match is not None and match.has_content),
            has_video=bool(match is not None and match.has_video),
            error=item.error))
    return MovieScanResponse(root=str(base), total=len(items), items=items)


def import_movie_dirs(
    session: Session, root: str | None, dir_names: list[str] | None,
    overwrite: bool,
) -> MovieImportResponse:
    """把选中的影片目录注册成资产（不经过上传视频；已存在的按 overwrite 处理）。"""
    try:
        base = movie_import.resolve_root(root)
    except movie_import.MovieAssetError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    outcome = movie_import.import_many(
        session, base, dir_names, overwrite=overwrite)
    counts = outcome.counts
    rows = [MovieImportItem(
        dir_name=o.dir_name, status=o.status, asset_id=o.asset_id,
        title=o.title, scenes=o.scenes, frames=o.frames, reason=o.reason)
        for o in (*outcome.imported, *outcome.skipped, *outcome.failed)]
    return MovieImportResponse(imported=counts["imported"],
                               skipped=counts["skipped"],
                               failed=counts["failed"], items=rows)
