"""TagProducer 协议、生成契约 dataclass 与注册表测试（Task 3）。

覆盖契约：
- 注册表：Task 5/6 起 closed/open 生成器随包导入自动注册 → 初始态
  list_producers() == ["closed", "open"]；重复注册同 mode
  抛 ValueError；
  未知 mode 抛 KeyError 且信息列出可用 mode（同 adapters 注册表测试
  模式）；
- dataclass：GenerationPolicy 默认值（max_tags=30, min_confidence=0.7）、
  GenerationSpec/Evidence/ProducedTag 逐字段构造。
"""

import json
from pathlib import Path

import pytest

import tagplatform.generation.base as generation_base
from tagplatform.generation import (
    Evidence,
    GenerationPolicy,
    GenerationSpec,
    ProducedTag,
    TagProducer,
    get_producer,
    list_producers,
    register_producer,
)
from tagplatform.schemas import ContentJSON

FIXTURE = Path("tests/fixtures/content_movie.json")


class _DummyProducer:
    """测试用最小生成器：满足 TagProducer 协议结构即可注册。"""

    mode = "dummy"

    def produce(self, spec, session, llm, embedder):
        return []


@pytest.fixture()
def restore_registry():
    """快照模块级注册表并在测试后还原，避免测试污染全局状态。"""
    saved = dict(generation_base._REGISTRY)
    yield
    generation_base._REGISTRY.clear()
    generation_base._REGISTRY.update(saved)


def _make_content() -> ContentJSON:
    return ContentJSON.model_validate(
        json.loads(FIXTURE.read_text(encoding="utf-8"))
    )


# ---- 注册表三函数 ----


def test_registry_empty_initial_state():
    # Task 5/6 起 closed/open 生成器随包导入自动注册
    assert list_producers() == ["closed", "open"]


def test_register_and_get_producer(restore_registry):
    p = _DummyProducer()
    register_producer(p)
    assert get_producer("dummy") is p
    # closed/open 随包导入已注册（Task 5/6），dummy 为本测试追加（排序返回）
    assert list_producers() == ["closed", "dummy", "open"]


def test_register_duplicate_mode_raises_valueerror(restore_registry):
    register_producer(_DummyProducer())
    with pytest.raises(ValueError):
        register_producer(_DummyProducer())


def test_get_unknown_mode_raises_keyerror(restore_registry):
    with pytest.raises(KeyError):
        get_producer("nope")


def test_unknown_mode_message_lists_available(restore_registry):
    register_producer(_DummyProducer())
    with pytest.raises(KeyError) as exc_info:
        get_producer("nope")
    # 契约：KeyError 信息列出可用 mode
    assert "dummy" in str(exc_info.value)


# ---- 协议形态 ----


def test_protocol_shape():
    # 协议约定：produce(spec, session, llm, embedder) 方法 + mode 注解属性
    assert callable(getattr(TagProducer, "produce", None))
    assert "mode" in TagProducer.__annotations__


# ---- dataclass 构造与默认值 ----


def test_generation_policy_defaults():
    policy = GenerationPolicy()
    assert policy.max_tags == 30
    assert policy.min_confidence == 0.7


def test_generation_policy_explicit():
    policy = GenerationPolicy(max_tags=10, min_confidence=0.5)
    assert policy.max_tags == 10
    assert policy.min_confidence == 0.5


def test_evidence_construction():
    ev = Evidence(
        scene_id="sc-01", start_ms=0, end_ms=3000, description="开场对话"
    )
    assert ev.scene_id == "sc-01"
    assert ev.start_ms == 0
    assert ev.end_ms == 3000
    assert ev.description == "开场对话"


def test_generation_spec_construction():
    spec = GenerationSpec(
        task_id="t-1",
        asset_id="a-1",
        content=_make_content(),
        policy=GenerationPolicy(),
        prompt_text="PROMPT",
        run_id="r-1",
    )
    assert spec.task_id == "t-1"
    assert spec.asset_id == "a-1"
    assert spec.content.schema_version == "1.0"
    assert spec.policy.max_tags == 30
    assert spec.prompt_text == "PROMPT"
    assert spec.run_id == "r-1"


def test_produced_tag_catalog_origin():
    tag = ProducedTag(
        origin="catalog",
        tag_id="tag-1",
        tag_name="悬疑",
        candidate_type=None,
        candidate_description=None,
        relevance_score=0.9,
        confidence=0.95,
        evidence=[
            Evidence(
                scene_id="sc-01", start_ms=0, end_ms=3000, description="反转"
            )
        ],
    )
    assert tag.origin == "catalog"
    assert tag.tag_id == "tag-1"
    assert tag.candidate_type is None
    assert len(tag.evidence) == 1
    assert tag.evidence[0].scene_id == "sc-01"


def test_produced_tag_generated_origin():
    tag = ProducedTag(
        origin="generated",
        tag_id=None,
        tag_name="非线性叙事",
        candidate_type="theme",
        candidate_description="非常规时间线结构",
        relevance_score=None,
        confidence=0.8,
        evidence=[],
    )
    assert tag.origin == "generated"
    assert tag.tag_id is None
    assert tag.candidate_type == "theme"
    assert tag.evidence == []
