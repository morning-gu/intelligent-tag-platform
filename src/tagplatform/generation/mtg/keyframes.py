"""场景 → MovieNet 关键帧：开放式生成的多模态输入装配。

输入是任务一产出的资产 JSON（`ContentJSON`，场景带 scene_id / 时间区间 /
中文描述），数据集侧提供帧文件。本模块只做两件事：

1. 把每个场景对回 MovieNet `annotation.story[i]`：优先用 scene_id 的
   `_story_{index}` 后缀定位（资产生成时就是按这个规则编号的），对不上
   再按时间区间就近匹配（容忍 5s，`renumber_ids` 过的资产走这条路）；
2. 在 story 段的 `shot` 区间内取帧（复用 `assets.builder.pick_keyframes`），
   全片超出上限时按"先每个场景一张、再回填第二张"的方式轮转截断，
   保证场景覆盖优先于单场景厚度。

取不到帧不是错误：上游数据可能没有帧，生成侧自动退化为纯文本 prompt。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from tagplatform.assets.builder import pick_keyframes
from tagplatform.movie_import import resolve_scene_frames as imported_scene_frames
from tagplatform.schemas import ContentJSON

_STORY_SUFFIX_RE = re.compile(r"_story_(\d+)$")
_MOVIE_ID_RE = re.compile(r"^(tt\d+)_story_")
# scene_id 后缀对不上时按时间就近匹配的容差（毫秒）
_TIME_MATCH_TOLERANCE_MS = 5000


@dataclass(frozen=True)
class SceneFrames:
    """单场景的帧装配结果（frames 可为空：该场景无可用帧）。"""

    scene_id: str
    start_ms: int
    end_ms: int
    frames: tuple[Path, ...]


@lru_cache(maxsize=32)
def _load_annotation(dataset_root: str, movie_id: str) -> dict | None:
    """读 MovieNet 标注（带缓存）；文件缺失/损坏返回 None（上层退化纯文本）。"""
    path = Path(dataset_root) / "annotation_data" / "annotation" / f"{movie_id}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def movie_id_of(content: ContentJSON) -> str | None:
    """资产 → MovieNet movie_id（imdb id）。

    优先 asset_id 的 `movie_` 前缀（本平台资产主键约定），否则从场景 id
    的 `ttXXXX_story_` 前缀反推；两者都没有则返回 None（纯文本生成）。
    """
    asset_id = content.asset.asset_id or ""
    if asset_id.startswith("movie_"):
        return asset_id[len("movie_"):]
    for scene in content.content.scenes:
        match = _MOVIE_ID_RE.match(scene.scene_id)
        if match:
            return match.group(1)
    return None


def _story_index(scene_id: str, start_ms: int,
                 story: list[dict]) -> int | None:
    """场景 → story 下标：后缀优先，其次按时间区间就近匹配。"""
    match = _STORY_SUFFIX_RE.search(scene_id)
    if match:
        index = int(match.group(1))
        if 0 <= index < len(story):
            return index
    best_index, best_delta = None, None
    for index, seg in enumerate(story):
        duration = seg.get("duration") or [0, 0]
        try:
            seg_start = int(round(float(duration[0]) * 1000))
        except (TypeError, ValueError, IndexError):
            continue
        delta = abs(seg_start - start_ms)
        if best_delta is None or delta < best_delta:
            best_index, best_delta = index, delta
    if best_delta is not None and best_delta <= _TIME_MATCH_TOLERANCE_MS:
        return best_index
    return None


def _cap_frames(plans: list[SceneFrames], max_frames: int) -> list[SceneFrames]:
    """帧总数上限：先给每个场景第 1 张，再回填第 2 张，直到用满预算。"""
    if max_frames <= 0:
        return [SceneFrames(p.scene_id, p.start_ms, p.end_ms, ()) for p in plans]
    picked: dict[str, list[Path]] = {p.scene_id: [] for p in plans}
    total = 0
    round_index = 0
    while total < max_frames:
        added = False
        for plan in plans:
            if total >= max_frames:
                break
            if len(picked[plan.scene_id]) >= len(plan.frames):
                continue
            picked[plan.scene_id].append(plan.frames[len(picked[plan.scene_id])])
            total += 1
            added = True
        round_index += 1
        if not added or round_index > 8:
            break
    return [SceneFrames(p.scene_id, p.start_ms, p.end_ms,
                        tuple(picked[p.scene_id])) for p in plans]


def plan_scene_frames(content: ContentJSON, dataset_root: str,
                      per_scene: int = 2, max_frames: int = 24,
                      movie_asset_root: str | None = None,
                      extra_roots: str | None = None
                      ) -> dict[str, tuple[Path, ...]]:
    """资产场景 → 每场景若干帧；返回 {scene_id: (Path, ...)}（顺序同资产）。

    帧来源两处，**目录导入的资产优先**：
    ① 目录导入时那套已挑好的帧：`movie_import.resolve_scene_frames` 会自动
       拼接路径——先按目录名/imdb 后缀找，再按 content.json 里的 asset_id
       反查（覆盖"从上一层目录导入""目录名改过"这两种情况）；根目录是
       `settings.movie_asset_root` + 它的父目录 + `extra_roots`（逗号分隔）；
    ② MovieNet 数据集按 story 段现取（上传视频那条路一直走的就是它）。
    两处都没有就退化为纯文本 prompt。
    """
    movie_id = movie_id_of(content)
    if not movie_id:
        return {}
    roots = _local_roots(movie_asset_root, extra_roots)
    annotation = _load_annotation(dataset_root, movie_id)
    story = (annotation or {}).get("story") or []
    frames_dir = Path(dataset_root) / "frames" / "frames" / movie_id
    asset_id = content.asset.asset_id

    plans: list[SceneFrames] = []
    for scene in content.content.scenes:
        frames: tuple[Path, ...] = ()
        if roots:
            local = imported_scene_frames(
                roots, scene.scene_id, asset_id=asset_id, imdb_id=movie_id)
            if local:
                frames = local[:per_scene]
        if not frames and story:
            index = _story_index(scene.scene_id, scene.time_range.start_ms, story)
            if index is not None:
                seg = story[index]
                frames = tuple(pick_keyframes(frames_dir, seg.get("shot") or [],
                                              per_scene))
        plans.append(SceneFrames(scene.scene_id, scene.time_range.start_ms,
                                 scene.time_range.end_ms, frames))
    planned = {p.scene_id: p.frames for p in _cap_frames(plans, max_frames)}
    # 一帧都没取到就按"没有帧"处理（与旧行为一致：退化纯文本，不返回空壳）
    return planned if any(planned.values()) else {}


def _local_roots(movie_asset_root: str | None,
                 extra_roots: str | None = None) -> list[Path]:
    """目录导入的帧根候选：配置根 + 它的父目录 + 额外配置的根。

    父目录一定要带上：用户可以先浏览到 `/cache/…/movie_asset`（上一层）再
    点进 `movie_asset` 选片，这类导入的目录不在配置根的正下方。
    不存在的目录直接丢掉，绝不让它拖挂整条生成。
    """
    candidates: list[str] = []
    if movie_asset_root:
        try:
            configured = Path(movie_asset_root)
        except OSError:
            configured = None
        # 只有配的根真实存在时才连父目录一起找：配错的路径不该顺带扫到
        # 它旁边不相关的目录
        if configured is not None and configured.is_dir():
            candidates.append(str(configured))
            parent = configured.parent
            if str(parent) != str(configured):
                candidates.append(str(parent))
    for chunk in (extra_roots or "").split(","):
        text = chunk.strip()
        if text:
            candidates.append(text)
    roots: list[Path] = []
    for candidate in candidates:
        try:
            path = Path(candidate)
            if path.is_dir() and path not in roots:
                roots.append(path)
        except OSError:
            continue
    return roots
