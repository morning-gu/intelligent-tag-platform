"""模式 B 开放式标签生成测试：归一匹配、OpenTagProducer 与 tag_id 防伪造（Task 6）。

覆盖契约：
- OpenTagProducer.produce 全流程：LLM → parse_open_output → 一次批量嵌入 →
  与 tag_catalog 有效行（排除 deprecate/无向量）逐个 search_similar 取最高分 →
  阈值（>= open_match_threshold，含等于）分流 catalog/generated →
  min_confidence 过滤 + confidence 降序截断 max_tags；
- 匹配分支：origin=catalog + tag_id + relevance_score=相似度、candidate_* 为 None；
- 未匹配分支（含空库）：origin=generated、tag_id=None、relevance_score=None、
  candidate_* 填充——open 模式空目录不报错（与模式 A 空候选显式失败相反）；
- LLM 传输失败 → RuntimeError；
- Review Focus 4：LLM 伪造 tag_id 不生效（解析层静默丢弃 + 匹配层重新赋值）。

Mock 技巧（与 mock.py / MockEmbedder 咬合）：
- Mock 模式 B 固定三候选（_OPEN_SEEDS 照抄 llm/mock.py，变更即测试报警）——
  把首候选原文播种进 tag_catalog（embed 文本 f"{name}。{desc}"），该候选的
  查询文本与库内向量 canonical 形逐字符相同（_canonical 丢标点不补空格）→
  余弦构造恰为 1.0，匹配与阈值边界均为构造保证而非哈希运气；
- confidence 哈希键 confidence|{run_id}|{name}（[0.5,1.0)）——run_id 选取：
  r1 三候选全部 >= 0.7（0.7417/0.8816/0.8635）；r4 仅情感张力（0.8929）
  >= 0.7；r7 三候选降序为 多线叙事(0.9467) > 情感张力(0.932) > 视听风格(0.8179)。
"""

import json

import pytest
from conftest import MOCK_EMBEDDER, MOCK_LLM
from sqlalchemy.orm import Session

from tagplatform.db.models import TagCatalog
from tagplatform.generation import GenerationPolicy, GenerationSpec
from tagplatform.generation import open_gen
from tagplatform.generation.open_gen import OpenTagProducer
from tagplatform.llm.client import LLMResponse
from tagplatform.llm.parsing import parse_open_output
from tagplatform.prompts import get_active_prompt, render_prompt
from tagplatform.settings import Settings
from tagplatform.vectors import encode_vector

# Mock 模式 B 固定三候选（名称 / 分类 / 一句话定义，照抄 llm/mock.py）
_OPEN_SEEDS = [
    ("多线叙事", "narrative", "叙事在多条线索或时间线之间交替推进。"),
    ("情感张力", "mood", "情绪在铺垫与释放之间保持持续的紧张感。"),
    ("视听风格", "style", "画面构图与声音设计共同呈现鲜明的风格化表达。"),
]
_SEED_NAMES = {s[0] for s in _OPEN_SEEDS}


def _seed_open_catalog(session: Session) -> None:
    """播种一枚与 Mock 模式 B 首候选同文本的目录标签（余弦构造恰为 1.0）。"""
    name, tag_type, desc = _OPEN_SEEDS[0]
    vec = MOCK_EMBEDDER.embed([f"{name}。{desc}"])[0]
    session.add(TagCatalog(
        tag_id="tag_open_1", tag_name=name, tag_type=tag_type,
        description=desc, status="active", embedding=encode_vector(vec),
    ))
    session.commit()


def _build_spec(session: Session, content_fixture, run_id: str,
                policy=None) -> GenerationSpec:
    """按任务 8 runner 的职责模拟组装：渲染 open prompt（content 注入）。"""
    prompt_text = render_prompt(
        get_active_prompt(session, "open"),
        content=content_fixture.model_dump_json(),
    )
    return GenerationSpec(
        task_id="t1", asset_id=content_fixture.asset.asset_id,
        content=content_fixture, policy=policy or GenerationPolicy(),
        prompt_text=prompt_text, run_id=run_id,
    )


# ---- OpenTagProducer：匹配 / 未匹配 / 空库 ----


def test_open_produce_end_to_end(session, content_fixture):
    """端到端：首候选与库内同文标签余弦=1.0 → catalog，其余两候选 → generated。"""
    _seed_open_catalog(session)
    spec = _build_spec(session, content_fixture, run_id="r1")
    out = OpenTagProducer().produce(spec, session, MOCK_LLM, MOCK_EMBEDDER)
    # r1 三候选 confidence 均 >= 0.7，无过滤截断
    assert len(out) == 3
    assert all(t.confidence >= 0.7 for t in out)
    assert all(t.evidence == [] for t in out)
    # confidence 降序
    confs = [t.confidence for t in out]
    assert confs == sorted(confs, reverse=True)
    # 匹配分支：恰好首候选命中播种标签
    catalog_hits = [t for t in out if t.origin == "catalog"]
    assert len(catalog_hits) == 1
    hit = catalog_hits[0]
    assert hit.tag_id == "tag_open_1" and hit.tag_name == "多线叙事"
    assert hit.relevance_score == 1.0  # 构造保证的精确余弦（含等于过阈值 0.85）
    assert hit.candidate_type is None and hit.candidate_description is None
    # 未匹配分支：其余两候选 generated，candidate_* 齐、relevance_score 为 None
    generated = [t for t in out if t.origin == "generated"]
    assert {t.tag_name for t in generated} == {"情感张力", "视听风格"}
    assert all(t.tag_id is None and t.relevance_score is None for t in generated)
    assert all(t.candidate_type and t.candidate_description for t in generated)
    # tag_id 只能来自匹配层：库内 id 或 None，绝不出现其他值
    assert {t.tag_id for t in out} <= {"tag_open_1", None}


def test_open_empty_catalog_all_generated(session, content_fixture):
    """空库：全部候选落 generated 且不报错（与模式 A 空候选显式失败相反）。"""
    spec = _build_spec(session, content_fixture, run_id="r1")
    out = OpenTagProducer().produce(spec, session, MOCK_LLM, MOCK_EMBEDDER)
    assert len(out) == 3
    assert all(t.origin == "generated" for t in out)
    assert all(t.tag_id is None and t.relevance_score is None for t in out)
    assert all(t.candidate_type and t.candidate_description for t in out)
    assert {t.tag_name for t in out} == _SEED_NAMES


def test_open_match_threshold_includes_equality(session, content_fixture,
                                                monkeypatch):
    """阈值边界：相似度恰好等于阈值仍归 catalog（含等于）。

    阈值抬到 1.0：唯一能过线的相似度是构造出的精确 1.0（同 canonical 形）；
    若实现误用严格大于（1.0 > 1.0 不成立），首候选将落 generated → 测试失败。
    """
    _seed_open_catalog(session)
    spec = _build_spec(session, content_fixture, run_id="r1")
    monkeypatch.setattr(
        open_gen, "get_settings",
        lambda: Settings(open_match_threshold=1.0))
    out = OpenTagProducer().produce(spec, session, MOCK_LLM, MOCK_EMBEDDER)
    catalog_hits = [t for t in out if t.origin == "catalog"]
    assert len(catalog_hits) == 1
    assert catalog_hits[0].tag_id == "tag_open_1"
    assert catalog_hits[0].relevance_score == 1.0
    assert {t.tag_name for t in out if t.origin == "generated"} == {
        "情感张力", "视听风格"}


# ---- 过滤与截断 ----


def test_open_min_confidence_filters(session, content_fixture):
    """min_confidence 过滤：r4 仅情感张力（0.8929）达标；低置信候选（含本可
    匹配目录的多线叙事 0.6055）一律剔除。"""
    _seed_open_catalog(session)
    spec = _build_spec(session, content_fixture, run_id="r4")
    out = OpenTagProducer().produce(spec, session, MOCK_LLM, MOCK_EMBEDDER)
    assert [t.tag_name for t in out] == ["情感张力"]
    assert out[0].origin == "generated" and out[0].confidence >= 0.7


def test_open_max_tags_truncates_by_confidence(session, content_fixture):
    """max_tags 截断：confidence 降序后取前 max_tags 个。"""
    spec = _build_spec(
        session, content_fixture, run_id="r7",
        policy=GenerationPolicy(max_tags=1))
    out = OpenTagProducer().produce(spec, session, MOCK_LLM, MOCK_EMBEDDER)
    # r7 降序：多线叙事(0.9467) > 情感张力(0.932) > 视听风格(0.8179)
    assert [t.tag_name for t in out] == ["多线叙事"]
    assert out[0].origin == "generated"


# ---- LLM 失败与 tag_id 防伪造 ----


class _FailingLLM:
    """ok=False 的桩 LLM：模拟传输失败（produce 第 1 步显式抛错）。"""

    def complete(self, messages, run_id):
        return LLMResponse(content="", usage={}, raw={"error": "boom"}, ok=False)


def test_open_llm_failure_raises(session, content_fixture):
    spec = _build_spec(session, content_fixture, run_id="r1")
    with pytest.raises(RuntimeError, match="LLM 调用失败"):
        OpenTagProducer().produce(spec, session, _FailingLLM(), MOCK_EMBEDDER)


def test_open_ignores_llm_tag_id():
    """Review Focus 4：LLM 输出中的 tag_id（伪造目录外键）在解析层静默丢弃。"""
    content = json.dumps([
        {"tag_id": "hacked", "tag_name": "多线叙事", "tag_type": "narrative",
         "description": "d", "confidence": 0.9, "evidence": []},
    ], ensure_ascii=False)
    items = parse_open_output(content)
    assert "tag_id" not in items[0]


class _HackedLLM:
    """伪造 tag_id 的桩 LLM：输出契约五键 + 非法 tag_id="hacked"。"""

    def complete(self, messages, run_id):
        content = json.dumps([
            {"tag_id": "hacked", "tag_name": "多线叙事",
             "tag_type": "narrative",
             "description": "叙事在多条线索或时间线之间交替推进。",
             "confidence": 0.9,
             "evidence": [{"scene_id": "sc-9", "start_ms": 0, "end_ms": 100,
                           "description": "伪造证据"}]},
        ], ensure_ascii=False)
        return LLMResponse(content=content, usage={}, raw={}, ok=True)


def test_open_produce_reassigns_forged_tag_id(session, content_fixture):
    """端到端防伪造：LLM 塞入的 tag_id 不透传，tag_id 由匹配层按相似度赋值。"""
    _seed_open_catalog(session)
    spec = _build_spec(session, content_fixture, run_id="r1")
    out = OpenTagProducer().produce(spec, session, _HackedLLM(), MOCK_EMBEDDER)
    assert len(out) == 1
    assert out[0].origin == "catalog"
    assert out[0].tag_id == "tag_open_1"  # 匹配层赋值，绝非 "hacked"
    # evidence 经契约校验映射为 Evidence 列表
    assert out[0].evidence[0].scene_id == "sc-9"
