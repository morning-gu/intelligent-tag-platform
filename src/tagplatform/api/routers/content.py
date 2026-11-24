"""`/api/v1/content`、`/api/v1/assets`、`/api/v1/adapters` 薄路由。

接入层只做协议转换：请求模型声明校验（kind/data/video_root 形状）→
调编排 → 返回。适配、导入落库、资产查询与影片目录扫描的读路径 SQL 与
装配全部在 tagplatform.orchestration.content（用例清单见该模块 docstring）；
业务规则在 capabilities.adapters / services.content（content_import、
movie_import）与 DB 层。错误映射（未知 kind / 内容 JSON 校验失败 /
video_root 非法 → 422；未知资产 → 404）随逻辑在编排层原样保留。

内容只有一份：`media_asset.content_json`，完整 JSON 走
`GET /assets/{asset_id}/content`；v1 的 contexts 数组与 contexts 端点已
随 media_context 表退役，不再保留兼容路由。
"""

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from tagplatform.api import get_session
from tagplatform.api.depends import require_admin
from tagplatform.capabilities.storage.models import UserAccount
from tagplatform.orchestration import content as content_orch
from tagplatform.orchestration._shared import ClearPreviewOut, ClearResultOut
from tagplatform.orchestration.content import (
    AssetDetail,
    AssetOut,
    ContentImportResponse,
    DirBrowseResponse,
    MovieImportResponse,
    MovieScanResponse,
)

router = APIRouter(prefix="/api/v1")


# ---------- 请求模型 ----------

class ContentImportRequest(BaseModel):
    """内容导入请求体：源格式 kind + 可选视频根目录 + 原始内容 JSON dict。"""

    kind: str
    video_root: str | None = None
    data: dict


class MovieImportRequest(BaseModel):
    """目录导入请求：不传 dir_names 表示全部；overwrite 覆盖已有内容。"""

    dir_names: list[str] | None = None
    overwrite: bool = False
    root: str | None = None


# ---------- 路由 ----------

@router.post("/content/import", response_model=ContentImportResponse)
def import_content(
    body: ContentImportRequest,
    session: Session = Depends(get_session),
) -> ContentImportResponse:
    """导入统一内容 JSON：适配 → 落库，返回资产标识。

    同步 def：适配校验与导入落库（服务层内部 commit）为阻塞操作，走线程
    池不占事件循环。
    """
    return content_orch.import_content(session, body.kind, body.video_root, body.data)


@router.get("/assets", response_model=list[AssetOut])
def list_assets(
    asset_type: str | None = Query(None),
    q: str | None = Query(None),
    session: Session = Depends(get_session),
) -> list[AssetOut]:
    """资产列表：asset_type 精确过滤；q 对 title 子串匹配；按 asset_id 排序。"""
    return content_orch.list_assets(session, asset_type, q)


@router.get("/assets/{asset_id}", response_model=AssetDetail)
def get_asset(asset_id: str, session: Session = Depends(get_session)) -> AssetDetail:
    """资产详情（不含 content_json）；未知 asset_id → 404。"""
    return content_orch.get_asset(session, asset_id)


@router.get("/assets/{asset_id}/content")
def get_asset_content(asset_id: str, session: Session = Depends(get_session)) -> dict:
    """资产主内容 JSON（`media_asset.content_json`，上游模板形态）。"""
    return content_orch.get_asset_content(session, asset_id)


@router.get("/adapters", response_model=list[str])
def list_adapters_api() -> list[str]:
    """已注册内容格式适配器 kind 列表（前端导入表单 kind 下拉数据源）。"""
    return content_orch.list_adapters()


@router.get("/content/import/browse", response_model=DirBrowseResponse)
def browse_dirs(
    path: str | None = Query(None, description="要浏览的绝对路径，缺省用配置的根目录"),
    session: Session = Depends(get_session),
) -> DirBrowseResponse:
    """浏览服务器目录（只列目录，不返回文件内容）：逐层点进目标影片目录。"""
    return content_orch.browse_dirs(session, path)


@router.get("/content/import/movie-scan", response_model=MovieScanResponse)
def scan_movie_asset_dirs(
    root: str | None = Query(None, description="影片资产目录根，缺省用配置值"),
    session: Session = Depends(get_session),
) -> MovieScanResponse:
    """扫描影片资产目录：每部片一条（标题/场景数/帧数/库里是否已有）。"""
    return content_orch.scan_movie_dirs(session, root)


@router.post("/content/import/movie-bulk", response_model=MovieImportResponse)
def import_movie_asset_dirs(
    body: MovieImportRequest,
    session: Session = Depends(get_session),
) -> MovieImportResponse:
    """把选中的影片目录注册成资产（不经过上传视频；已存在的按 overwrite 处理）。"""
    return content_orch.import_movie_dirs(
        session, body.root, body.dir_names, body.overwrite)


# ---------- clear-reset（2026-11-24 spec §3.1：①②④⑤ + 预览，admin 专属） ----------

@router.delete("/assets/{asset_id}/tags", response_model=ClearResultOut)
def clear_asset_tags(
    asset_id: str,
    session: Session = Depends(get_session),
    user: UserAccount = Depends(require_admin),
) -> ClearResultOut:
    """① 清空单内容标签数据（待审核/正式挂载/该资产任务历史，queued 一并删）。"""
    return content_orch.clear_asset_tags(session, asset_id)


@router.delete("/content/tags", response_model=ClearResultOut)
def clear_all_content_tags(
    session: Session = Depends(get_session),
    user: UserAccount = Depends(require_admin),
) -> ClearResultOut:
    """② 清空全部内容的标签数据（标签库与内容资产幸存）。"""
    return content_orch.clear_all_content_tags(session)


@router.delete("/assets/{asset_id}", response_model=ClearResultOut)
def delete_asset(
    asset_id: str,
    session: Session = Depends(get_session),
    user: UserAccount = Depends(require_admin),
) -> ClearResultOut:
    """④ 删除单内容资产（含子场景与全部关联行；视频源文件不删除）。"""
    return content_orch.delete_asset(session, asset_id)


@router.delete("/assets", response_model=ClearResultOut)
def delete_all_assets(
    session: Session = Depends(get_session),
    user: UserAccount = Depends(require_admin),
) -> ClearResultOut:
    """⑤ 删除全部内容资产（含全部关联行；标签库幸存）。"""
    return content_orch.delete_all_assets(session)


@router.get("/content/clear-preview", response_model=ClearPreviewOut)
def content_clear_preview(
    scope: str = Query(..., pattern="^(asset-tags|all-tags|asset|all-assets)$"),
    asset_id: str | None = Query(None),
    session: Session = Depends(get_session),
    user: UserAccount = Depends(require_admin),
) -> ClearPreviewOut:
    """①②④⑤ 的计数预览（只读）：确认弹窗"将删除 N 行"的数据源。"""
    return content_orch.clear_preview(session, scope, asset_id)
