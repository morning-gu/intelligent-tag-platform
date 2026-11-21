"""闭环评测数据集层：发现（scan）、gt/影片内容加载与选片（纯文件系统读）。

为什么需要这个模块
==================

闭环评测以 `/cache/MovieTags` 基准（gt CSV 三列 `movieId,tagId,relevance`
+ 同根 `data/<ttID>/content.json`）为真值。数据集层回答三件事：

1. **有哪些数据集可评**：扫 `gt/*.csv`，每个文件 = 一个数据集；合法性
   = 首行三列 `movieId,tagId,relevance` 且数据行可解析。畸形文件**跳过
   该文件**而不是让整个扫描崩掉（宁缺勿崩）——三份 gt 有一份手滑改坏
   列名，其余两份照常可评，坏的那份从列表里消失即可见；
2. **每部片怎么读**：`load_gt` 取 {movieId: {tagId: relevance}} 真值表；
   `load_movie_content` 经 `content_io.load_content` 走上游形态自动识别
   转换——远端 content.json 的台词字段是 `speak_id`（历史上游笔误），
   平台契约是 `speaker_id`，读入口统一改名，否则该字段被上游模型的
   extra=ignore 静默吞掉、说话人退化成「未知说话人」；
3. **评哪些片**：`select_movies` 三种模式（preset30 固定 seed 抽样 /
   all / custom 越权显式拒绝）。

缓存纪律（closed_eval 三分，原 docstring 段落随使用方迁入本层）
==============================================================

- gt 表按 (path, mtime) 进程内缓存：genome-scores 14.7 万行首读秒级，
  复扫/复评不重读；文件被替换（mtime 变化）自动失效；
- 片名（content.json 的 asset.title）按路径进程内缓存：评测期间数据集
  根只读，同名路径不会变。

分层红线：本层是 service / report 之下的底座——纯文件系统读 + 选片，
不 import fastapi（设计文档 §9.4），零 DB 交互；数据集根只读。
"""
from __future__ import annotations

import csv
import json
import os
import random
from collections.abc import Collection
from dataclasses import dataclass

from tagplatform.schemas import ContentJSON
from tagplatform.services.content.content_io import load_content

__all__ = [
    "PRESET_SEED",
    "PRESET_SIZE",
    "DatasetInfo",
    "load_gt",
    "load_movie_content",
    "movie_entries",
    "scan_datasets",
    "select_movies",
]

# 试点 30 部：seed 钉死保证换机器重跑抽到同一批片（spec §6）
PRESET_SIZE = 30
PRESET_SEED = 20261120

# gt CSV 的合法首行（spec §5：统一三列 movieId,tagId,relevance）
_GT_HEADER = ["movieId", "tagId", "relevance"]

# gt 表缓存：path → ((mtime, size), {movieId: {tagId: relevance}})。
# 键含 size：Windows mtime 精度为秒级文件时间戳，同秒内追加写入 mtime
# 不变（实测 delta=0.0），仅按 mtime 判失效会复用旧表；size 与内容长度
# 同步变化，同秒写入也能正确失效（Linux 上更稳健）
_GT_CACHE: dict[str, tuple[tuple[float, int], dict[str, dict[str, float]]]] = {}
# 片名缓存：content.json 路径 → asset.title
_TITLE_CACHE: dict[str, str] = {}


# ---------------------------------------------------------------- 数据集发现

@dataclass
class DatasetInfo:
    """扫描出的一份可评数据集：gt 概况 + 与当前标签库的可用性。

    - `n_movies/n_tags`：gt 里的影片数与标签数（去重）；
    - `library_overlap`：gt 标签 ∩ 库标签——换库后哪个 gt 还能用一眼可见；
    - `usable_movies`：gt 有分 ∩ `data/<movieId>/content.json` 存在，
      按 movieId 排序（评测只评内容齐备的片）。
    """

    id: str
    gt_path: str
    n_movies: int
    n_tags: int
    library_overlap: int
    usable_movies: list[str]


def _read_gt_table(path: str) -> dict[str, dict[str, float]] | None:
    """读一份 gt CSV → {movieId: {tagId: relevance}}；畸形 → None（宁缺勿崩）。

    首行不是三列 `movieId,tagId,relevance`、或任何数据行列数/浮点解析
    不对 → 整份文件判无效（调用方跳过）。按 (path, mtime+size) 进程内
    缓存（size 防 Windows 同秒写入 mtime 不变的失效盲区）。
    """
    try:
        st = os.stat(path)
        stamp = (st.st_mtime, st.st_size)
    except OSError:
        return None
    cached = _GT_CACHE.get(path)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    table: dict[str, dict[str, float]] = {}
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f)
            if next(reader, None) != _GT_HEADER:  # 空文件/首行不对 → 无效
                return None
            for row in reader:
                if not row:
                    continue  # 空行不算数据行
                if len(row) != 3:
                    return None
                movie_id, tag_id, raw = row
                table.setdefault(movie_id, {})[tag_id] = float(raw)
    except (OSError, ValueError, csv.Error):
        return None
    _GT_CACHE[path] = (stamp, table)
    return table


def scan_datasets(root: str, library_tag_ids: Collection[str]) -> list[DatasetInfo]:
    """扫 `root/gt/*.csv` → 可评数据集列表（按 id 排序）。

    畸形 gt（首行不对/数据行不可解析）跳过该文件，不影响其余（宁缺勿崩）；
    根目录不存在 → 空列表。gt 表读取走 (path, mtime) 进程内缓存。
    """
    gt_dir = os.path.join(root, "gt")
    try:
        names = sorted(os.listdir(gt_dir))
    except OSError:
        return []
    library = set(library_tag_ids)
    infos: list[DatasetInfo] = []
    for name in names:
        if not name.endswith(".csv"):
            continue
        path = os.path.join(gt_dir, name)
        table = _read_gt_table(path)
        if table is None:
            continue
        tag_ids = {tag for tags in table.values() for tag in tags}
        infos.append(DatasetInfo(
            id=name[:-len(".csv")],
            gt_path=path,
            n_movies=len(table),
            n_tags=len(tag_ids),
            library_overlap=len(tag_ids & library),
            usable_movies=[
                movie_id for movie_id in sorted(table)
                if os.path.isfile(os.path.join(root, "data", movie_id,
                                               "content.json"))
            ],
        ))
    infos.sort(key=lambda info: info.id)
    return infos


# ---------------------------------------------------------------- gt 与影片加载

def _gt_path(root: str, dataset_id: str) -> str:
    return os.path.join(root, "gt", f"{dataset_id}.csv")


def load_gt(root: str, dataset_id: str) -> dict[str, dict[str, float]]:
    """一份 gt → {movieId: {tagId: relevance}}；数据集不存在/畸形 → ValueError。

    返回缓存表的逐层浅拷贝：调用方改返回值不影响缓存。
    """
    path = _gt_path(root, dataset_id)
    table = _read_gt_table(path)
    if table is None:
        raise ValueError(f"gt 数据不存在或无法解析：dataset_id={dataset_id!r}"
                         f"（{path}）")
    return {movie_id: dict(tags) for movie_id, tags in table.items()}


def _rename_speak_id(data: dict) -> None:
    """把台词的 `speak_id` 键改名为平台契约的 `speaker_id`（就地改）。

    /cache/MovieTags 的 content.json 台词字段是 `speak_id`（spec §4 记录的
    已知差异），平台上游 schema 是 `speaker_id`；不改名会被上游模型的
    extra="ignore" 静默吞掉，说话人退化成「未知说话人」。浅遍历
    content.scenes[*].dialogues[*]，键存在才改。
    """
    content = data.get("content")
    if not isinstance(content, dict):
        return
    for scene in content.get("scenes") or []:
        if not isinstance(scene, dict):
            continue
        for dialogue in scene.get("dialogues") or []:
            if isinstance(dialogue, dict) and "speak_id" in dialogue:
                dialogue["speaker_id"] = dialogue.pop("speak_id")


def load_movie_content(root: str, tt: str) -> ContentJSON:
    """读 `data/<tt>/content.json` → 平台内部 ContentJSON。

    读 JSON 后先改 `speak_id` → `speaker_id`，再交 `content_io.load_content`
    （上游形态自动识别转换；平台内部形态的旧数据同样可读）。文件缺失由
    open 抛 FileNotFoundError（调用方按"该片不可评"处理）。
    """
    path = os.path.join(root, "data", tt, "content.json")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    _rename_speak_id(data)
    return load_content(json.dumps(data, ensure_ascii=False))


def _read_title(path: str) -> str:
    """content.json → asset.title（进程内缓存；读不出 → 空串，宁缺勿崩）。"""
    title = _TITLE_CACHE.get(path)
    if title is None:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            title = str((data.get("asset") or {}).get("title") or "")
        except (OSError, ValueError):
            title = ""
        _TITLE_CACHE[path] = title
    return title


def movie_entries(root: str, dataset_id: str) -> list[dict[str, str]]:
    """数据集的 usable 影片 → [{tt, title}]，按 tt 排序（前端选片列表）。

    标题读 content.json 的 asset.title（进程内缓存）；数据集不存在/畸形
    → ValueError（与 load_gt 同口径）。
    """
    info = next((i for i in scan_datasets(root, set()) if i.id == dataset_id),
                None)
    if info is None:
        raise ValueError(f"数据集不存在或无法解析：dataset_id={dataset_id!r}"
                         f"（{_gt_path(root, dataset_id)}）")
    return [{"tt": tt,
             "title": _read_title(os.path.join(root, "data", tt, "content.json"))}
            for tt in info.usable_movies]


# ---------------------------------------------------------------- 选片

def select_movies(usable: list[str], mode: str,
                  custom: list[str] | None = None, *,
                  size: int = PRESET_SIZE,
                  seed: int = PRESET_SEED) -> list[str]:
    """按模式从 usable 选片，返回排序去重列表。

    - `preset30`：`sorted` 后 `random.Random(seed).sample(..., min(size, len))`
      ——不足 size 全取；固定 seed 保证换机器重跑抽到同一批片。sample 的
      输出顺序随输入顺序漂移，末尾再排序让结果与输入顺序无关；
    - `all`：全部（排序去重）；
    - `custom`：只接受 usable 子集，越权 id → ValueError 列出未知项
      （前端只发可见列表，越权 = 调用方 bug，早失败不静默缩圈）。
    """
    pool = sorted(set(usable))
    if mode == "preset30":
        return sorted(random.Random(seed).sample(pool, min(size, len(pool))))
    if mode == "all":
        return pool
    if mode == "custom":
        chosen = set(custom or [])
        unknown = sorted(chosen - set(pool))
        if unknown:
            raise ValueError(f"custom 选片含 usable 之外的影片 id：{unknown}")
        return sorted(chosen)
    raise ValueError(f"未知选片模式：{mode!r}（支持 preset30 / all / custom）")
