"""上传视频 → 自动生成资产：数据集匹配、后台任务、落库。

用户在前端上传任意 mp4（内容无所谓），文件名里通常带片名。本模块：

1. `match_filename`：文件名 → 数据集影片（`assets/dataset.py` 的匹配规则）；
2. `create_task` + `submit`：建任务行并丢进单线程池（生成要几分钟，不能
   卡住 HTTP 请求）；
3. `execute`：跑 `AssetBuilder` 逐场景生成 → 把结果以**上传那条资产**的
   asset_id 导入平台（`content_import` upsert），前端刷新即可看到内容。

已生成过的资产再上传同一个片名：直接命中已有 context，秒回（不烧 token）。

分层红线：本模块不 import fastapi；HTTP 映射在 api/routers/media.py。
"""
from __future__ import annotations

import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from tagplatform.adapters.vision import to_content_json
from tagplatform.assets import AssetBuilder, MatchResult, build_index, match, source_tag
from tagplatform.content_import import import_content
from tagplatform.db import create_engine_for, make_session_factory
from tagplatform.db.models import AssetBuildTask, MediaAsset, MediaContext
from tagplatform.schemas.upstream import parse_upstream
from tagplatform.settings import Settings, get_settings

_INDEX_LOCK = threading.Lock()
_INDEX_CACHE: dict[str, list] = {}

_EXECUTOR: ThreadPoolExecutor | None = None
_EXECUTOR_LOCK = threading.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def movie_index(root: str) -> list:
    """数据集索引（进程内缓存；扫描一次约 1–3 秒）。"""
    with _INDEX_LOCK:
        if root not in _INDEX_CACHE:
            _INDEX_CACHE[root] = build_index(Path(root))
        return _INDEX_CACHE[root]


def match_filename(filename: str, dataset_root: str) -> MatchResult | None:
    """上传文件名 → 数据集影片；数据集不可读或没命中返回 None。"""
    try:
        return match(filename, movie_index(dataset_root))
    except OSError:
        return None


def create_task(session: Session, asset_id: str, movie_id: str) -> AssetBuildTask:
    """建一条排队任务（调用方负责随后 submit）。"""
    task = AssetBuildTask(
        task_id=f"build_{uuid.uuid4().hex[:8]}",
        asset_id=asset_id, movie_id=movie_id, status="queued",
        total_scenes=0, done_scenes=0, created_at=_now(),
    )
    session.add(task)
    session.commit()
    return task


def _executor() -> ThreadPoolExecutor:
    """单线程池：串行跑生成，避免多人同时上传把机器压满。"""
    global _EXECUTOR
    with _EXECUTOR_LOCK:
        if _EXECUTOR is None:
            _EXECUTOR = ThreadPoolExecutor(max_workers=1,
                                           thread_name_prefix="asset-build")
        return _EXECUTOR


def submit(task_id: str, settings: Settings | None = None, *,
           force: bool = False) -> None:
    """把任务丢进后台线程池（返回不等于完成）。

    settings 显式传入：后台线程不能依赖进程环境变量重新解析配置，
    否则测试实例会连到别的库（`get_settings()` 只在没传时兜底）。

    force：同一口径已生成过也强制重跑（用户在「该片已有资产」提示上
    选了"仍然重新生成"）。
    """
    _executor().submit(execute, task_id, settings, force)


def _asset_out_dir(settings: Settings) -> Path:
    """资产 JSON 落盘目录：与数据库同级的 assets/（便于事后核对）。"""
    return Path(settings.database_path).resolve().parent / "assets"


def _purge_contexts(session: Session, asset_id: str) -> None:
    """删掉该资产及其场景子资产（`<asset_id>::<scene>`）的旧 context。

    重新生成时必须清：`content_import` 是「每次导入新建一行」，不清就会在
    资产详情里堆出多份内容，审核/生成时也说不清依据的是哪一版。
    """
    from sqlalchemy import delete, or_

    # LIKE 的 % 与 _ 是通配符：asset_id 里出现下划线（当前 id 规则就会有）
    # 时必须转义，否则可能匹配到相邻前缀的 context
    escaped = (asset_id.replace("\\", "\\\\")
               .replace("%", "\\%").replace("_", "\\_"))
    session.execute(delete(MediaContext).where(or_(
        MediaContext.asset_id == asset_id,
        MediaContext.asset_id.like(f"{escaped}::%", escape="\\"),
    )))
    session.commit()


def execute(task_id: str, settings: Settings | None = None,
            force: bool = False) -> None:
    """后台执行：生成 → 导入 → 更新任务状态。任何异常都落成 failed。

    force=True 时忽略「同口径已生成过」的复用判断，清掉旧内容重跑
    （用户明确要求重新生成的情形）。
    """
    settings = settings or get_settings()
    engine = create_engine_for(settings.database_path)
    factory = make_session_factory(engine)
    session = factory()
    try:
        task = session.get(AssetBuildTask, task_id)
        if task is None:
            return
        task.status = "running"
        session.commit()

        wanted_source = source_tag(settings.asset_infer)
        existing = (session.query(MediaContext)
                    .filter(MediaContext.asset_id == task.asset_id).all())
        if not force and any(row.source == wanted_source for row in existing):
            # 同一口径已生成过：秒回，不烧 token
            task.status = "succeeded"
            task.done_scenes = task.total_scenes
            task.finished_at = _now()
            session.commit()
            return
        if existing:
            # 旧口径（早期精简资产 / 更早的生成版本），或用户要求强制重跑：
            # 先清掉父与场景子资产的 context，避免堆着多份互相矛盾的内容
            _purge_contexts(session, task.asset_id)

        def progress(done: int, total: int) -> None:
            # 独立 session：回调在生成线程里，避免与主 session 争用
            side = factory()
            try:
                row = side.get(AssetBuildTask, task_id)
                if row is not None:
                    row.done_scenes = done
                    row.total_scenes = total
                    side.commit()
            finally:
                side.close()

        from tagplatform.llm import make_llm_client

        builder = AssetBuilder(
            root=Path(settings.dataset_root),
            out_dir=_asset_out_dir(settings),
            client=make_llm_client(settings),
            k=settings.asset_frames,
            workers=settings.asset_workers,
            dialogue_limit=settings.asset_dialogue_limit,
            progress_cb=progress,
            infer=settings.asset_infer,
        )
        stats = builder.build_movie(task.movie_id, force=True)
        if not stats.output:
            raise RuntimeError("生成未产出文件")

        payload = json.loads(Path(stats.output).read_text(encoding="utf-8"))
        # 关键：这次生成的资产必须落到**上传的那条资产**上（不是新建一条）
        payload["asset"]["asset_id"] = task.asset_id
        asset = session.get(MediaAsset, task.asset_id)
        if asset is not None and asset.video_uri:
            payload["asset"]["video_uri"] = asset.video_uri
        content = to_content_json(parse_upstream(payload))
        # 标记生成口径：下次上传同一部片据此判断「复用」还是「重生成」
        content.metadata.source = wanted_source
        # 原样入库：媒体上下文里存的就是这份交付用模板 JSON
        import_content(content, session, None, raw=payload)

        task = session.get(AssetBuildTask, task_id)
        task.status = "succeeded"
        task.total_scenes = stats.scenes
        task.done_scenes = stats.scenes
        task.finished_at = _now()
        if stats.failed_scenes:
            task.error = "部分场景降级: " + ", ".join(stats.failed_scenes[:5])
        session.commit()
    except Exception as exc:  # noqa: BLE001 —— 后台任务不能把异常抛给线程池
        session.rollback()
        task = session.get(AssetBuildTask, task_id)
        if task is not None:
            task.status = "failed"
            task.error = f"{type(exc).__name__}: {exc}"[:500]
            task.finished_at = _now()
            session.commit()
    finally:
        session.close()
        engine.dispose()
