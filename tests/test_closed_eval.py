"""closed_eval 数据集层的单测：MovieTags 基准的发现、加载与选片口径钉死。

夹具 `_write_dataset` 在 tmp 内搭一套迷你 MovieTags 数据集（绝不扫 /cache/*）：

1. **畸形 gt 不崩扫描**（Review Focus 1）——bad.csv 首行两列 → 扫描直接
   跳过该文件，合法的 emotion.csv 照常出现（宁缺勿崩）；
2. **usable = gt 有分 ∩ content.json 存在**——tt0000003 只有 gt 分没有
   content.json，不进可用列表；
3. **speak_id 改名**——远端 content.json 的台词字段是 `speak_id`（平台
   上游契约是 `speaker_id`），加载时统一改名，说话人不能退化成「未知」；
4. **选片确定性**——preset30 同 seed 同结果（输入顺序不同也算相等）、
   不足 30 全取、custom 越权 id 显式报错。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tagplatform.closed_eval import (
    PRESET_SEED,
    PRESET_SIZE,
    load_gt,
    load_movie_content,
    movie_entries,
    scan_datasets,
    select_movies,
)


# ---------------------------------------------------------------- 夹具

_UPSTREAM_TEMPLATE = """{{
  "schema_version": "1.0",
  "asset": {{"asset_id": "{tt}", "asset_type": "movie", "title": "{title}"}},
  "content": {{
    "description": "一部测试影片的整体剧情描述。",
    "characters": [{{"character_id": "character_001", "name": "主角"}}],
    "scenes": [
      {{
        "scene_id": "scene_001", "start_ms": 0, "end_ms": 60000,
        "description": "开场对话。",
        "dialogues": [
          {{"dialogue_id": "d1", "speak_id": "character_001", "text": "你好。",
             "start_ms": 1000, "end_ms": 2000}}
        ]
      }}
    ]
  }}
}}
"""


def _write_dataset(root: Path) -> None:
    """内置夹具：迷你 MovieTags 数据集（gt 两份 + 两部片的内容 JSON）。

    - gt/emotion.csv：三列首行 `movieId,tagId,relevance`，3 影片 × 4 标签
      （t1/t2/t7/t9，与测试库 {t1, t9} 交集恰 2）；
    - gt/bad.csv：首行两列——畸形文件，扫描必须跳过而不是崩；
    - data/tt0000001、tt0000002/content.json：上游形态、台词用 `speak_id`；
      tt0000003 只有 gt 分、没有 content.json（→ 不进 usable）。
    """
    gt = root / "gt"
    gt.mkdir(parents=True)
    rows = ["movieId,tagId,relevance"]
    for tt in ("tt0000001", "tt0000002", "tt0000003"):
        for tag, rel in (("t1", "0.9"), ("t2", "0.4"), ("t7", "0.1"),
                         ("t9", "0.8")):
            rows.append(f"{tt},{tag},{rel}")
    (gt / "emotion.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    (gt / "bad.csv").write_text("movieId,tagId\ntt0000001,t1\n",
                                encoding="utf-8")
    data = root / "data"
    for tt, title in (("tt0000001", "A Movie"), ("tt0000002", "B Movie")):
        movie_dir = data / tt
        movie_dir.mkdir(parents=True)
        (movie_dir / "content.json").write_text(
            _UPSTREAM_TEMPLATE.format(tt=tt, title=title), encoding="utf-8")


@pytest.fixture
def root(tmp_path):
    """临时数据集根（str）：每测试独立一套迷你 MovieTags。"""
    _write_dataset(tmp_path)
    return str(tmp_path)


# ---------------------------------------------------------------- 扫描

def test_scan_datasets_skips_invalid(root):  # Review Focus 1
    infos = scan_datasets(root, library_tag_ids={"t1", "t9"})
    assert [i.id for i in infos] == ["emotion"]
    assert (infos[0].n_movies, infos[0].n_tags, infos[0].library_overlap) == (3, 4, 2)


def test_scan_datasets_usable_excludes_missing_content(root):
    assert scan_datasets(root, set())[0].usable_movies == ["tt0000001", "tt0000002"]


# ---------------------------------------------------------------- gt 加载

def test_load_gt_unknown_dataset_raises(root):
    with pytest.raises(ValueError):
        load_gt(root, "nope")


def test_load_gt_shape(root):
    # 返回形状 {movieId: {tagId: relevance}}，relevance 是 float
    gt = load_gt(root, "emotion")
    assert set(gt) == {"tt0000001", "tt0000002", "tt0000003"}
    assert gt["tt0000001"] == {"t1": 0.9, "t2": 0.4, "t7": 0.1, "t9": 0.8}
    assert all(isinstance(v, float) for tags in gt.values() for v in tags.values())


def test_gt_cache_rereads_after_mtime_change(root):
    # 进程内缓存按 (path, mtime) 失效：文件变化后复扫必须看到新行数
    assert scan_datasets(root, set())[0].n_movies == 3
    with (Path(root) / "gt" / "emotion.csv").open("a", encoding="utf-8") as f:
        f.write("tt0000009,t1,0.5\n")
    assert scan_datasets(root, set())[0].n_movies == 4


# ---------------------------------------------------------------- 影片加载

def test_load_movie_content_renames_speak_id(root):
    content = load_movie_content(root, "tt0000001")
    assert content.content.scenes[0].dialogues[0].speaker == "character_001"


def test_movie_entries_titles(root):
    entries = movie_entries(root, "emotion")
    assert entries == [{"tt": "tt0000001", "title": "A Movie"},
                       {"tt": "tt0000002", "title": "B Movie"}]


# ---------------------------------------------------------------- 选片

def test_select_movies_preset_deterministic(root):
    a = select_movies(["x1", "x2", "x3"], "preset30")
    b = select_movies(["x3", "x1", "x2"], "preset30")
    assert a == b and len(a) == 3          # 不足 30 → 全取
    assert select_movies(["x1", "x2"], "all") == ["x1", "x2"]
    with pytest.raises(ValueError, match="tt9999"):
        select_movies(["x1"], "custom", ["x1", "tt9999"])


def test_select_movies_sorted_dedup_and_defaults():
    # 契约：返回排序去重列表；常量钉死（试点 30 部 seed=20261120）
    assert select_movies(["b", "a", "b"], "all") == ["a", "b"]
    assert select_movies(["b", "a", "b"], "custom", ["b", "a"]) == ["a", "b"]
    assert PRESET_SIZE == 30 and PRESET_SEED == 20261120
