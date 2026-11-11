"""目录归属评测脚本的单测：防泄漏口径 + 基线可比性。

`scripts/eval_dir_assignment.py` 的核心不是命令行，而是两件容易写错的事：

1. **防泄漏**——目录文本里带"已收录：标签名"清单，若被评测的那一枚还
   留在清单里，归属是白送的，分数会虚高。留一法必须用"去掉自己"的文本；
2. **编码次数**——最初的实现每枚标签调一次模型编码（514 枚 = 514 次前
   向），在 CPU 上要跑几小时，看起来像卡死。现在压成两批：查询一批、
   目录基座 + 全部"去自己"变体一批。这个约束用假的计数嵌入器钉死，
   退化回逐标签编码会立刻失败。

同时钉住基线可比性的前提：同一份库 + 同一个（确定性）嵌入器，两次评测必
须给出**完全相同**的数字——否则它不能当回归基线。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pytest
from conftest import MOCK_EMBEDDER

from tagplatform.library import _DIR_SAMPLE_TAGS, try_load_library

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "eval_dir_assignment.py"


def _load_script():
    """按路径导入脚本（它在 scripts/ 下，不是包）。"""
    spec = importlib.util.spec_from_file_location("eval_dir_assignment", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def script():
    return _load_script()


class _CountingEmbedder:
    """包一层真嵌入器，只记录"被调用了几次、每次多少条文本"。"""

    def __init__(self, inner):
        self._inner = inner
        self.calls: list[int] = []

    def embed(self, texts, *, role):
        self.calls.append(len(texts))
        return self._inner.embed(texts, role=role)


# ---------------------------------------------------------------- 防泄漏

def test_variant_text_drops_the_held_out_tag(script):
    """被评测标签若在前 12 枚内，变体文本必须不含它的名字。"""
    library = try_load_library()
    dirs = library.new_tag_dirs(None)
    base_texts, variants = script._dir_texts_and_variants(library, dirs)

    home = library.directory("dir_theme")
    base = script.dir_text(
        home,
        [t.tag_name for t in library.tags()
         if t.dir_id == "dir_theme" and not script.is_deprecated(t.status)][:_DIR_SAMPLE_TAGS])
    assert "复仇" in base, "前提：基座文本本就含被评测标签"

    variant = variants[("dir_theme", "tag_revenge")]
    assert "已收录：" in variant
    listed = variant.split("已收录：", 1)[1].split("、")
    assert "复仇" not in listed, "留一法泄漏：变体文本仍含被评测标签"
    assert "非线性叙事" in listed, "变体应保留同目录的其余标签"


def test_variant_only_spans_first_sample_tags(script):
    """只有落在前 `_DIR_SAMPLE_TAGS` 枚内的标签才有变体，集合大小可预期。"""
    library = try_load_library()
    dirs = library.new_tag_dirs(None)
    _base, variants = script._dir_texts_and_variants(library, dirs)

    for directory in dirs:
        hits = [k for k in variants if k[0] == directory.dir_id]
        in_dir = [t for t in library.tags() if t.dir_id == directory.dir_id]
        assert len(hits) == min(len(in_dir), _DIR_SAMPLE_TAGS)


def test_variant_equals_base_when_tag_is_the_only_one(script):
    """目录里只剩这一枚标签时，"去掉自己"就是空清单（不能残留分隔符）。"""
    library = try_load_library()
    dirs = [d for d in library.new_tag_dirs(None) if d.dir_id == "dir_theme"]
    base_texts, variants = script._dir_texts_and_variants(library, dirs)
    variant = variants[("dir_theme", "tag_revenge")]
    assert variant != base_texts[0]
    assert variant.count("已收录：") == 1


# ---------------------------------------------------------------- 编码次数

def test_encoding_is_batched_not_per_tag(script):
    """反向断言：编码调用次数与标签数无关（只与目录数/批大小有关）。"""
    library = try_load_library()
    counting = _CountingEmbedder(MOCK_EMBEDDER)

    samples, _warnings = script.library_samples(
        library, counting, None, None, strict=False)

    n_tags = len(samples)
    n_dirs = len(library.new_tag_dirs(None))
    # 两批：① 查询（每批 64 条）② 基座 + 变体（每批 64 条）
    expected_upper = 2 * (1 + (n_dirs + n_tags) // 64 + 2)
    assert n_tags > 3, "默认库要有足够标签才有意义"
    assert len(counting.calls) <= expected_upper, (
        f"编码调用了 {len(counting.calls)} 次，疑似退化为逐标签编码")
    # 逐标签的旧写法必定 >= 标签数次调用
    assert len(counting.calls) < n_tags


def test_query_batch_is_encoded_once(script):
    """全部标签名应一次（或按批）编码完，而不是每枚一趟。"""
    library = try_load_library()
    counting = _CountingEmbedder(MOCK_EMBEDDER)
    samples, _ = script.library_samples(library, counting, None, None, strict=False)

    assert counting.calls[0] == len(samples), "第一批应当是全部查询标签名"


# ---------------------------------------------------------------- 可比性

def test_baseline_is_deterministic(script):
    """同一份库 + 确定性嵌入器 → 完全相同的分数（回归基线的前提）。"""
    library = try_load_library()
    first, _ = script.library_samples(library, MOCK_EMBEDDER, None, None, strict=True)
    second, _ = script.library_samples(library, MOCK_EMBEDDER, None, None, strict=True)

    assert [s.ranked for s in first] == [s.ranked for s in second]
    assert [s.relevance for s in first] == [s.relevance for s in second]


def test_samples_shape_is_usable(script):
    """每条样本都要有非空候选与相关度表，否则指标会静默算成 0。"""
    library = try_load_library()
    samples, _ = script.library_samples(library, MOCK_EMBEDDER, None, None,
                                        strict=False)
    for sample in samples:
        assert sample.ranked, "候选列表为空，指标必然全 0"
        assert sample.relevance, "相关度表为空，样本应被归进不可归属率"
        assert max(sample.relevance.values()) == 2.0, "真值目录必须是精确档"
        assert len(sample.ranked) <= script.TOP_N


def test_relevance_is_graded_in_loose_mode(script):
    """宽松口径下同父兄弟目录得半分，严格口径下归零。"""
    from tests.library_fixture import add_dirs

    # 候选集只含可扩展目录（dir_dynamic），所以兄弟目录也必须是动态的：
    # dir_mood 与 dir_theme 同挂 dir_content
    add_dirs([{
        "dir_id": "dir_mood", "parent_id": "dir_content", "level": 2,
        "dir_name": "情绪基调", "dir_type": "dir_dynamic",
        "applicable_scope": "content", "description": "整体情绪与氛围",
        "aliases": [],
    }])
    library = try_load_library()
    candidates = [d.dir_id for d in library.new_tag_dirs(None)]
    assert {"dir_theme", "dir_mood"} <= set(candidates), "前提：两者都在候选集里"

    loose = script._relevance(library, "dir_theme", candidates, strict=False)
    strict = script._relevance(library, "dir_theme", candidates, strict=True)

    assert loose["dir_theme"] == 2.0, "真值目录必须是精确档"
    assert loose["dir_mood"] == 1.0, "同父兄弟目录在宽松口径算相关"
    assert strict["dir_mood"] == 0.0, "严格口径只认精确目录"