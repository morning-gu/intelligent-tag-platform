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
import re
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
from tagplatform.db.models import AssetBuildTask, MediaAsset
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


# 文件名里的发布组噪音：这些词不参与片名比对（`Memento.1080p.BluRay.x264`
# 与资产标题 `Memento` 是同一部片）。
_NOISE_TOKENS = frozenset({
    "1080p", "720p", "2160p", "480p", "4k", "8k", "uhd",
    "bluray", "blu", "ray", "brrip", "bdrip", "webrip", "web", "dl", "webdl",
    "hdrip", "dvdrip", "hdtv", "remux", "hdr", "sdr", "10bit", "8bit",
    "x264", "x265", "h264", "h265", "hevc", "avc", "aac", "ac3", "eac3",
    "dts", "ddp", "dd", "atmos", "truehd", "flac", "mp3",
    "proper", "repack", "extended", "unrated", "imax", "directors", "cut",
    "multi", "dual", "chs", "cht", "eng", "jpn", "kor", "国语", "粤语",
    "中字", "字幕", "双语", "高清", "蓝光",
})
_YEAR_RE = re.compile(r"^(19|20)\d{2}$")
# 音轨写法（DDP5.1 会被切开成 ddp5 + 1）：按前缀词根整体丢掉
_CODEC_RE = re.compile(r"^(ddp|dd|dts|aac|ac3|eac3|truehd|atmos|mp3|flac|opus)\d*$")


def name_tokens(name: str) -> str:
    """片名/文件名 → 可比较的词序列（丢掉年份、分辨率、编码等噪音）。

    `Pride.and.Prejudice.2005.mp4` 与资产标题 `Pride & Prejudice` 归一化后
    都是 `pride and prejudice`；`&` 统一成 `and` 是为了两种写法能对上。
    """
    stem = Path(name or "").stem.lower()
    stem = stem.replace("&", " and ")
    # 去掉 [1080p]、(2005) 这类括注：里面通常是发布信息而不是片名
    stem = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", stem)
    stem = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", " ", stem)
    words = [word for word in stem.split()
             if word
             and word not in _NOISE_TOKENS
             and not _YEAR_RE.match(word)
             and not _CODEC_RE.match(word)
             and not word.isdigit()]
    return " ".join(words)


def match_asset_title(session: Session, filename: str) -> MediaAsset | None:
    """文件名 → 既有资产（按标题兜底，供数据集索引命中不到时使用）。

    为什么需要这层兜底：`/media/match` 原先只认数据集索引，文件名一旦不含
    英文片名/IMDb id（中文名、改名、或数据集目录不可读），命中不到就等于
    "没这部片"，于是重复上传静默通过。这里改成再按**我们自己的**
    `media_asset.title` 比一遍——"已有资产"的事实本来就只在我们库里。

    比对规则（`name_tokens` 归一化后）：
    - 完全相等优先；
    - 否则片名词序列被文件名包含（片名 ≥5 字符，避免 "It" 这类误命中）；
    - 多命中时优先有内容、其次更新时间最新的那行。

    已知取舍：包含匹配会把 "The Godfather Part II" 这类续集也算成
    "The Godfather"（归一化后前者包含后者）。这里刻意接受——这类片数据集里
    本来也建不出来（没有 IMDb id 就无法重建），宁可提示"已有同名资产"让人
    改名带上 IMDb id，也不要静默多建一条永远生成不了内容的占位资产。
    """
    target = name_tokens(filename)
    if not target:
        return None
    rows = (session.query(MediaAsset)
            .filter(MediaAsset.asset_type == "movie")
            .all())
    exact: list[MediaAsset] = []
    partial: list[MediaAsset] = []
    for asset in rows:
        title = name_tokens(asset.title)
        if not title:
            continue
        if title == target:
            exact.append(asset)
        elif len(title) >= 5 and title in target:
            partial.append(asset)
    pool = exact or partial
    if not pool:
        return None
    pool.sort(key=lambda a: (bool(a.content_json),
                             a.updated_at or datetime(1970, 1, 1)),
              reverse=True)
    return pool[0]


def create_task(session: Session, asset_id: str, movie_id: str) -> AssetBuildTask:
    """建一条排队任务（调用方负责随后 submit）。

    任务行只记"跑了哪部片、什么状态"，不记推断口径：口径是执行参数
    （全局配置 `asset_infer`，上传时可逐次覆盖），与 main 的表结构完全一致。
    """
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
           infer: bool | None = None) -> None:
    """把任务丢进后台线程池（返回不等于完成）。

    settings 显式传入：后台线程不能依赖进程环境变量重新解析配置，
    否则测试实例会连到别的库（`get_settings()` 只在没传时兜底）。
    infer 是本次上传选的推断口径（前端勾选项），None 表示用全局配置。
    """
    _executor().submit(execute, task_id, settings, infer)


def _asset_out_dir(settings: Settings) -> Path:
    """资产 JSON 落盘目录：与数据库同级的 assets/（便于事后核对）。"""
    return Path(settings.database_path).resolve().parent / "assets"


def execute(task_id: str, settings: Settings | None = None, *,
            infer: bool | None = None) -> None:
    """后台执行：生成 → 覆盖内容 → 更新任务状态。任何异常都落成 failed。

    不做「已有内容就秒回」：要不要重新生成由用户在预检提示里决定，建了
    任务就一定真跑一遍，任务表因此每次都能如实反映这一轮跑了什么。
    （main 的「同口径秒回」刻意不合并，否则用户在弹窗里点"重新生成"后
    后端会直接返回成功而内容没变。）

    是否用大模型补字段由**执行参数**决定：上传时前端勾选的值经 `infer`
    透传进来，缺省（如重跑历史任务、脚本调用）则取全局 `settings.asset_infer`。
    口径不落任务行——任务表结构与 main 完全一致。
    """
    settings = settings or get_settings()
    effective_infer = settings.asset_infer if infer is None else infer
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
            infer=effective_infer,
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
        content.metadata.source = source_tag(effective_infer)
        # 内容整块覆盖同一条资产行（v1 的 media_context 已并入 media_asset）
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
