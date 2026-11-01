"""`/api/v1/media/upload` 上传占位路由（阶段 4 任务 4）。

占位语义：上传即登记（存文件 + media_asset 行），**不做识别**——
无 media_context、无转码，内容详情由前端提示"未导入识别内容"。

权限说明（设计如此，报告注明）：登录即可上传，未设 admin 门槛——
与其他业务 router 一致经 create_app include 时统一挂 protected_deps
（api/__init__.py），不额外加 require_admin。

薄路由层：后缀白名单 / 大小上限 / uuid 存盘 / 资产行登记全部在核心层
tagplatform.media，本 router 只做 multipart 提取与错误映射：
- 后缀非白名单（UnsupportedVideoFormat）→ 422 {"detail": "不支持的视频格式"}；
- 超上限（UploadTooLarge）→ 413，两条拦截路径：
  - Content-Length 头早拒（端点体首步）：诚实客户端的 2GB+ 上传无需
    再复制落盘到 media_root 即拒。注意 FastAPI 在依赖求解前已收完并
    解析 multipart（starlette 磁盘 spool，内存不上爆），处理器内检查
    无法早于收包——真正的预读拦截需全局中间件，超出本任务范围；
  - 流式累计超限（核心层兜底）：防分块传输 / 头缺失 / 伪造小头。
"""

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from tagplatform import media
from tagplatform.api import get_session, get_settings_dep
from tagplatform.settings import Settings

router = APIRouter(prefix="/api/v1")


class MediaUploadResponse(BaseModel):
    """上传响应：新资产标识 + 标题（原名去后缀）+ 落盘绝对路径。"""

    asset_id: str
    title: str
    video_uri: str


def enforce_upload_size_limit(request: Request) -> None:
    """按 Content-Length 头拒绝超上限请求（端点体首步，先于任何落盘/入库）。

    注意 FastAPI 在调用处理器前已收完并解析 multipart（磁盘 spool，
    内存不上爆），本检查拦在 media_root 复制与入库之前。头缺失（分块
    传输）或伪造小时跳过——由核心层流式累计超限兜底。上限经
    media.MAX_UPLOAD_BYTES 调用期读取（测试可 monkeypatch 收紧）。
    """
    header = request.headers.get("content-length")
    if header is not None and header.isdigit() and int(header) > media.MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="文件大小超出上传上限")


@router.post("/media/upload", response_model=MediaUploadResponse)
def upload_media(
    request: Request,
    file: UploadFile,
    settings: Settings = Depends(get_settings_dep),
    session: Session = Depends(get_session),
) -> MediaUploadResponse:
    """上传视频占位登记：存文件 + 建 media_asset（不做识别）。

    同步 def：磁盘落盘与登记为阻塞操作，走线程池不占事件循环。进入本
    函数时 multipart 已由 FastAPI 收完解析（磁盘 spool，内存不上爆），
    先按 Content-Length 头早拒，再交核心层流式落盘与登记。
    """
    enforce_upload_size_limit(request)
    try:
        stored = media.save_upload(
            file.file, file.filename or "", session, settings.media_root)
    except media.UnsupportedVideoFormat:
        raise HTTPException(
            status_code=422, detail="不支持的视频格式") from None
    except media.UploadTooLarge:
        raise HTTPException(
            status_code=413, detail="文件大小超出上传上限") from None
    return MediaUploadResponse(
        asset_id=stored.asset_id, title=stored.title, video_uri=stored.video_uri
    )
