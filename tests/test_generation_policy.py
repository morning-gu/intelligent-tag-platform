"""生成策略（目录范围 / 新词开关 / 单目录上限）在两条链路上的行为。

覆盖契约：
- dir_ids 限定召回范围：范围外目录的标签不参与对齐，也不产生新词；
- allow_new_tags=False：只产出命中库内的行，新词一律丢弃（审计 dropped
  写明原因）；
- max_tags_per_dir：同一目录超限的候选丢弃；
- closed 模式遵守 dir_ids：候选只来自范围内目录。
"""

import pytest
from conftest import MOCK_EMBEDDER, MOCK_LLM

from tagplatform.services.generation import GenerationPolicy, GenerationSpec
from tagplatform.services.generation.closed.producer import ClosedTagProducer
from tagplatform.services.generation.open.producer import OpenTagProducer
from tagplatform.services.tags.library import try_load_library
from tests.library_fixture import add_tags


def _spec(content, run_id, policy, library=None):
    return GenerationSpec(
        task_id=f"t-{run_id}", asset_id=content.asset.asset_id,
        content=content, policy=policy, prompt_text="", run_id=run_id,
        library=library or try_load_library())


def test_open_dir_scope_limits_new_tag_dirs(content_fixture):
    """dir_ids 只留 dir_theme：新词全部归到该目录、挂 content。"""
    spec = _spec(content_fixture, "p1",
                 GenerationPolicy(min_confidence=0.5, dir_ids=["dir_theme"]))
    out = OpenTagProducer().produce(spec, None, MOCK_LLM, MOCK_EMBEDDER)
    assert out
    assert {t.dir_id for t in out} == {"dir_theme"}
    assert {t.target_type for t in out} == {"content"}


def test_open_dir_scope_character_without_cast_drops(content_fixture):
    """范围只剩角色目录、素材里没有角色 → 挂载点解析失败，全丢（不挂错）。"""
    spec = _spec(content_fixture, "p1b",
                 GenerationPolicy(min_confidence=0.5, dir_ids=["dir_char"]))
    assert OpenTagProducer().produce(spec, None, MOCK_LLM, MOCK_EMBEDDER) == []


def test_open_allow_new_tags_false_drops_new_words(content_fixture):
    spec = _spec(content_fixture, "p2",
                 GenerationPolicy(min_confidence=0.5, allow_new_tags=False))
    out = OpenTagProducer().produce(spec, None, MOCK_LLM, MOCK_EMBEDDER)
    assert all(t.origin == "catalog" for t in out)
    assert not out  # 默认库没有同名标签 → 全部按新词丢弃


def _with_two_scenes(content):
    original = content.content.scenes[0]
    second = original.model_copy(update={"scene_id": "scene_002"})
    return content.model_copy(update={"content": content.content.model_copy(
        update={"scenes": [original, second]})})


def test_open_max_tags_per_dir_caps_output(content_fixture):
    add_tags([{"tag_id": "tag_extra", "dir_id": "dir_theme",
               "tag_name": "多线叙事", "tag_type": "val_fixed",
               "description": "多线叙事", "status": "active"}])
    spec = _spec(_with_two_scenes(content_fixture), "p3",
                 GenerationPolicy(max_tags_per_dir=1, dir_ids=["dir_theme"]))
    out = OpenTagProducer().produce(spec, None, MOCK_LLM, MOCK_EMBEDDER)
    assert [t.dir_id for t in out if t.origin == "generated"] == ["dir_theme"]
    assert [t.tag_id for t in out if t.origin == "catalog"] == ["tag_extra"]


def test_closed_respects_dir_scope(session, content_fixture):
    """closed 候选只从 dir_ids 范围内召回：范围内没有标签 → 显式失败。"""
    from tests.library_fixture import add_dirs

    add_dirs([{"dir_id": "dir_empty", "parent_id": None, "level": 1,
               "dir_name": "空目录", "dir_type": "dir_dynamic",
               "applicable_scope": "content", "description": "", "aliases": []}])
    spec = _spec(content_fixture, "p5", GenerationPolicy(dir_ids=["dir_empty"]))
    with pytest.raises(ValueError, match="候选标签为空"):
        ClosedTagProducer().produce(spec, session, MOCK_LLM, MOCK_EMBEDDER)
