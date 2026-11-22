"""`/api/v1/assets/{asset_id}/video` 视频流薄路由（阶段 3 B6，详情页视频数据源）。

接入层只做协议转换：提取 Range 头 → 调编排（资产查库、video_uri 解析、
文件存在性校验、Range 解析与流式响应构造全在
tagplatform.orchestration.media.stream_asset_video——业务规则（路径解析
四规则）在 services.content.content_import.resolve_video_path，本文件零
路径逻辑）→ 返回响应对象。错误映射（404 资产/视频不存在、416 Range
越界、非法 Range 忽略回 200）随逻辑在编排层原样保留。

登录门槛由 create_app include 时的 protected_deps 统一挂载（api/__init__.py）。
"""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from tagplatform.api import get_session
from tagplatform.orchestration import media as media_orch

router = APIRouter(prefix="/api/v1")


@router.get("/assets/{asset_id}/video")
def stream_video(
    asset_id: str, request: Request, session: Session = Depends(get_session)
):
    """视频流端点：按 Range 分块 / 全量流式返回资产视频文件。

    同步 def：查库与磁盘 IO 为阻塞操作，走线程池不占事件循环。
    """
    return media_orch.stream_asset_video(
        session, asset_id, request.headers.get("range"))
