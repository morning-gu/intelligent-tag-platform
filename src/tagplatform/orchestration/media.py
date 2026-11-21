"""媒体域编排：上传/文件名预检/构建任务/进度页/视频流——orchestration/media 域。

用例清单（原 api/routers/media.py 与 api/routers/video.py 的读路径与装配
逻辑，Task 11 原样下沉；SQL 与装配零语义变化，Task 12 再做进度页外置静态
文件改造）：

- `match_media`         按文件名预检（数据集索引 → 资产标题兜底两级匹配）
- `upload_media`        上传视频登记资产（冲突 409 护栏 ×2、落盘、命中建任务）
- `get_build_task`      构建任务进度查询（404 未知任务）
- `list_build_tasks`    构建任务列表（asset_id 过滤、创建时间降序）
- `build_task_view`     进度页 HTML（浏览器自动轮询，替代缺失的前端进度条）
- `stream_asset_video`  视频流（Range 分块 206 / 越界 416 / 全量 200）

Content-Length 头早拒（413）是纯协议守卫，留在接入层 router（enforce_
upload_size_limit）；核心层流式累计超限兜底在本层 save_upload 错误映射。
"""

import json
from collections.abc import Iterator
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from tagplatform.orchestration._shared import UtcDatetime
from tagplatform.capabilities.storage.models import AssetBuildTask, MediaAsset
from tagplatform.services.content import asset_build, media
from tagplatform.services.content.content_import import resolve_video_path
from tagplatform.settings import Settings


# ---------- 响应模型 ----------

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
    """选完文件后的预检结果：命中哪部片、这部片是否已有资产和内容。"""

    matched: bool
    movie_id: str | None = None
    matched_title: str | None = None
    match_how: str | None = None
    asset_id: str | None = None
    # 已有内容（content_json 非空）→ 前端问用户"是否重新生成"
    has_content: bool = False
    content_source: str | None = None
    scene_count: int = 0
    built_at: UtcDatetime | None = None
    # 能否直接重新生成：只有经数据集索引命中（拿到 movie_id）才行；
    # 按资产标题兜底命中（match_how="asset_title"，中文名/改名/数据集不可读）
    # 只能提醒"已有同名资产"，重建得先把文件名改成含 IMDb id 或英文片名。
    can_rebuild: bool = True


class BuildTaskOut(BaseModel):
    """生成任务进度：前端轮询「正在分析第 done/total 个场景」。"""

    task_id: str
    asset_id: str
    movie_id: str
    status: str
    total_scenes: int
    done_scenes: int
    error: str | None = None


# ---------- 内部辅助 ----------

def _scene_count(asset: MediaAsset | None) -> int:
    """资产内容里的场景数（内容损坏/为空时按 0 算）。"""
    if asset is None or not asset.content_json:
        return 0
    try:
        payload = json.loads(asset.content_json)
        return len((payload.get("content") or {}).get("scenes") or [])
    except (ValueError, AttributeError):
        return 0


def _task_out(task: AssetBuildTask) -> BuildTaskOut:
    return BuildTaskOut(
        task_id=task.task_id, asset_id=task.asset_id, movie_id=task.movie_id,
        status=task.status, total_scenes=task.total_scenes,
        done_scenes=task.done_scenes, error=task.error,
    )


# ---------- 用例：上传与预检 ----------

def match_media(
    session: Session, settings: Settings, filename: str
) -> MediaMatchResponse:
    """按文件名预检：命中哪部片、这部片是否已有资产（有没有内容）。

    只读接口——不落盘、不建任务，供前端在**选完文件、点上传之前**问用户
    "该视频已有对应资产，是否重新生成"。判定只认 `media_asset`：资产行
    存在且 `content_json` 非空，才算已有内容。

    两级匹配：先查数据集索引（能拿到 movie_id，可直接重建）；索引没命中
    时按**资产标题**兜底（`match_how="asset_title"`，`can_rebuild=false`
    ——只能提醒，重建需要文件名带 IMDb id 或英文片名）。没有这层兜底，
    中文名/改过名的文件重复上传时会静默通过。
    """
    matched = asset_build.match_filename(filename, settings.dataset_root)
    if matched is None:
        asset = asset_build.match_asset_title(session, filename)
        if asset is None:
            return MediaMatchResponse(matched=False)
        return MediaMatchResponse(
            matched=True,
            matched_title=asset.title,
            match_how="asset_title",
            asset_id=asset.asset_id,
            has_content=bool(asset.content_json),
            content_source=asset.source,
            scene_count=_scene_count(asset),
            built_at=asset.updated_at,
            can_rebuild=False,
        )

    asset = asset_build.asset_for_movie(session, matched.movie.movie_id)
    if asset is None:
        # 数据集命中但还没建过任务（例如内容是从 JSON 导入的）
        asset = asset_build.match_asset_title(session, filename)
    return MediaMatchResponse(
        matched=True,
        movie_id=matched.movie.movie_id,
        matched_title=matched.movie.title,
        match_how=matched.how,
        asset_id=asset.asset_id if asset is not None else None,
        has_content=bool(asset is not None and asset.content_json),
        content_source=asset.source if asset is not None else None,
        scene_count=_scene_count(asset),
        built_at=asset.updated_at if asset is not None else None,
    )


def upload_media(
    session: Session,
    settings: Settings,
    fileobj,
    filename: str,
    asset_id: str | None,
    auto_build: bool,
    infer: bool | None,
) -> MediaUploadResponse:
    """上传视频：登记资产；文件名命中数据集则异步生成资产内容。

    同步 def：磁盘落盘与登记为阻塞操作，走线程池不占事件循环。

    同一部片重复上传**不产生新资产**：按 movie_id 找到已有资产，只把视频
    地址换掉，内容由后台任务整块覆盖。是否重新生成由前端预检时问用户，
    这里不再有 force 开关。

    infer=False：只落数据集事实（场景描述/字幕/时间），氛围、事件、说话人
    留空，一次模型调用都不发；口径随之变为 asset_builder:v5-facts。
    该项**不落任务行**，只作为本次执行参数透传给后台线程；不传则用全局
    `settings.asset_infer`（与 main 的全局开关语义一致），表结构也不差一列。
    """
    # 客户端显式指定 asset_id 时只允许"预留主键"：已绑定视频的资产不能被
    # 覆盖，否则任何登录用户都能把别人的视频换掉。
    if asset_id is not None:
        existing = session.get(MediaAsset, asset_id)
        if existing is not None and existing.video_uri:
            raise HTTPException(
                status_code=409,
                detail="资产已绑定视频，不能通过上传覆盖") from None

    # 先匹配：命中数据集时复用这部片已有的资产（asset_id 不变、内容覆盖），
    # 没命中就是一条全新的占位资产
    matched = asset_build.match_filename(filename, settings.dataset_root)
    if matched is not None and asset_id is None:
        current = asset_build.asset_for_movie(session, matched.movie.movie_id)
        if current is not None:
            asset_id = current.asset_id

    # 兜底护栏：数据集没命中、但库里已有同名资产 → 拒绝静默重复入库。
    # 这条只会挡"同一部片、文件名认不出"的情况（中文名/改名）；换个带
    # IMDb id 或英文片名的文件名上传即可。前端预检也会提示，这里是不
    # 依赖前端状态的第二道闸。
    if matched is None and asset_id is None:
        duplicate = asset_build.match_asset_title(session, filename)
        if duplicate is not None and duplicate.content_json:
            raise HTTPException(
                status_code=409,
                detail=(f"已有同名资产《{duplicate.title}》，不能重复上传；"
                        "要重新生成请把文件名改成含 IMDb id（如 tt0068646）"
                        "或英文片名再传")) from None

    try:
        stored = media.save_upload(
            fileobj, filename, session, settings.media_root,
            asset_id=asset_id,
            title=matched.movie.title if matched is not None else None)
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
        asset_build.submit(task.task_id, settings, infer=infer)

    return MediaUploadResponse(
        asset_id=stored.asset_id,
        title=stored.title,
        video_uri=stored.video_uri,
        movie_id=matched.movie.movie_id if matched else None,
        matched_title=matched.movie.title if matched else None,
        match_how=matched.how if matched else None,
        build_task_id=task.task_id if task else None,
    )


# ---------- 用例：构建任务 ----------

def get_build_task(session: Session, task_id: str) -> BuildTaskOut:
    """查询生成任务进度；未知 task_id → 404。"""
    task = session.get(AssetBuildTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    return _task_out(task)


def list_build_tasks(session: Session, asset_id: str | None) -> list[BuildTaskOut]:
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


def build_task_view(session: Session, task_id: str) -> HTMLResponse:
    """进度页（不依赖前端构建）：浏览器打开即自动轮询，替代 dist 里缺失的进度条。"""
    if session.get(AssetBuildTask, task_id) is None:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    return HTMLResponse(_PROGRESS_PAGE.replace("__TASK__", task_id))


# ---------- 用例：视频流（原 api/routers/video.py 的读路径与流式装配）----------

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


def stream_asset_video(
    session: Session, asset_id: str, range_header: str | None
):
    """视频流用例：按 Range 分块 / 全量流式返回资产视频文件。

    同步 def：查库与磁盘 IO 为阻塞操作，走线程池不占事件循环。错误映射：
    asset 不存在 → 404；无 video_uri / 文件不存在 → 404
    {"detail": "视频文件不存在"}；Range 起始越界 → 416。

    - 200 全量：FileResponse 磁盘流式（自带 Content-Length；此路径无 Range
      头，不会触发 starlette 内建 range 解析）；
    - Range 语法非法 → 忽略 Range 回 200 全量：流式分块（不用 FileResponse，
      避免 starlette 对同一非法头二次解析回 400/multipart 的契约偏离）；
    - 206 精确分块：按 [start, end] 流式产出（Content-Range 携带总长）。
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
