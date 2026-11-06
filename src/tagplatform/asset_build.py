"""上传视频 → 自动生成资产：数据集匹配、后台任务、落库。

用户在前端上传任意 mp4（内容无所谓），文件名里通常带片名。本模块：

1. `match_filename`：文件名 → 数据集影片（`assets/dataset.py` 的匹配规则）；
2. `asset_for_movie`：这部片已有的资产（按构建任务反查，id 与影片一一对应）；
3. `create_task` + `submit`：建任务行并丢进单线程池（生成要几分钟，不能
   卡住 HTTP 请求）；
4. `execute`：跑 `AssetBuilder` 逐场景生成 → 把结果写进**那一条资产**
   （`content_import` 覆盖 `content_json`）。

语义（2026-11 起）：任务可以重复建，资产不重复——同一部片永远只有一个
asset_id，重新生成就是覆盖旧内容。是否"已有资产、要不要重新生成"由前端
在预检时问用户，后端不再做"已有内容就秒回"的短路。

分层红线：本模块不 import fastapi；HTTP 映射在 api/routers/media.py。
"""
from __future__ import annotations

import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import delete, or_
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


def asset_for_movie(session: Session, movie_id: str) -> MediaAsset | None:
    """这部片当前的资产行：按最近的构建任务反查（没有则 None）。

    资产 id 是随机的 `asset_xxxxxxxx`，与影片 id 无字面关系，所以"同一部片
    是否已有资产"只能经由任务表的 movie_id 反查。任务可以有多条，资产只有
    一条——最近一条任务的 asset_id 就是它。
    """
    task = (session.query(AssetBuildTask)
            .filter(AssetBuildTask.movie_id == movie_id)
            .order_by(AssetBuildTask.created_at.desc(),
                      AssetBuildTask.task_id.desc())
            .first())
    return session.get(MediaAsset, task.asset_id) if task is not None else None


def create_task(session: Session, asset_id: str, movie_id: str, *,
                infer: bool = True) -> AssetBuildTask:
    """建一条排队任务（调用方负责随后 submit）。

    infer 落到任务行上，而不是只读全局配置：同一部片可以这次"只填数据集
    事实"、下次"让模型补字段"，两次的内容口径（source）不同，事后能查。
    """
    task = AssetBuildTask(
        task_id=f"build_{uuid.uuid4().hex[:8]}",
        asset_id=asset_id, movie_id=movie_id, infer=infer, status="queued",
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


def submit(task_id: str, settings: Settings | None = None) -> None:
    """把任务丢进后台线程池（返回不等于完成）。

    settings 显式传入：后台线程不能依赖进程环境变量重新解析配置，
    否则测试实例会连到别的库（`get_settings()` 只在没传时兜底）。
    """
    _executor().submit(execute, task_id, settings)


def _asset_out_dir(settings: Settings) -> Path:
    """资产 JSON 落盘目录：与数据库同级的 assets/（便于事后核对）。"""
    return Path(settings.database_path).resolve().parent / "assets"


def _purge_contexts(session: Session, asset_id: str, *,
                    commit: bool = True) -> None:
    """删掉该资产及其场景子资产（`<asset_id>::<scene>`）的旧 context。

    过渡期 `content_import` 仍会新建 context 行；重新生成前清掉旧的，标签
    生成侧读到的才是这一次的内容（否则会堆多份、取"最新"全靠时间排序）。
    """
    # LIKE 的 % 与 _ 是通配符：asset_id 里出现下划线（当前 id 规则就会有）
    # 时必须转义，否则可能匹配到相邻前缀的 context
    escaped = (asset_id.replace("\\", "\\\\")
               .replace("%", "\\%").replace("_", "\\_"))
    session.execute(delete(MediaContext).where(or_(
        MediaContext.asset_id == asset_id,
        MediaContext.asset_id.like(f"{escaped}::%", escape="\\"),
    )))
    if commit:
        session.commit()


def execute(task_id: str, settings: Settings | None = None) -> None:
    """后台执行：生成 → 覆盖内容 → 更新任务状态。任何异常都落成 failed。

    不做「已有内容就秒回」：要不要重新生成由用户在预检提示里决定，建了
    任务就一定真跑一遍，任务表因此每次都能如实反映这一轮跑了什么。

    是否用大模型补字段看**任务行**的 infer（上传时前端选的那一项）。
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
            infer=bool(task.infer),
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
        content.metadata.source = source_tag(bool(task.infer))
        # 旧 context 与新内容同一次提交里替换（过渡期双写，标签侧仍读 context）
        _purge_contexts(session, task.asset_id, commit=False)
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
