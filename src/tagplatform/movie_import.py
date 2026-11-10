"""影片资产目录导入：一次把"已备好的资产"注册进库（不经过上传视频）。

来源布局（2026-11 现状，先按此写死；换布局只改 `settings.movie_asset_root`）：

```
<cine_root>/
    <片名>--<imdb_id>/
        content.json          # 上游长视频理解格式（asset/content/scenes）
        frames/
            scene_001_frame_1.jpg
            ...
```

设计取舍：

- **只登记，不生成**：content.json 已经是补全好的内容，直接落 `media_asset`
  （含场景行），一次模型调用都不发；
- **与上传并行**：上传那条路（`/media/upload` → asset_build）一行不改；
  两个入口写同一张表，靠 asset_id 幂等；
- **asset_id 取 content.json 里的 `asset.asset_id`**（`movie_ttXXXXXXX`），
  同一部片重复导入只会覆盖，不会长出新资产；
- **已绑定视频的资产默认跳过**（除非 overwrite=True）：避免"目录导入"把
  上传进来的视频地址冲掉；
- 分层红线：本模块不 import fastapi，错误用 ValueError / 自定义异常表达，
  由路由层转 HTTP 状态码。

帧文件不复制、不移动：生成侧按 imdb id 回这里取帧（见
`generation/mtg/keyframes.py`），目录只是被引用。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from tagplatform.adapters.vision import to_content_json
from tagplatform.content_import import import_content as import_content_svc
from tagplatform.content_io import is_upstream_shape
from tagplatform.content_template import to_template
from tagplatform.db.models import MediaAsset
from tagplatform.schemas.upstream import parse_upstream

#: 目录名里的 imdb 后缀：`<片名>--tt0075314`
_IMDB_SUFFIX_RE = re.compile(r"--(tt\d+)$")
#: 场景帧命名：`scene_001_frame_1.jpg`
_FRAME_RE = re.compile(r"^(?P<scene>scene_\d+)_frame_(?P<idx>\d+)\.(?P<ext>jpe?g|png)$",
                       re.IGNORECASE)


class MovieAssetError(ValueError):
    """目录导入的可预期错误（由路由层转 422/409）。"""


@dataclass(frozen=True)
class MovieDirItem:
    """扫描到的一部影片目录（只读事实，不含 DB 状态）。"""

    dir_name: str
    path: str
    title: str
    imdb_id: str | None
    asset_id: str | None
    scenes: int
    frames: int
    duration_ms: int | None
    size_bytes: int
    mtime: float
    error: str | None = None


@dataclass(frozen=True)
class ImportOutcome:
    """单部影片的导入结果。status: imported | skipped | failed"""

    dir_name: str
    status: str
    asset_id: str | None = None
    title: str | None = None
    scenes: int = 0
    reason: str | None = None
    frames: int = 0


@dataclass
class BulkOutcome:
    """批量导入汇总。"""

    imported: list[ImportOutcome] = field(default_factory=list)
    skipped: list[ImportOutcome] = field(default_factory=list)
    failed: list[ImportOutcome] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        return {"imported": len(self.imported), "skipped": len(self.skipped),
                "failed": len(self.failed)}


def resolve_root(root: str | None) -> Path:
    """导入根目录：显式传入优先，否则用配置里的默认值；不存在直接报错。"""
    from tagplatform.settings import get_settings

    raw = (root or "").strip() or get_settings().movie_asset_root
    if not raw:
        raise MovieAssetError("没有配置影片资产目录（TAGPLT_MOVIE_ASSET_ROOT）")
    path = Path(raw).expanduser()
    if not path.is_dir():
        raise MovieAssetError(f"影片资产目录不存在: {raw}")
    return path


def imdb_of_dir(dir_name: str) -> str | None:
    """目录名 → imdb id（`Rear Window--tt0047396` → `tt0047396`）。"""
    match = _IMDB_SUFFIX_RE.search(dir_name)
    return match.group(1) if match else None


def frames_dir(root: Path, imdb_id: str | None,
               dir_name: str | None = None) -> Path | None:
    """按 imdb id（或目录名）找帧目录；找不到返回 None。

    提供 dir_name 时先直接命中同名目录（最快、无歧义），再按 imdb 后缀扫。
    """
    if dir_name:
        direct = root / dir_name / "frames"
        if direct.is_dir():
            return direct
    if imdb_id:
        for child in root.iterdir():
            if not child.is_dir():
                continue
            if imdb_of_dir(child.name) == imdb_id:
                candidate = child / "frames"
                if candidate.is_dir():
                    return candidate
    return None


def scene_frames(root: Path, imdb_id: str | None, scene_id: str,
                 dir_name: str | None = None) -> tuple[Path, ...]:
    """某场景的帧文件（按 frame 序号排序）；没有返回空元组。"""
    directory = frames_dir(root, imdb_id, dir_name)
    if directory is None:
        return ()
    found: list[tuple[int, Path]] = []
    for file in directory.iterdir():
        if not file.is_file():
            continue
        match = _FRAME_RE.match(file.name)
        if match and match.group("scene") == scene_id:
            found.append((int(match.group("idx")), file))
    found.sort(key=lambda item: item[0])
    return tuple(path for _, path in found)


def _read_payload(content_path: Path) -> dict:
    try:
        data = json.loads(content_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise MovieAssetError("缺少 content.json") from None
    except json.JSONDecodeError as exc:
        raise MovieAssetError(f"content.json 不是合法 JSON: {exc}") from None
    if not isinstance(data, dict):
        raise MovieAssetError("content.json 顶层必须是对象")
    return data


def scan_movie_dirs(root: Path, *, limit: int | None = None) -> list[MovieDirItem]:
    """扫描根目录下的影片目录（每部读一次 content.json 取标题/场景数）。

    单个目录坏掉不影响整体：错误记在 `error` 字段里，前端可以单独跳过。
    """
    items: list[MovieDirItem] = []
    for child in sorted(p for p in root.iterdir() if p.is_dir()):
        if child.name.startswith("_"):
            continue
        content_path = child / "content.json"
        frames = child / "frames"
        frame_files = [f for f in frames.iterdir()
                       if f.is_file() and _FRAME_RE.match(f.name)] if frames.is_dir() else []
        size = sum(f.stat().st_size for f in frame_files)
        mtime = content_path.stat().st_mtime if content_path.exists() else child.stat().st_mtime
        error = None
        title, asset_id, scenes, duration = child.name, None, 0, None
        try:
            data = _read_payload(content_path)
            # 只做一次宽松解析：坏内容在列表里直接标红，别等点了导入才失败
            parse_upstream(data)
            asset = data.get("asset") or {}
            content = data.get("content") or {}
            title = str(asset.get("title") or child.name)
            asset_id = asset.get("asset_id")
            scenes = len(content.get("scenes") or [])
            duration = asset.get("duration_ms")
        except MovieAssetError as exc:
            error = str(exc)
        except Exception as exc:  # noqa: BLE001 —— 上游格式不合法（pydantic 校验）
            error = f"content.json 格式不合法: {exc}"
        items.append(MovieDirItem(
            dir_name=child.name, path=str(child), title=title,
            imdb_id=imdb_of_dir(child.name), asset_id=asset_id,
            scenes=scenes, frames=len(frame_files),
            duration_ms=duration if isinstance(duration, int) else None,
            size_bytes=size, mtime=mtime, error=error))
        if limit is not None and len(items) >= limit:
            break
    return items


def _existing_asset(session: Session, asset_id: str) -> MediaAsset | None:
    return session.get(MediaAsset, asset_id)


def import_movie_dir(session: Session, root: Path, dir_name: str, *,
                     overwrite: bool = False) -> ImportOutcome:
    """把一部影片目录注册成资产（已存在则按 overwrite 决定是否覆盖内容）。"""
    movie_dir = root / dir_name
    if not movie_dir.is_dir():
        return ImportOutcome(dir_name=dir_name, status="failed",
                             reason=f"目录不存在: {dirname_safe(dir_name)}")
    content_path = movie_dir / "content.json"
    frames = scene_frames_count(movie_dir)
    try:
        data = _read_payload(content_path)
        payload = parse_upstream(data)
        content = to_content_json(payload)
    except MovieAssetError as exc:
        return ImportOutcome(dir_name=dir_name, status="failed",
                             reason=str(exc), frames=frames)
    except Exception as exc:  # noqa: BLE001 —— 上游格式异常（pydantic 校验等）
        return ImportOutcome(dir_name=dir_name, status="failed",
                             reason=f"content.json 解析失败: {exc}", frames=frames)

    asset_id = content.asset.asset_id
    if not asset_id:
        return ImportOutcome(dir_name=dir_name, status="failed",
                             reason="content.json 缺少 asset.asset_id",
                             frames=frames)
    existing = _existing_asset(session, asset_id)
    if existing is not None:
        if existing.video_uri and not overwrite:
            return ImportOutcome(
                dir_name=dir_name, status="skipped", asset_id=asset_id,
                title=existing.title, frames=frames,
                reason="该资产已绑定上传的视频，跳过（需要覆盖请显式勾选）")
        if not overwrite and _same_content(existing.content_json, data, asset_id):
            return ImportOutcome(
                dir_name=dir_name, status="skipped", asset_id=asset_id,
                title=existing.title, scenes=len(content.content.scenes),
                frames=frames, reason="内容与库内一致，无需重复导入")

    try:
        result = import_content_svc(content, session, None, raw=data)
    except Exception as exc:  # noqa: BLE001 —— 落库失败不该中断整批
        session.rollback()
        return ImportOutcome(dir_name=dir_name, status="failed",
                             reason=f"写库失败: {exc}", frames=frames)
    return ImportOutcome(dir_name=dir_name, status="imported",
                         asset_id=result.asset_id, title=content.asset.title,
                         scenes=len(content.content.scenes), frames=frames)


def import_many(session: Session, root: Path, dir_names: list[str] | None, *,
                overwrite: bool = False) -> BulkOutcome:
    """批量导入；dir_names 为空表示全部（仍未处理的目录）。"""
    available = [item.dir_name for item in scan_movie_dirs(root)
                 if item.error is None]
    targets = [name for name in (dir_names or available) if name in set(available)]
    outcome = BulkOutcome()
    for name in targets:
        result = import_movie_dir(session, root, name, overwrite=overwrite)
        if result.status == "imported":
            outcome.imported.append(result)
        elif result.status == "skipped":
            outcome.skipped.append(result)
        else:
            outcome.failed.append(result)
    return outcome


def scene_frames_count(movie_dir: Path) -> int:
    """帧目录里的帧文件数（扫描用，不算进 DB）。"""
    frames = movie_dir / "frames"
    if not frames.is_dir():
        return 0
    return sum(1 for f in frames.iterdir() if f.is_file() and _FRAME_RE.match(f.name))


def dirname_safe(name: str) -> str:
    """回显目录名时去掉路径分隔符（防越界/难读）。"""
    return name.replace("/", "").replace("\\", "")


def _same_content(stored_json: str | None, raw: dict,
                  asset_id: str | None) -> bool:
    """库内 content_json 与本次 content.json 是否等价。

    库内是**模板形态**（import_content 会把上游 JSON 规范化后落库），所以
    比较前先把本次 raw 走同一条 `to_template(parse_upstream())`，再按规范化
    哈希对比；直接比原文会永远"不等"，导致重复导入不断重写。
    """
    if not stored_json:
        return False
    try:
        stored = json.loads(stored_json)
        if not isinstance(stored, dict):
            return False
        want = (to_template(parse_upstream(raw)) if is_upstream_shape(raw)
                else dict(raw))
    except Exception:  # noqa: BLE001 —— 解析不了就当成"需要重导"
        return False
    if asset_id:
        want.setdefault("asset", {})["asset_id"] = asset_id
    return _digest(stored) == _digest(want)


def _digest(data: dict) -> str:
    canonical = json.dumps(data, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"))
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()


def touched_at() -> datetime:
    """统一的时间戳来源（UTC），便于测试替换。"""
    return datetime.now(timezone.utc)
