"""内容导入服务：统一内容 JSON → media_asset 层级 + media_context 落库。

分层红线：本模块不得 import fastapi（设计文档 §9.4）；video_root 非法
抛 ValueError，由 API 层（Task 11）捕获转 422。

落库模型（2026-11 起）：**一条影片资产一行，内容整块覆盖**
- `media_asset.content_json` 存标准资产 JSON（结构对齐 new_json.txt），
  重新生成时直接覆盖，不再每次导入新增一份；
- `media_asset.source` 记内容口径（v5-infer / v5-facts）；
- `media_asset.updated_at` 随每次写入刷新；
- 场景行是物理切片：`title = {片名}_{scene_id}`，不存内容；
- `media_context` 为**过渡期双写**：标签生成侧仍读它，等那侧切到
  `content_json` 后整表删除。

契约规则：
- **video_root 校验**：非 None 时必须为已存在目录（Path.is_dir()），否则
  抛 ValueError("video_root 不存在: ...")；
- **parent 校验**：新建资产时 parent_id 非空则必须为已存在资产，否则抛
  ValueError("parent 资产不存在: ...")——拦截外键 IntegrityError 逃逸；
- **asset_id**：content.asset.asset_id 非空则 upsert（只更新 title/
  duration_ms/video_uri/content_json/source/updated_at，parent 冻结）；
  为空则生成 `asset_` + uuid4().hex[:8]，并回写 content.asset.asset_id，
  保证表列与 content_json 一致；
- **场景资产**：asset_id = f"{movie_asset_id}::{scene_id}"、asset_type=
  "scene"、duration_ms = end - start、video_uri 同影片；同 scene_id 重复
  导入 upsert；本轮不再出现的场景连同其标签一起删除（否则旧场景会以
  孤儿行挂着，标签证据也对不上）；
- **media_context**：照旧新建一行（过渡期双写），source/model_version/
  recognition_status 取 metadata、embedding 留空。

实现注意：
- MediaAsset.parent_id 外键无 relationship()：影片行先 flush 落库，
  场景行（子行）后插，避免外键先炸（同 Task 3/7 教训）；
- 时间戳列无默认值，插入/更新时显式赋 UTC now；
- 删除场景行前先删引用它的 asset_tag（及其 review_record），否则外键
  约束会挡住。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlparse

from sqlalchemy import delete
from sqlalchemy.orm import Session

from tagplatform.content_io import dump_for_storage, is_upstream_shape
from tagplatform.content_template import to_template, to_template_from_content
from tagplatform.db.models import (
    AssetTag,
    MediaAsset,
    MediaContext,
    ReviewRecord,
)
from tagplatform.schemas import ContentJSON
from tagplatform.schemas.upstream import parse_upstream


@dataclass
class ImportResult:
    """导入结果：影片 asset_id、本次新建 context_id、场景 asset_id 列表。"""

    asset_id: str
    context_id: str
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


def scene_title(movie_title: str, scene_id: str) -> str:
    """场景资产标题：`{片名}_{scene_id}`（下划线连接，前端列表直接可读）。"""
    return f"{movie_title}_{scene_id}"


def _upsert_movie(
    session: Session, content: ContentJSON, asset_id: str,
    video_uri: str | None, stored_json: str, now: datetime,
) -> MediaAsset:
    """写入/覆盖影片行（asset_id 已解析好，JSON 里也是同一个）。"""
    movie = session.get(MediaAsset, asset_id)
    if movie is not None:
        # upsert 只更新内容相关列：asset_type/parent_id 冻结（主键不可变更）
        movie.title = content.asset.title
        movie.duration_ms = content.asset.duration_ms
        # video_uri 为 None 时保留原值：上传绑定的文件不能被内容 JSON 抹掉
        if video_uri is not None:
            movie.video_uri = video_uri
        movie.content_json = stored_json
        movie.source = content.metadata.source
        movie.updated_at = now
        return movie

    # 写库前校验父资产存在性：parent_id 有外键约束，指向不存在资产时
    # 影片行 flush 会抛未捕获 IntegrityError（客户端拿裸 500）
    if content.asset.parent_id and session.get(
            MediaAsset, content.asset.parent_id) is None:
        raise ValueError(f"parent 资产不存在: {content.asset.parent_id}")
    movie = MediaAsset(
        asset_id=asset_id,
        asset_type=content.asset.asset_type,
        parent_id=content.asset.parent_id or None,
        title=content.asset.title,
        duration_ms=content.asset.duration_ms,
        video_uri=video_uri,
        content_json=stored_json,
        source=content.metadata.source,
        created_at=now,
        updated_at=now,
    )
    session.add(movie)
    return movie


def _upsert_scenes(
    session: Session, content: ContentJSON, asset_id: str,
    video_uri: str | None, now: datetime,
) -> list[str]:
    """按 content.content.scenes 逐条 upsert 场景行；返回本次的场景 asset_id。"""
    scene_asset_ids: list[str] = []
    for scene in content.content.scenes:
        scene_asset_id = f"{asset_id}::{scene.scene_id}"
        duration = scene.time_range.end_ms - scene.time_range.start_ms
        row = session.get(MediaAsset, scene_asset_id)
        if row is not None:
            row.title = scene_title(content.asset.title, scene.scene_id)
            row.duration_ms = duration
            row.video_uri = video_uri
            row.updated_at = now
        else:
            session.add(MediaAsset(
                asset_id=scene_asset_id,
                asset_type="scene",
                parent_id=asset_id,
                title=scene_title(content.asset.title, scene.scene_id),
                duration_ms=duration,
                video_uri=video_uri,
                created_at=now,
                updated_at=now,
            ))
        scene_asset_ids.append(scene_asset_id)
    return scene_asset_ids


def _prune_scenes(session: Session, asset_id: str, keep: list[str]) -> None:
    """删掉本次内容里已不存在的场景行（连同挂在上面的标签）。

    场景切分变化（换了生成版本、调整了场景规划）时，旧场景若留着会变成
    没有父级内容的孤儿行，标签证据也指向不存在的场景。
    """
    query = session.query(MediaAsset.asset_id).filter(
        MediaAsset.parent_id == asset_id,
        MediaAsset.asset_type == "scene",
    )
    if keep:
        # 本次内容里没有的场景才是要删的；keep 为空说明一个都不留
        query = query.filter(MediaAsset.asset_id.notin_(keep))
    stale = [row[0] for row in query]
    if not stale:
        return
    tag_ids = [row[0] for row in session.query(AssetTag.id).filter(
        AssetTag.asset_id.in_(stale))]
    if tag_ids:
        session.execute(delete(ReviewRecord).where(
            ReviewRecord.result_id.in_(tag_ids)))
        session.execute(delete(AssetTag).where(AssetTag.id.in_(tag_ids)))
    session.execute(delete(MediaAsset).where(MediaAsset.asset_id.in_(stale)))


def _write_context(
    session: Session, content: ContentJSON, asset_id: str,
    stored_json: str, now: datetime,
) -> str:
    """过渡期双写 media_context（标签生成侧读它）；返回新 context_id。"""
    context_id = f"ctx_{uuid.uuid4().hex[:8]}"
    session.add(MediaContext(
        context_id=context_id,
        asset_id=asset_id,
        content_json=stored_json,
        embedding=None,  # 留空，由嵌入生产任务写入
        source=content.metadata.source,
        model_version=content.metadata.model_version,
        recognition_status=content.metadata.recognition_status,
        created_at=now,
    ))
    return context_id


def import_content(
    content: ContentJSON, session: Session, video_root: str | None,
    raw: dict | None = None,
) -> ImportResult:
    """导入统一内容 JSON：影片行覆盖内容 + 场景行 upsert + context 双写。

    video_root 非 None 时必须为已存在目录，否则抛 ValueError（任何
    数据库写入之前先校验，失败零副作用）。

    raw 是**调用方收到的原始 JSON**（上游模板形态）。给了就按上游模型取
    全量字段（保留 `emotion.code` / 事件参与人情绪）后落库；不给则由内部
    `ContentJSON` 反推模板。两种路径都写成**完整模板形态**（每个字段都在，
    缺值 null / 空列表 []）——`content_json` 与 API 返回同一口径。
    """
    if video_root is not None and not Path(video_root).is_dir():
        raise ValueError(f"video_root 不存在: {video_root}")

    now = datetime.now(timezone.utc)
    video_uri = (
        resolve_video_path(content.asset.video_uri, video_root)
        if content.asset.video_uri is not None
        else None
    )
    # 先定 asset_id：没给就生成，并回写进 content，保证表列与 content_json
    # 里的 asset.asset_id 一致（下游拿 JSON 也能对上资产）
    asset_id = content.asset.asset_id or f"asset_{uuid.uuid4().hex[:8]}"
    content.asset.asset_id = asset_id
    stored = (
        to_template(parse_upstream(raw))
        if raw is not None and is_upstream_shape(raw)
        else to_template_from_content(content)
    )
    # 表列是主键的唯一事实来源：JSON 里的 asset.asset_id 一律对齐到它
    stored["asset"]["asset_id"] = asset_id
    stored_json = dump_for_storage(stored)

    _upsert_movie(session, content, asset_id, video_uri, stored_json, now)
    # 无 relationship()：影片行（父）先 flush 落库，场景行外键才成立
    session.flush()

    scene_asset_ids = _upsert_scenes(session, content, asset_id, video_uri, now)
    session.flush()
    _prune_scenes(session, asset_id, scene_asset_ids)

    context_id = _write_context(session, content, asset_id, stored_json, now)
    session.commit()
    return ImportResult(
        asset_id=asset_id, context_id=context_id, scene_asset_ids=scene_asset_ids
    )
