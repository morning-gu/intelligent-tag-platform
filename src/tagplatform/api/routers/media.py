"""`/api/v1/media/*` 上传与「上传即生成资产」路由。

上传语义（2026-11 起）：登记资产（存文件 + media_asset 行），并用**文件名**
去数据集里匹配影片——命中就建一条后台生成任务，逐场景跑多模态模型，把
生成结果以同一条资产的 asset_id 导入平台；没命中就是普通占位资产。

这样前端只需"选一个 mp4"，不需要填片名：文件名带片名即可
（`The.Godfather.1972.1080p.mkv`），带 IMDb id（`tt0068646.mp4`）最准。

权限说明（设计如此，报告注明）：登录即可上传，未设 admin 门槛——与其他业务
router 一致经 create_app include 时统一挂 protected_deps。

薄路由层：后缀白名单 / 大小上限 / uuid 存盘 / 资产行登记在核心层
tagplatform.media；匹配与生成编排在 tagplatform.asset_build；本 router 只做
multipart 提取与错误映射：
- 后缀非白名单（UnsupportedVideoFormat）→ 422 {"detail": "不支持的视频格式"}；
- 超上限（UploadTooLarge）→ 413，两条拦截路径（Content-Length 头早拒 +
  核心层流式累计兜底）。
"""

import json
from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from tagplatform import asset_build, media
from tagplatform.api import get_session, get_settings_dep
from tagplatform.assets import source_tag
from tagplatform.db.models import AssetBuildTask, MediaAsset, MediaContext
from tagplatform.settings import Settings

router = APIRouter(prefix="/api/v1")


class MediaUploadResponse(BaseModel):
    """上传响应：资产标识 + 标题 + 落盘路径 + 数据集匹配与任务信息。"""

    asset_id: str
    title: str
    video_uri: str
    # 文件名命中数据集时非空，前端据此轮询 build_status
    movie_id: str | None = None
    matched_title: str | None = None
    match_how: str | None = None
    build_task_id: str | None = None


class MediaMatchResponse(BaseModel):
    """选完文件后的预检结果：命中哪部片、该片是否已有可直接复用的内容。"""

    matched: bool
    movie_id: str | None = None
    matched_title: str | None = None
    match_how: str | None = None
    asset_id: str | None = None
    # 已有**当前口径**的内容 → 不必再传视频；口径不符则仍会重建
    has_content: bool = False
    content_source: str | None = None
    scene_count: int = 0
    built_at: datetime | None = None


class BuildTaskOut(BaseModel):
    """生成任务进度：前端轮询「正在分析第 done/total 个场景」。"""

    task_id: str
    asset_id: str
    movie_id: str
    status: str
    total_scenes: int
    done_scenes: int
    error: str | None = None


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


@router.get("/media/match", response_model=MediaMatchResponse)
def match_media(
    filename: str,
    settings: Settings = Depends(get_settings_dep),
    session: Session = Depends(get_session),
) -> MediaMatchResponse:
    """按文件名预检：命中的影片是否已生成过资产。

    只读接口——不落盘、不建任务，供前端在**选完文件、点上传之前**
    判断"这部片已经有资产了，不用再传一遍视频"。

    内容口径与后台 `execute` 的复用判断一致：只有 source 等于当前
    asset_infer 口径的行才算"可直接复用"，口径不符时上传仍会重建。
    """
    matched = asset_build.match_filename(filename, settings.dataset_root)
    if matched is None:
        return MediaMatchResponse(matched=False)

    asset_id = f"movie_{matched.movie.movie_id}"
    rows = (session.query(MediaContext)
            .filter(MediaContext.asset_id == asset_id)
            .order_by(MediaContext.created_at)
            .all())
    wanted = source_tag(settings.asset_infer)
    usable = [row for row in rows if row.source == wanted]
    latest = (usable or rows or [None])[-1]
    scene_count = 0
    if latest is not None:
        try:
            payload = json.loads(latest.content_json)
            scene_count = len((payload.get("content") or {}).get("scenes") or [])
        except (ValueError, AttributeError):
            scene_count = 0
    return MediaMatchResponse(
        matched=True,
        movie_id=matched.movie.movie_id,
        matched_title=matched.movie.title,
        match_how=matched.how,
        asset_id=asset_id,
        has_content=bool(usable),
        content_source=latest.source if latest is not None else None,
        scene_count=scene_count,
        built_at=latest.created_at if latest is not None else None,
    )


@router.post("/media/upload", response_model=MediaUploadResponse)
def upload_media(
    request: Request,
    file: UploadFile,
    asset_id: str | None = Form(None),
    auto_build: bool = Form(True),
    force: bool = Form(False),
    settings: Settings = Depends(get_settings_dep),
    session: Session = Depends(get_session),
) -> MediaUploadResponse:
    """上传视频：登记资产；文件名命中数据集则异步生成资产内容。

    同步 def：磁盘落盘与登记为阻塞操作，走线程池不占事件循环。进入本
    函数时 multipart 已由 FastAPI 收完解析（磁盘 spool，内存不上爆），
    先按 Content-Length 头早拒，再交核心层流式落盘与登记。

    force=True：该片已有同口径内容也重新生成（前端"仍然重新生成"）。
    """
    enforce_upload_size_limit(request)
    filename = file.filename or ""
    asset_id = (asset_id or "").strip() or None

    # 客户端显式指定 asset_id 时只允许"预留主键"：已绑定视频的资产不能被
    # 覆盖，否则任何登录用户都能把别人的视频换掉。自动匹配出的
    # movie_<id> 不受此限——那代表同一部片的重复上传，覆盖是预期行为。
    if asset_id is not None:
        existing = session.get(MediaAsset, asset_id)
        if existing is not None and existing.video_uri:
            raise HTTPException(
                status_code=409,
                detail="资产已绑定视频，不能通过上传覆盖") from None

    # 先匹配再存盘：命中数据集时资产直接用 movie_<id> 作为主键，
    # 重复上传同一部片不会产生一堆 asset_xxxx 孤儿行
    matched = asset_build.match_filename(filename, settings.dataset_root)
    if matched is not None and asset_id is None:
        asset_id = f"movie_{matched.movie.movie_id}"

    try:
        stored = media.save_upload(
            file.file, filename, session, settings.media_root,
            asset_id=asset_id)
    except media.UnsupportedVideoFormat:
        raise HTTPException(
            status_code=422, detail="不支持的视频格式") from None
    except media.UploadTooLarge:
        raise HTTPException(
            status_code=413, detail="文件大小超出上传上限") from None

    task = None
    if matched is not None and auto_build:
        task = asset_build.create_task(session, stored.asset_id,
                                       matched.movie.movie_id)
        asset_build.submit(task.task_id, settings, force=force)

    return MediaUploadResponse(
        asset_id=stored.asset_id,
        title=stored.title,
        video_uri=stored.video_uri,
        movie_id=matched.movie.movie_id if matched else None,
        matched_title=matched.movie.title if matched else None,
        match_how=matched.how if matched else None,
        build_task_id=task.task_id if task else None,
    )


@router.get("/media/build-tasks/{task_id}", response_model=BuildTaskOut)
def get_build_task(task_id: str, session: Session = Depends(get_session)) -> BuildTaskOut:
    """查询生成任务进度；未知 task_id → 404。"""
    task = session.get(AssetBuildTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    return _task_out(task)


@router.get("/media/build-tasks", response_model=list[BuildTaskOut])
def list_build_tasks(asset_id: str | None = None,
                     session: Session = Depends(get_session)) -> list[BuildTaskOut]:
    """任务列表：按 asset_id 过滤，创建时间降序。"""
    query = session.query(AssetBuildTask)
    if asset_id:
        query = query.filter(AssetBuildTask.asset_id == asset_id)
    rows = query.order_by(AssetBuildTask.created_at.desc()).all()
    return [_task_out(row) for row in rows]


_PROGRESS_PAGE = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><title>生成进度</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body{font:15px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif;margin:40px auto;
max-width:640px;padding:0 20px;color:#1f2328}
h1{font-size:18px;margin:0 0 4px}
.bar{height:10px;background:#eaeef2;border-radius:6px;overflow:hidden;margin:18px 0 8px}
.fill{height:100%;width:0;background:#2f81f7;transition:width .4s}
.meta{color:#59636e;font-size:13px}
.ok{color:#1a7f37}.bad{color:#cf222e}
code{background:#f6f8fa;padding:1px 5px;border-radius:4px;font-size:13px}
</style></head><body>
<h1>资产生成进度</h1>
<div class="meta">任务 <code>__TASK__</code></div>
<div class="bar"><div class="fill" id="fill"></div></div>
<div id="line" class="meta">读取中…</div>
<div id="extra" class="meta" style="margin-top:14px"></div>
<script>
const TASK = "__TASK__";
async function tick(){
  try{
    const r = await fetch("/api/v1/media/build-tasks/" + TASK, {credentials:"include"});
    if(!r.ok){ document.getElementById("line").textContent = "查询失败 HTTP " + r.status; return; }
    const t = await r.json();
    const pct = t.total_scenes ? Math.round(100*t.done_scenes/t.total_scenes) : 0;
    document.getElementById("fill").style.width = pct + "%";
    const label = {queued:"排队中",running:"生成中",succeeded:"已完成",failed:"失败"}[t.status] || t.status;
    const line = document.getElementById("line");
    line.textContent = label + "  " + t.done_scenes + "/" + (t.total_scenes || "?") + " 场景  (" + pct + "%)";
    line.className = "meta " + (t.status==="succeeded"?"ok":t.status==="failed"?"bad":"");
    const extra = document.getElementById("extra");
    if(t.status === "succeeded"){
      extra.innerHTML = "资产 <code>" + t.asset_id + "</code> 已可查看（素材页刷新即可）。";
    } else if(t.status === "failed"){
      extra.textContent = "错误：" + (t.error || "未知");
    }
    if(t.status === "succeeded" || t.status === "failed") return;
  }catch(e){ document.getElementById("line").textContent = "网络错误：" + e; }
  setTimeout(tick, 2000);
}
tick();
</script></body></html>"""


@router.get("/media/build-tasks/{task_id}/view", response_class=HTMLResponse,
            include_in_schema=False)
def build_task_view(task_id: str, session: Session = Depends(get_session)) -> HTMLResponse:
    """进度页（不依赖前端构建）：浏览器打开即自动轮询，替代 dist 里缺失的进度条。"""
    if session.get(AssetBuildTask, task_id) is None:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    return HTMLResponse(_PROGRESS_PAGE.replace("__TASK__", task_id))


def _task_out(task: AssetBuildTask) -> BuildTaskOut:
    return BuildTaskOut(
        task_id=task.task_id, asset_id=task.asset_id, movie_id=task.movie_id,
        status=task.status, total_scenes=task.total_scenes,
        done_scenes=task.done_scenes, error=task.error,
    )
