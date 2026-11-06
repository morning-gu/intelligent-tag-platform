"""上传占位核心层：视频文件落盘 + media_asset 登记（不做识别）。

分层红线：本模块不得 import fastapi（设计文档 §9.4）；HTTP 状态映射
（422/413）由 API 路由层完成，本模块以异常表达失败：
- UnsupportedVideoFormat：后缀不在白名单 → 路由层转 422；
- UploadTooLarge：流式累计超上限 → 路由层转 413。

占位语义（阶段 4 任务 4）：上传即建资产——asset_type 固定 "movie"、
duration_ms 置空、不建 media_context，识别内容后续由导入/生成流水线
补齐（前端资产列表可见、内容详情提示"未导入识别内容"）。

安全设计：存盘名一律 uuid4().hex + 归一小写后缀，与原始文件名零关联
——`../` 穿越与中文等任意原名成分天然免疫（测试断言存盘名不含原名）。

大文件内存安全：流式落盘（固定 1 MiB 块）逐块累计字节数，超限即中止
并清理半成品文件，绝不整读进内存。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

from sqlalchemy.orm import Session

from tagplatform.db.models import MediaAsset

# 上传后缀白名单（比较前归一小写；存盘名后缀同取小写）
ALLOWED_VIDEO_SUFFIXES = {".mp4", ".webm", ".mkv"}

# 单文件上传上限：2GB（> 上限才拒，恰等于放行）
MAX_UPLOAD_BYTES = 2 * 1024**3

# 流式落盘块大小：1 MiB（内存上界与磁盘 IO 次数的折中，同 video 路由）
_CHUNK_SIZE = 1024 * 1024


class UnsupportedVideoFormat(ValueError):
    """上传文件后缀不在白名单（路由层转 422）。"""


class UploadTooLarge(ValueError):
    """上传内容累计超过单文件上限（路由层转 413）。"""


@dataclass
class StoredUpload:
    """上传登记结果：新资产标识、标题（原名去后缀）、落盘绝对路径。"""

    asset_id: str
    title: str
    video_uri: str


def stored_filename(original_name: str) -> str:
    """校验后缀白名单并生成存盘名：uuid4().hex + 归一小写后缀。

    非白名单后缀抛 UnsupportedVideoFormat。存盘名与原名零关联（uuid
    重命名），穿越/中文/特殊字符等原名成分天然免疫。
    """
    suffix = Path(original_name).suffix.lower()
    if suffix not in ALLOWED_VIDEO_SUFFIXES:
        raise UnsupportedVideoFormat(f"不支持的视频格式: {original_name}")
    return f"{uuid.uuid4().hex}{suffix}"


def save_upload(
    stream: BinaryIO,
    original_name: str,
    session: Session,
    media_root: str,
    asset_id: str | None = None,
    title: str | None = None,
) -> StoredUpload:
    """占位上传：校验后缀 → 流式落盘（超限中止清理）→ 登记资产并提交。

    任何失败路径零残留：422（后缀）发生在任何写盘之前；413（超限）
    中止读取并删除半成品；两者均不产生数据库行。上限经 MAX_UPLOAD_BYTES
    模块全局调用期读取（测试可 monkeypatch 收紧以触发流式兜底路径）。

    asset_id：可选的资产绑定键。
    - 指向已存在资产 → 只更新 video_uri（不新建行、不改父级）——同一部片
      重复上传走的正是这条：资产行不重复，内容是后面覆盖的；
    - 指向不存在的资产 → 以该标识新建（上传先于识别时预留主键）；
    - 缺省 → 生成 `asset_` + uuid 的新主键。

    title：显示标题。缺省用文件名去后缀；命中数据集时由路由层传片名。
    """
    # 先校验后缀（失败零副作用：目录未建、文件未写、行未入）
    name = stored_filename(original_name)
    title = title or Path(original_name).stem
    root = Path(media_root)
    root.mkdir(parents=True, exist_ok=True)  # 上传时确保目录存在
    dest = root / name

    limit = MAX_UPLOAD_BYTES
    written = 0
    try:
        with dest.open("wb") as out:
            while True:
                chunk = stream.read(_CHUNK_SIZE)
                if not chunk:
                    break
                written += len(chunk)
                if written > limit:
                    raise UploadTooLarge(
                        f"上传大小 {written} 字节超出上限 {limit} 字节")
                out.write(chunk)
    except UploadTooLarge:
        # with 已关闭句柄（Windows 上打开句柄不可删），半成品清理后重抛
        dest.unlink(missing_ok=True)
        raise

    video_uri = str(dest.resolve())

    # 绑定已有资产：只补视频地址，不新建行（识别服务/数据集已登记资产的情形）
    if asset_id is not None:
        existing = session.get(MediaAsset, asset_id)
        if existing is not None:
            existing.video_uri = video_uri
            session.commit()
            return StoredUpload(asset_id=existing.asset_id, title=existing.title,
                                video_uri=video_uri)

    new_id = asset_id or f"asset_{uuid.uuid4().hex[:8]}"
    session.add(MediaAsset(
        asset_id=new_id,
        asset_type="movie",
        title=title,
        duration_ms=None,  # 占位：时长未知，识别流水线后续补齐
        video_uri=video_uri,  # 存盘绝对路径
        created_at=datetime.now(timezone.utc),  # 时间戳列无默认值，显式赋 UTC now
        updated_at=datetime.now(timezone.utc),
    ))
    session.commit()
    return StoredUpload(asset_id=new_id, title=title, video_uri=video_uri)
