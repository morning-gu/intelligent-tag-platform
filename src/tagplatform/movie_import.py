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
#: 片名结尾的年份括号：`The Godfather (1972)` → 去掉年份再比
_TITLE_YEAR_RE = re.compile(r"\s*[\(\[]\s*(?:19|20)\d{2}\s*[\)\]]\s*$")


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


@dataclass(frozen=True)
class DirEntry:
    """目录里的一项：影片目录（有 content.json）或普通目录（里面可能还有片）。"""

    name: str
    path: str
    #: 这一项本身就是影片目录（目录下有 content.json）
    is_movie: bool
    #: 这一项下面还套着几部片（普通目录才有意义）
    movies: int
    scenes: int = 0
    frames: int = 0
    error: str | None = None
    #: 影片目录里 content.json 声明的 asset_id 与标题
    asset_id: str | None = None
    title: str | None = None
    #: 库里匹配到的已有资产（由路由层用 AssetIndex 填）
    matched_by: str | None = None
    existing_asset_id: str | None = None


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


def browse(path_str: str | None) -> tuple[Path, Path | None, list[DirEntry]]:
    """浏览服务器目录：返回（当前目录, 上级目录, 子项清单）。

    - 不传 path → 用配置里的默认根（前端打开弹窗的落点）；
    - 子项只列**目录**，按名字排序；带 `content.json` 的标成影片目录；
      其余目录统计"里面还套着几部片"，方便一层层点进去；
    - 隐藏目录（`.` 开头）与 `_` 开头的运行期目录照样列出但排在后面：
      服务器上确实存在，不藏起来，免得以为文件没了。
    """
    base = resolve_root(path_str)
    parent = base.parent if base.parent != base else None
    entries: list[DirEntry] = []
    try:
        children = sorted((p for p in base.iterdir() if p.is_dir()),
                          key=_dir_sort_key)
    except OSError as exc:
        raise MovieAssetError(f"读不了目录: {base}（{exc}）") from None

    for child in children:
        if (child / "content.json").exists():
            item = _movie_item(child)
            entries.append(DirEntry(
                name=child.name, path=str(child), is_movie=True, movies=0,
                scenes=item.scenes, frames=item.frames, error=item.error,
                asset_id=item.asset_id, title=item.title))
            continue
        entries.append(DirEntry(
            name=child.name, path=str(child), is_movie=False,
            movies=_count_movies(child)))
    return base, parent, entries


def _dir_sort_key(path: Path) -> tuple[int, str]:
    """排序：普通/影片目录在前（按名），`_` 与 `.` 开头的运行期目录垫后。"""
    return (1 if path.name[:1] in {"_", "."} else 0, path.name.lower())


def _count_movies(directory: Path, *, limit: int = 400) -> int:
    """一个普通目录里套着几部片（有 content.json 的子目录数，封顶 limit 次探测）。"""
    count = 0
    try:
        for index, child in enumerate(directory.iterdir()):
            if index >= limit:
                break
            if child.is_dir() and (child / "content.json").exists():
                count += 1
    except OSError:
        return count
    return count


def _movie_item(child: Path) -> MovieDirItem:
    """单个影片目录 → MovieDirItem（解析失败只记 error，不抛）。"""
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
    return MovieDirItem(
        dir_name=child.name, path=str(child), title=title,
        imdb_id=imdb_of_dir(child.name), asset_id=asset_id,
        scenes=scenes, frames=len(frame_files),
        duration_ms=duration if isinstance(duration, int) else None,
        size_bytes=size, mtime=mtime, error=error)


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
    `_` 开头的运行期目录不算影片。
    """
    items: list[MovieDirItem] = []
    for child in sorted(p for p in root.iterdir() if p.is_dir()):
        if child.name.startswith("_"):
            continue
        items.append(_movie_item(child))
        if limit is not None and len(items) >= limit:
            break
    return items


@dataclass(frozen=True)
class ExistingMatch:
    """目录里的片在库里已有的资产（按 asset_id / imdb / 片名 认出来的）。"""

    asset_id: str
    matched_by: str  # "asset_id" | "imdb" | "title"
    title: str
    has_content: bool
    has_video: bool


def normalize_title(title: str) -> str:
    """片名归一：去首尾空白、去结尾括号里的年份、全角转半角不去、统一小写。"""
    text = (title or "").strip().lower()
    match = _TITLE_YEAR_RE.search(text)
    if match:
        text = text[:match.start()].strip()
    return " ".join(text.split())


def _imdb_of_asset(row: MediaAsset) -> str | None:
    """资产 → imdb id：先看 asset_id 的 `movie_tt` 前缀，再看存的 JSON。"""
    asset_id = row.asset_id or ""
    if asset_id.startswith("movie_"):
        return asset_id[len("movie_"):]
    if row.content_json:
        try:
            stored = json.loads(row.content_json)
            inner = (stored.get("asset") or {}).get("asset_id") or ""
            if inner.startswith("movie_"):
                return inner[len("movie_"):]
        except Exception:  # noqa: BLE001 —— 内容 JSON 坏掉不影响匹配
            return None
    return None


class AssetIndex:
    """库内 movie 资产的匹配索引：asset_id / imdb / 片名 三路。

    存在的意义：手工上传那条路建的资产主键是 `asset_<8位>`，目录里的片是
    `movie_ttXXXX`；同一部片两边主键不同，只按 asset_id 比会全都"没导入"，
    再导一遍就出现两条同名片。这里让目录导入**认到已有资产**，把内容写进
    那一行（保留上传的视频地址），一部片始终只有一行。
    """

    def __init__(self, session: Session) -> None:
        rows = (session.query(MediaAsset)
                .filter(MediaAsset.asset_type == "movie").all())
        self._by_id: dict[str, MediaAsset] = {}
        self._by_imdb: dict[str, MediaAsset] = {}
        self._by_title: dict[str, MediaAsset] = {}
        for row in rows:
            self._by_id[row.asset_id] = row
            imdb = _imdb_of_asset(row)
            if imdb:
                self._by_imdb.setdefault(imdb, row)
            key = normalize_title(row.title)
            if key:
                self._by_title.setdefault(key, row)

    def match(self, asset_id: str | None, imdb: str | None,
              title: str | None) -> ExistingMatch | None:
        """按 asset_id → imdb → 片名 依次匹配；片名匹配要 imdb 不冲突。"""
        row = self._by_id.get(asset_id or "")
        if row is not None:
            return self._wrap(row, "asset_id")
        if imdb:
            row = self._by_imdb.get(imdb)
            if row is not None:
                return self._wrap(row, "imdb")
        key = normalize_title(title or "")
        if key:
            row = self._by_title.get(key)
            if row is not None:
                # 同名但 imdb 不同（重拍片）不算同一部
                other = _imdb_of_asset(row)
                if imdb and other and imdb != other:
                    return None
                return self._wrap(row, "title")
        return None

    @staticmethod
    def _wrap(row: MediaAsset, matched_by: str) -> ExistingMatch:
        return ExistingMatch(asset_id=row.asset_id, matched_by=matched_by,
                             title=row.title,
                             has_content=row.content_json is not None,
                             has_video=row.video_uri is not None)


def import_movie_dir(session: Session, root: Path, dir_name: str, *,
                     overwrite: bool = False,
                     index: AssetIndex | None = None) -> ImportOutcome:
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
    # 先按 asset_id / imdb / 片名 找库里已有的同一部片（上传那条路的 asset_xxx
    # 也要能认出来，否则会多出一行同名片）
    index = index or AssetIndex(session)
    match = index.match(asset_id, imdb_of_dir(dir_name), content.asset.title)
    if match is not None:
        if match.has_content and not overwrite:
            if _same_content(session.get(MediaAsset, match.asset_id).content_json,
                             data, match.asset_id):
                return ImportOutcome(
                    dir_name=dir_name, status="skipped", asset_id=match.asset_id,
                    title=match.title, scenes=len(content.content.scenes),
                    frames=frames, reason="内容与库内一致，无需重复导入")
            how = "按片名匹配" if match.matched_by == "title" else "已存在"
            return ImportOutcome(
                dir_name=dir_name, status="skipped", asset_id=match.asset_id,
                title=match.title, scenes=len(content.content.scenes),
                frames=frames,
                reason=(f"{how}到资产 {match.asset_id}（已有内容），"
                        "跳过；需要覆盖请勾选「覆盖已导入资产的内容」"))
        if match.has_video and not overwrite:
            return ImportOutcome(
                dir_name=dir_name, status="skipped", asset_id=match.asset_id,
                title=match.title, frames=frames,
                reason="该资产已绑定上传的视频，跳过（需要覆盖请显式勾选）")
        # 内容写进已有那一行：视频地址由 _upsert_movie 保留，不会丢
        content.asset.asset_id = match.asset_id
        asset_id = match.asset_id

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
    """批量导入；dir_names 为空表示全部（仍未处理的目录）。

    名字对不上时**必须如实报失败**：早前静默过滤掉，前端拿到
    imported/skipped/failed 全 0 却显示"导入完成"，用户以为导进去了。
    """
    items = scan_movie_dirs(root)
    available = {item.dir_name: item for item in items if item.error is None}
    known = set(available)
    outcome = BulkOutcome()

    requested = list(dir_names) if dir_names else list(available)
    for name in requested:
        if name not in known:
            item = next((i for i in items if i.dir_name == name), None)
            if item is not None and item.error:
                reason = item.error
            else:
                reason = (f"这个目录下没有名为 {name} 的影片目录"
                          "（可能选的是别的层或名字已变），未执行导入")
            outcome.failed.append(ImportOutcome(
                dir_name=name, status="failed", reason=reason,
                frames=item.frames if item is not None else 0))
            continue
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
