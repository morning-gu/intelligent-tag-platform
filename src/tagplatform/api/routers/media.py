"""`/api/v1/media/*` 上传与「上传即生成资产」薄路由。

上传语义（2026-11 起）：登记资产（存文件 + media_asset 行），并用**文件名**
去数据集里匹配影片——命中就建一条后台生成任务，逐场景跑多模态模型，把
生成结果以同一条资产的 asset_id 导入平台；没命中就是普通占位资产。前端
只需"选一个 mp4"：文件名带片名（`The.Godfather.1972.1080p.mkv`）或
IMDb id（`tt0068646.mp4`，最准）。

接入层只做协议转换：multipart 表单提取、Content-Length 头早拒（413，
纯协议守卫，先于任何落盘/入库）→ 调编排 → 返回。预检匹配、上传落盘
登记、构建任务查询与进度页全部在 tagplatform.orchestration.media（用例
清单见该模块 docstring）；错误映射（422 后缀白名单 / 413 超上限 / 409
冲突护栏）随逻辑在编排层原样保留。

权限说明（设计如此，报告注明）：登录即可上传，未设 admin 门槛——与其他
业务 router 一致经 create_app include 时统一挂 protected_deps。
"""

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from tagplatform.api import get_session, get_settings_dep
from tagplatform.orchestration import media as media_orch
from tagplatform.orchestration.media import (
    BuildTaskOut,
    MediaMatchResponse,
    MediaUploadResponse,
)
from tagplatform.settings import Settings

router = APIRouter(prefix="/api/v1")


def enforce_upload_size_limit(request: Request) -> None:
    """按 Content-Length 头拒绝超上限请求（端点体首步，先于任何落盘/入库）。

    注意 FastAPI 在调用处理器前已收完并解析 multipart（磁盘 spool，
    内存不上爆），本检查拦在 media_root 复制与入库之前。头缺失（分块
    传输）或伪造小时跳过——由核心层流式累计超限兜底。上限经
    media.MAX_UPLOAD_BYTES 调用期读取（测试可 monkeypatch 收紧）。
    """
    header = request.headers.get("content-length")
    if header is not None and header.isdigit() and int(header) > media_orch.media.MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="文件大小超出上传上限")


@router.get("/media/match", response_model=MediaMatchResponse)
def match_media(
    filename: str,
    settings: Settings = Depends(get_settings_dep),
    session: Session = Depends(get_session),
) -> MediaMatchResponse:
    """按文件名预检：命中哪部片、这部片是否已有资产（有没有内容）。"""
    return media_orch.match_media(session, settings, filename)


@router.post("/media/upload", response_model=MediaUploadResponse)
def upload_media(
    request: Request,
    file: UploadFile,
    asset_id: str | None = Form(None),
    auto_build: bool = Form(True),
    infer: bool | None = Form(
        None,
        description="是否用大模型补充资产字段（描述/氛围/事件/说话人）；"
                    "false 时只填数据集已有的事实，不调用模型。"
                    "缺省取全局配置 asset_infer"),
    settings: Settings = Depends(get_settings_dep),
    session: Session = Depends(get_session),
) -> MediaUploadResponse:
    """上传视频：登记资产；文件名命中数据集则异步生成资产内容。

    同步 def：磁盘落盘与登记为阻塞操作，走线程池不占事件循环。进入本
    函数时 multipart 已由 FastAPI 收完解析（磁盘 spool，内存不上爆），
    先按 Content-Length 头早拒，再交编排层（匹配护栏 → 落盘登记 → 建任务）。
    """
    enforce_upload_size_limit(request)
    return media_orch.upload_media(
        session, settings, file.file, file.filename or "",
        (asset_id or "").strip() or None, auto_build, infer)


@router.get("/media/build-tasks/{task_id}", response_model=BuildTaskOut)
def get_build_task(task_id: str, session: Session = Depends(get_session)) -> BuildTaskOut:
    """查询生成任务进度；未知 task_id → 404。"""
    return media_orch.get_build_task(session, task_id)


@router.get("/media/build-tasks", response_model=list[BuildTaskOut])
def list_build_tasks(asset_id: str | None = None,
                     session: Session = Depends(get_session)) -> list[BuildTaskOut]:
    """任务列表：按 asset_id 过滤，创建时间降序。"""
    return media_orch.list_build_tasks(session, asset_id)


@router.get("/media/build-tasks/{task_id}/view", response_class=HTMLResponse,
            include_in_schema=False)
def build_task_view(task_id: str, session: Session = Depends(get_session)) -> HTMLResponse:
    """进度页（不依赖前端构建）：浏览器打开即自动轮询。"""
    return media_orch.build_task_view(session, task_id)
