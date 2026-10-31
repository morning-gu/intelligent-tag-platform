"""`/api/v1/assets/{asset_id}/video` 视频流路由（阶段 3 B6，详情页视频数据源）。

薄路由层：asset 查库 → video_uri 解析（复用 content_import.resolve_video_path，
不自行重写路径规则）→ 文件存在性校验 → Range 请求分块响应。业务规则（路径
解析四规则）在 content_import 层，本 router 只做协议转换与错误映射：
- asset 不存在 → 404；无 video_uri / 文件 `Path(...).is_file()` False →
  404 {"detail": "视频文件不存在"}；
- 合法 `Range: bytes=start-end` → 206 + `Content-Range: bytes start-end/total`
  + `Accept-Ranges: bytes` + 对应分块；end 越界截到文件尾；start 越界
  （>= 文件大小）→ 416 + `Content-Range: bytes */total`（计划 Review Focus 3
  允许"忽略回 200 或 416 取一致者"——定 416，语义更标准）；
- Range 语法非法（非 bytes= 前缀 / 解析失败 / start > end / 多区间）→ 忽略
  Range 回 200 全量；无 Range → 200 全量；
- 开放式 `bytes=start-` 与后缀 `bytes=-N` 按 RFC 7233 标准语义支持（浏览器
  视频拖动的常见请求形）；`bytes=-0` 零长不可满足 → 416；
- 大文件不整读内存：无 Range 走 FileResponse 磁盘流式（自带 Content-Length）；
  带 Range 头时（无论合法与否）一律走本层流式分块生成器（固定 1 MiB 块），
  绝不让 FileResponse 二次解析 Range——starlette 内建 range 行为（非法头回
  400、多区间回 multipart 206）与本契约（忽略回 200）不符；
- Content-Type 按后缀映射：.mp4/.webm/.mkv（大小写不敏感），其余
  application/octet-stream。

登录门槛由 create_app include 时的 protected_deps 统一挂载（api/__init__.py）。
"""

from collections.abc import Iterator
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.orm import Session

from tagplatform.api import get_session
from tagplatform.content_import import resolve_video_path
from tagplatform.db.models import MediaAsset

router = APIRouter(prefix="/api/v1")

# 流式读块大小：内存上界与磁盘 IO 次数的折中（大文件逐块产出，绝不整读）
_CHUNK_SIZE = 1024 * 1024

# 后缀 → Content-Type 映射（契约固定三种视频格式，其余兜底 octet-stream）
_CONTENT_TYPES = {
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
}


class _RangeUnsatisfiable(Exception):
    """Range 起始字节越界（start >= 文件大小）：端点转 416。"""


def _content_type(path: Path) -> str:
    """按文件后缀取 Content-Type（大小写不敏感；未识别后缀走兜底）。"""
    return _CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")


def _parse_range(
    header: str, total: int
) -> tuple[int, int] | None:
    """把 Range 头解析为闭区间 (start, end)；None = 忽略 Range（回 200 全量）。

    语法非法（非 bytes= 前缀 / 数字解析失败 / start > end / 负 start /
    多区间 / 空区间）→ None；起始越界（start >= total，含后缀 bytes=-0
    零长）→ 抛 _RangeUnsatisfiable 由端点转 416；合法区间归一化：开放式
    bytes=start- 截到 total-1，后缀 bytes=-N 取最后 N 字节（N 超文件长取
    全文件），end 越界截到 total-1。
    """
    if not header.startswith("bytes="):
        return None
    spec = header[len("bytes="):]
    if "," in spec:
        # 多区间不支持：整头忽略回 200（RFC 允许忽略不支持的 ranges 形式）
        return None
    first, sep, last = spec.partition("-")
    if not sep:
        return None
    first, last = first.strip(), last.strip()
    if first == "":
        # 后缀形式 bytes=-N：最后 N 字节
        try:
            n = int(last)
        except ValueError:
            return None
        if n <= 0 or total == 0:
            raise _RangeUnsatisfiable  # 零长后缀/空文件无可服务字节
        return (max(0, total - n), total - 1)
    try:
        start = int(first)
        end = int(last) if last else None
    except ValueError:
        return None
    if start < 0 or (end is not None and start > end):
        return None
    if start >= total:
        raise _RangeUnsatisfiable
    return (start, total - 1 if end is None else min(end, total - 1))


def _iter_file(path: Path, start: int, end: int) -> Iterator[bytes]:
    """按 [start, end] 闭区间流式读文件（固定块产出，绝不整读内存）。"""
    with path.open("rb") as f:
        f.seek(start)
        remaining = end - start + 1
        while remaining > 0:
            data = f.read(min(_CHUNK_SIZE, remaining))
            if not data:  # 文件被并发截短的兜底：给足已读字节即止
                break
            remaining -= len(data)
            yield data


@router.get("/assets/{asset_id}/video")
def stream_video(
    asset_id: str, request: Request, session: Session = Depends(get_session)
):
    """视频流端点：按 Range 分块 / 全量流式返回资产视频文件。

    同步 def：查库与磁盘 IO 为阻塞操作，走线程池不占事件循环。错误映射：
    asset 不存在 → 404；无 video_uri / 文件不存在 → 404
    {"detail": "视频文件不存在"}；Range 起始越界 → 416。
    """
    asset = session.get(MediaAsset, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail=f"资产不存在：{asset_id}")
    if not asset.video_uri:
        raise HTTPException(status_code=404, detail="视频文件不存在")
    path = Path(resolve_video_path(asset.video_uri, None))
    if not path.is_file():
        raise HTTPException(status_code=404, detail="视频文件不存在")
    total = path.stat().st_size
    media_type = _content_type(path)
    range_header = request.headers.get("range")
    if range_header is None:
        # 200 全量：FileResponse 磁盘流式（自带 Content-Length；
        # 此路径无 Range 头，不会触发 starlette 内建 range 解析）
        return FileResponse(
            path, media_type=media_type, headers={"Accept-Ranges": "bytes"})
    try:
        chunk = _parse_range(range_header, total)
    except _RangeUnsatisfiable:
        raise HTTPException(
            status_code=416,
            detail=f"请求范围超出文件大小（0-{total}）",
            headers={"Content-Range": f"bytes */{total}"},
        ) from None
    if chunk is None:
        # 语法非法 → 忽略 Range 回 200 全量：流式分块（不用 FileResponse，
        # 避免 starlette 对同一非法头二次解析回 400/multipart 的契约偏离）
        return StreamingResponse(
            _iter_file(path, 0, total - 1),
            media_type=media_type,
            headers={"Accept-Ranges": "bytes"},
        )
    start, end = chunk
    # 206 精确分块：按 [start, end] 流式产出（Content-Range 携带总长）
    return StreamingResponse(
        _iter_file(path, start, end),
        status_code=206,
        media_type=media_type,
        headers={
            "Content-Range": f"bytes {start}-{end}/{total}",
            "Accept-Ranges": "bytes",
        },
    )
