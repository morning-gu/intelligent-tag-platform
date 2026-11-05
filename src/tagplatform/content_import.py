"""内容导入服务：统一内容 JSON → media_asset 层级落库（v2 合并语义）。

v2 起 media_asset 与 media_context 合并（doc §3.1）：content_json（识别
产出 JSON 串）与 source（识别结果来源）直写**影片级 MediaAsset 行**，
重新导入直接**覆盖**（历史版本由 mtg 审计文件留档）——不再新建 context
行，也没有任何清理步骤。识别元信息（model_version/recognition_status）
存 content_json 内的 metadata，不单设列。

分层红线：本模块不得 import fastapi（设计文档 §9.4）；video_root 非法
抛 ValueError，由 API 层（Task 11）捕获转 422。

契约规则（brief）：
- **video_root 校验**：非 None 时必须为已存在目录（Path.is_dir()），否则
  抛 ValueError("video_root 不存在: ...")；
- **parent 校验**：新建资产（库内无 asset_id）时 parent_id 非空则必须为
  已存在资产，否则抛 ValueError("parent 资产不存在: ...")——拦截外键
  IntegrityError 逃逸；upsert 路径 parent 冻结不校验；
- **video 路径解析** resolve_video_path 四规则（见函数 docstring）；
- **asset_id**：content.asset.asset_id 非空则 upsert（存在则仅更新
  title/duration_ms/video_uri/content_json/source，不动 parent——asset_id
  是主键不允许变更，parent 一并冻结；不存在则插入）；为空则生成
  asset_ + uuid4().hex[:8]；
- **场景资产**：asset_id = f"{movie_asset_id}::{scene_id}"、
  asset_type="scene"、duration_ms = end - start、video_uri 同影片、
  content_json 保持 NULL（内容挂影片级，场景行是层级占位）；
  重复导入 upsert（同 scene_id 不重复插行）。

实现注意：
- MediaAsset.parent_id 外键无 relationship()：影片行先 flush 落库，
  场景行（子行）后插，避免外键先炸（同 Task 3/7 教训）；
- 时间戳列无默认值，插入时显式赋 UTC now（Task 3 审查遗留注意项）；
- 场景 title 列 NOT NULL 而契约场景构造未给 title，取 scene.description。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlparse

from sqlalchemy.orm import Session

from tagplatform.content_io import dump_for_storage
from tagplatform.db.models import MediaAsset
from tagplatform.schemas import ContentJSON


@dataclass
class ImportResult:
    """导入结果：影片 asset_id 与场景 asset_id 列表（v2 无 context 概念）。"""

    asset_id: str
    scene_asset_ids: list[str]


def resolve_video_path(video_uri: str, video_root: str | None) -> str:
    """视频路径解析四规则（顺序即优先级）。

    - file:// URI → 取路径部分（无论 video_root 与否）；
    - 绝对路径 → 原样。POSIX 风格 "/abs/..." 在 Windows 上
      Path.is_absolute() 为 False，故以 PurePosixPath 判定（视频 URI
      约定 POSIX 风格）；Windows 盘符路径由 Path.is_absolute() 兜住；
    - 其余（相对路径）→ join(video_root, uri)；
    - video_root 为 None → 原样返回。
    """
    if video_uri.startswith("file://"):
        # 剥离 scheme 与 authority，只留路径部分（%xx 解码回文件名字符）
        return unquote(urlparse(video_uri).path)
    if PurePosixPath(video_uri).is_absolute() or Path(video_uri).is_absolute():
        return video_uri
    if video_root is None:
        return video_uri
    return str(Path(video_root) / video_uri)


def import_content(
    content: ContentJSON, session: Session, video_root: str | None,
    raw: dict | None = None,
) -> ImportResult:
    """导入统一内容 JSON：影片/场景资产 upsert + content_json 落影片行并提交。

    video_root 非 None 时必须为已存在目录，否则抛 ValueError（任何
    数据库写入之前先校验，失败零副作用）。

    raw 是**调用方收到的原始 JSON**（上游模板形态）。给了就原样入库，不给
    才退回内部形态的序列化——"资产以什么格式交付，库里就是什么格式"，
    读的时候再由 `content_io.load_content` 归一化。
    """
    if video_root is not None and not Path(video_root).is_dir():
        raise ValueError(f"video_root 不存在: {video_root}")

    now = datetime.now(timezone.utc)
    video_uri = (
        resolve_video_path(content.asset.video_uri, video_root)
        if content.asset.video_uri is not None
        else None
    )
    # 库存原文：模型/协议信息（metadata）就在这份 JSON 里，不单设列（doc §3.1）
    content_json = (dump_for_storage(raw) if raw is not None
                    else content.model_dump_json())

    # ---------- 影片资产：提供 asset_id 则 upsert，为空则生成 ----------
    asset_id = content.asset.asset_id
    existing = session.get(MediaAsset, asset_id) if asset_id else None
    if existing is not None:
        # upsert 只更新非键字段：asset_type/parent_id 冻结（主键不可变更）
        existing.title = content.asset.title
        existing.duration_ms = content.asset.duration_ms
        # video_uri 为 None 时保留原值：上传绑定的文件不能被内容 JSON 抹掉
        if video_uri is not None:
            existing.video_uri = video_uri
        # v2 合并语义：重识别直接覆盖 content_json/source，无清理步骤
        existing.content_json = content_json
        existing.source = content.metadata.source
    else:
        if not asset_id:
            asset_id = f"asset_{uuid.uuid4().hex[:8]}"
        # 写库前校验父资产存在性：parent_id 有外键约束，指向不存在资产时
        # 影片行 flush 会抛未捕获 IntegrityError（客户端拿裸 500）——在
        # 任何数据库写入之前先拦截（同 video_root 校验模式，失败零副作用；
        # upsert 路径 parent 冻结不参与写入，无需校验）
        if content.asset.parent_id and session.get(
                MediaAsset, content.asset.parent_id) is None:
            raise ValueError(f"parent 资产不存在: {content.asset.parent_id}")
        session.add(MediaAsset(
            asset_id=asset_id,
            asset_type=content.asset.asset_type,
            parent_id=content.asset.parent_id or None,
            title=content.asset.title,
            duration_ms=content.asset.duration_ms,
            video_uri=video_uri,
            content_json=content_json,
            source=content.metadata.source,
            created_at=now,
        ))
    # 无 relationship()：影片行（父）先 flush 落库，场景行外键才成立
    session.flush()

    # ---------- 场景资产：asset_id = f"{movie}::{scene_id}"，重复导入 upsert ----------
    scene_asset_ids: list[str] = []
    for scene in content.content.scenes:
        scene_asset_id = f"{asset_id}::{scene.scene_id}"
        scene_duration = scene.time_range.end_ms - scene.time_range.start_ms
        scene_existing = session.get(MediaAsset, scene_asset_id)
        if scene_existing is not None:
            # 同 scene_id 重复导入：更新非键字段，不重复插行
            scene_existing.title = scene.description
            scene_existing.duration_ms = scene_duration
            scene_existing.video_uri = video_uri
        else:
            session.add(MediaAsset(
                asset_id=scene_asset_id,
                asset_type="scene",
                parent_id=asset_id,
                title=scene.description,  # title 列 NOT NULL，场景标题取描述
                duration_ms=scene_duration,
                video_uri=video_uri,
                created_at=now,
            ))
        scene_asset_ids.append(scene_asset_id)
    session.flush()
    session.commit()
    return ImportResult(asset_id=asset_id, scene_asset_ids=scene_asset_ids)
