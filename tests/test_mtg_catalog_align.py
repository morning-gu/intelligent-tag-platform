"""库内标签对齐（catalog_align）测试：分档规则、提示召回、流水线接入。

契约：
- 库内索引只取叶子标签（目录节点不算），排除废弃与无向量行；
- 分档顺序：同名 → 名字包含 → 字符重合 → 余弦 → 新词，先命中先算数；
- 只有"同名"落 origin=catalog，疑似/新词仍走 generated（只标记不删）；
- 分档参与排序：同重要度下新词优先占名额。

替身说明：真实 e5 的分数分布已在 8101 上标定（阈值 0.87 ≈ 随机配对
99.9% 分位），单测不用真模型——手写向量直接验证判定分支。
"""

import json
import pathlib

import numpy as np
import pytest
from conftest import MOCK_EMBEDDER, MOCK_LLM

from tagplatform.db.models import TagCatalog
from tagplatform.generation import GenerationPolicy, GenerationSpec
from tagplatform.generation.mtg import pipeline as mtg_pipeline
from tagplatform.generation.mtg.catalog_align import (
    BAND_DUPLICATE,
    BAND_NEW,
    BAND_SUSPECT,
    LibraryTag,
    align_candidates,
    load_library_index,
    recall_library_hints,
    summarize_bands,
)
from tagplatform.generation.open_gen import OpenTagProducer
from tagplatform.settings import Settings
from tagplatform.vectors import encode_vector


class _FixedEmbedder:
    """按文本返回预置向量的替身（只用于验证判定分支，不代表语义）。"""

    name = "fixed"

    def __init__(self, mapping: dict[str, list[float]]):
        self.mapping = mapping

    def embed(self, texts, *, role):
        return np.stack([np.asarray(self.mapping[t], dtype=np.float32)
                         for t in texts])


def _tag(tag_id: str, name: str, vector: list[float]) -> LibraryTag:
    return LibraryTag(tag_id=tag_id, name=name,
                      vector=np.asarray(vector, dtype=np.float32))


def _add_catalog(session, tag_id, name, *, parent_id=None, status="active",
                 embedding=True):
    """插一行 tag_catalog；embedding=False 模拟"没算过向量"的历史行。"""
    session.add(TagCatalog(
        tag_id=tag_id, tag_name=name, tag_type="genre", description="",
        parent_id=parent_id, status=status,
        embedding=(encode_vector(MOCK_EMBEDDER.embed([name], role="passage")[0])
                   if embedding else None),
    ))


# ---------- 索引 ----------

def test_load_library_index_keeps_leaves_only(session):
    """目录节点（有子节点）与废弃/无向量行都不参与对齐。"""
    _add_catalog(session, "tag_root", "内容元数据标签")
    _add_catalog(session, "tag_leaf", "悬疑", parent_id="tag_root")
    _add_catalog(session, "tag_dep", "废弃标签", status="deprecate")
    _add_catalog(session, "tag_noemb", "没向量", embedding=False)
    session.commit()

    index = load_library_index(session)
    assert [t.name for t in index] == ["悬疑"]


# ---------- 分档 ----------

def test_align_duplicate_by_exact_name():
    """同名 → duplicate，并带回库内 tag_id（落 origin=catalog 用）。"""
    lib = [_tag("t1", "悬疑", [1.0, 0.0, 0.0])]
    embedder = _FixedEmbedder({"悬疑": [1.0, 0.0, 0.0]})

    result = align_candidates(lib, embedder, ["悬疑"],
                              suspect_cosine=0.87, name_overlap=0.4)
    item = result["悬疑"]
    assert item.band == BAND_DUPLICATE
    assert item.matched_tag_id == "t1"
    assert item.reason == "与库内标签同名"


def test_align_duplicate_ignores_punctuation():
    """归一化后同名也算重复（"多线 叙事" vs "多线叙事"）。"""
    lib = [_tag("t1", "多线叙事", [1.0, 0.0, 0.0])]
    embedder = _FixedEmbedder({"多线 叙事": [0.9, 0.1, 0.0]})

    result = align_candidates(lib, embedder, ["多线 叙事"],
                              suspect_cosine=0.87, name_overlap=0.4)
    assert result["多线 叙事"].band == BAND_DUPLICATE


def test_align_suspect_by_containment():
    """库名被候选包含（≥2 字）→ suspect，reason 点名被包含的库标签。"""
    lib = [_tag("t1", "悬疑", [1.0, 0.0, 0.0])]
    embedder = _FixedEmbedder({"悬疑神秘": [0.0, 1.0, 0.0]})

    result = align_candidates(lib, embedder, ["悬疑神秘"],
                              suspect_cosine=0.87, name_overlap=0.4)
    item = result["悬疑神秘"]
    assert item.band == BAND_SUSPECT
    assert "悬疑" in item.reason


def test_align_suspect_by_char_overlap():
    """字符重合 ≥ 阈值 → suspect（"荒诞幽默" vs "荒诞怪诞" 重合 0.4）。"""
    lib = [_tag("t1", "荒诞怪诞", [1.0, 0.0, 0.0])]
    embedder = _FixedEmbedder({"荒诞幽默": [0.0, 1.0, 0.0]})

    result = align_candidates(lib, embedder, ["荒诞幽默"],
                              suspect_cosine=0.87, name_overlap=0.4)
    assert result["荒诞幽默"].band == BAND_SUSPECT
    assert "字符重合" in result["荒诞幽默"].reason


def test_align_suspect_by_cosine():
    """名字无关但余弦过线 → suspect（"惊悚片" vs "悬疑" 手写 0.95）。"""
    lib = [_tag("t1", "悬疑", [1.0, 0.0, 0.0])]
    embedder = _FixedEmbedder({"惊悚片": [0.95, 0.3122, 0.0]})

    result = align_candidates(lib, embedder, ["惊悚片"],
                              suspect_cosine=0.87, name_overlap=0.4)
    item = result["惊悚片"]
    assert item.band == BAND_SUSPECT
    assert "相似度" in item.reason
    assert item.cosine >= 0.87


def test_align_new_when_nothing_matches():
    """名字无关且余弦低 → new，但仍记录最像的库标签供事后复查。"""
    lib = [_tag("t1", "悬疑", [1.0, 0.0, 0.0])]
    embedder = _FixedEmbedder({"洛杉矶街景": [0.0, 1.0, 0.0]})

    result = align_candidates(lib, embedder, ["洛杉矶街景"],
                              suspect_cosine=0.87, name_overlap=0.4)
    item = result["洛杉矶街景"]
    assert item.band == BAND_NEW
    assert item.matched_name == "悬疑"
    assert item.matched_tag_id is None


def test_align_empty_library_returns_empty():
    """库为空 → 不做任何判定（调用方按"全部新词"处理）。"""
    assert align_candidates([], _FixedEmbedder({}), ["悬疑"],
                            suspect_cosine=0.87, name_overlap=0.4) == {}


def test_summarize_bands_counts():
    lib = [_tag("t1", "悬疑", [1.0, 0.0, 0.0])]
    embedder = _FixedEmbedder({
        "悬疑": [1.0, 0.0, 0.0],
        "悬疑神秘": [0.0, 1.0, 0.0],
        "洛杉矶街景": [0.0, 0.0, 1.0],
    })
    result = align_candidates(lib, embedder, ["悬疑", "悬疑神秘", "洛杉矶街景"],
                              suspect_cosine=0.87, name_overlap=0.4)
    assert summarize_bands(result) == {
        BAND_DUPLICATE: 1, BAND_SUSPECT: 1, BAND_NEW: 1}


# ---------- 提示召回 ----------

def test_recall_library_hints_returns_top_n_in_order():
    """内容嵌入 → 按相关度降序取 top_n 个名字。"""
    lib = [_tag("t1", "悬疑", [1.0, 0.0]), _tag("t2", "复仇", [0.0, 1.0]),
           _tag("t3", "家庭", [0.7, 0.7])]
    embedder = _FixedEmbedder({"摘要": [1.0, 0.0], "场景描述": [1.0, 0.0]})

    scene = type("Scene", (), {"description": "场景描述"})()
    body = type("Body", (), {"summary": "摘要", "scenes": [scene]})()
    content = type("Content", (), {"content": body})()

    hints = recall_library_hints(lib, embedder, content, top_n=2)
    assert hints == ["悬疑", "家庭"]


def test_recall_library_hints_empty_index():
    assert recall_library_hints([], _FixedEmbedder({}), object(), top_n=5) == []


# ---------- 流水线接入 ----------

def _build_spec(content_fixture, run_id: str, policy=None) -> GenerationSpec:
    return GenerationSpec(
        task_id=f"t-{run_id}", asset_id=content_fixture.asset.asset_id,
        content=content_fixture, policy=policy or GenerationPolicy(),
        prompt_text="", run_id=run_id,
    )


@pytest.fixture
def mtg_settings(tmp_path, monkeypatch):
    settings = Settings(
        use_mock_llm=True,
        dataset_root=str(tmp_path / "movienet"),
        mtg_report_dir=str(tmp_path / "audit"),
    )
    monkeypatch.setattr(mtg_pipeline, "get_settings", lambda: settings)
    return settings


def test_pipeline_marks_duplicate_and_keeps_generated(
        session, content_fixture, mtg_settings):
    """库里有同名标签 → 该条 origin=catalog；其余仍 generated，审计带分档。"""
    vec = MOCK_EMBEDDER.embed(["多线叙事"], role="passage")[0]
    session.add(TagCatalog(tag_id="tag_cat_1", tag_name="多线叙事",
                           tag_type="narrative", description="多线叙事",
                           status="active", embedding=encode_vector(vec)))
    session.commit()

    spec = _build_spec(content_fixture, run_id="align1")
    out = OpenTagProducer().produce(spec, session, MOCK_LLM, MOCK_EMBEDDER)

    by_name = {t.tag_name: t for t in out}
    assert by_name["多线叙事"].origin == "catalog"
    assert by_name["多线叙事"].tag_id == "tag_cat_1"
    others = [t for t in out if t.tag_name != "多线叙事"]
    assert others and all(t.origin == "generated" for t in others)

    report = json.loads(
        (pathlib.Path(mtg_settings.mtg_report_dir, "t-align1.json")
         .read_text(encoding="utf-8")))
    assert report["library"]["bands"][BAND_DUPLICATE] == 1
    assert report["library"]["size"] == 1
    bands = {row["tag"]: row["band"] for row in report["library"]["align"]}
    assert bands["多线叙事"] == BAND_DUPLICATE
    assert len(report["library"]["hints"]) == 1


def test_pipeline_ranking_prefers_new_over_duplicate(
        session, content_fixture, mtg_settings, monkeypatch):
    """名额只有 1 时，新词占那个名额；命中的库内标签另算、不挤新词。"""
    vec = MOCK_EMBEDDER.embed(["多线叙事"], role="passage")[0]
    session.add(TagCatalog(tag_id="tag_cat_1", tag_name="多线叙事",
                           tag_type="narrative", description="多线叙事",
                           status="active", embedding=encode_vector(vec)))
    session.commit()

    spec = _build_spec(content_fixture, run_id="align2",
                       policy=GenerationPolicy(max_tags=1))
    out = OpenTagProducer().produce(spec, session, MOCK_LLM, MOCK_EMBEDDER)

    # 新词 1 条占满名额，库内命中 1 条额外带出（不占名额）
    assert len(out) == 2
    assert out[0].origin == "generated" and out[0].tag_name != "多线叙事"
    assert out[1].origin == "catalog" and out[1].tag_name == "多线叙事"


def test_replaced_tags_do_not_consume_quota(
        session, content_fixture, mtg_settings, monkeypatch):
    """库内命中再多，也不把新词挤掉：新词始终按 limit 走自己的桶。"""
    for index in range(3):
        vec = MOCK_EMBEDDER.embed([f"占位{index}"], role="passage")[0]
        session.add(TagCatalog(tag_id=f"tag_fill_{index}", tag_name=f"占位{index}",
                               tag_type="narrative", description="占位",
                               status="active", embedding=encode_vector(vec)))
    vec = MOCK_EMBEDDER.embed(["多线叙事"], role="passage")[0]
    session.add(TagCatalog(tag_id="tag_cat_1", tag_name="多线叙事",
                           tag_type="narrative", description="多线叙事",
                           status="active", embedding=encode_vector(vec)))
    session.commit()

    spec = _build_spec(content_fixture, run_id="align7",
                       policy=GenerationPolicy(max_tags=2))
    out = OpenTagProducer().produce(spec, session, MOCK_LLM, MOCK_EMBEDDER)

    generated = [t for t in out if t.origin == "generated"]
    replaced = [t for t in out if t.origin == "catalog"]
    assert len(generated) == 2          # mock 三候选、去掉同名那条后剩两条
    assert len(replaced) == 1
    report = json.loads(pathlib.Path(
        mtg_settings.mtg_report_dir, "t-align7.json").read_text(encoding="utf-8"))
    assert report["accepted"][0]["origin"] == "generated"


def _seed_library(session, tag_id: str, name: str) -> None:
    vec = MOCK_EMBEDDER.embed([name], role="passage")[0]
    session.add(TagCatalog(tag_id=tag_id, tag_name=name, tag_type="narrative",
                           description=name, status="active",
                           embedding=encode_vector(vec)))
    session.commit()


class _VerifyClient:
    """复核桩：按构造参数决定"是否同一概念"。"""

    def __init__(self, same: bool):
        self.same = same
        self.calls = 0

    def complete(self, messages, run_id):
        from tagplatform.llm.client import LLMResponse
        self.calls += 1
        text = messages[0]["content"]
        rows = [line[2:] for line in text.splitlines()
                if line.startswith("- ") and "｜" in line]
        results = [{"candidate": row.split("｜")[0], "same": self.same}
                   for row in rows]
        return LLMResponse(
            content=json.dumps({"results": results}, ensure_ascii=False),
            usage={'prompt_tokens': 10, 'completion_tokens': 5}, raw={}, ok=True)


def test_pipeline_replaces_suspect_when_verified(
        session, content_fixture, mtg_settings, monkeypatch):
    """疑似对子复核通过 → 候选名换成库内标准名，原词进审计的替换记录。"""
    # 库里只有"多线"，mock 生成"多线叙事" → 名字包含 → 疑似
    _seed_library(session, "tag_cat_1", "多线")
    monkeypatch.setattr(mtg_pipeline, "_make_verify_client",
                        lambda settings, llm: _VerifyClient(same=True))

    spec = _build_spec(content_fixture, run_id="align4")
    out = OpenTagProducer().produce(spec, session, MOCK_LLM, MOCK_EMBEDDER)

    replaced = [t for t in out if t.origin == "catalog"]
    assert replaced and replaced[0].tag_name == "多线"
    assert replaced[0].tag_id == "tag_cat_1"
    report = json.loads(pathlib.Path(
        mtg_settings.mtg_report_dir, "t-align4.json").read_text(encoding="utf-8"))
    assert report["library"]["replacements"] == [{
        "generated": "多线叙事", "library": "多线", "tag_id": "tag_cat_1",
        "cosine": report["library"]["replacements"][0]["cosine"],
        "reason": report["library"]["replacements"][0]["reason"],
    }]


def test_pipeline_keeps_candidate_when_verify_says_different(
        session, content_fixture, mtg_settings, monkeypatch):
    """复核判定"不是同一概念" → 保留生成的原词，不替换。"""
    _seed_library(session, "tag_cat_1", "多线")
    monkeypatch.setattr(mtg_pipeline, "_make_verify_client",
                        lambda settings, llm: _VerifyClient(same=False))

    spec = _build_spec(content_fixture, run_id="align5")
    out = OpenTagProducer().produce(spec, session, MOCK_LLM, MOCK_EMBEDDER)

    names = {t.tag_name for t in out}
    assert "多线叙事" in names
    assert all(t.origin == "generated" for t in out if t.tag_name == "多线叙事")
    report = json.loads(pathlib.Path(
        mtg_settings.mtg_report_dir, "t-align5.json").read_text(encoding="utf-8"))
    assert report["library"]["replacements"] == []


def test_pipeline_replace_switch_off_keeps_generated(
        session, content_fixture, mtg_settings, monkeypatch):
    """替换开关关掉 → 疑似即使复核通过也不替换（同名仍算库内标签）。"""
    _seed_library(session, "tag_cat_1", "多线")
    monkeypatch.setattr(mtg_pipeline, "_make_verify_client",
                        lambda settings, llm: _VerifyClient(same=True))
    monkeypatch.setattr(mtg_settings, "open_library_replace_enabled", False)

    spec = _build_spec(content_fixture, run_id="align6")
    out = OpenTagProducer().produce(spec, session, MOCK_LLM, MOCK_EMBEDDER)

    assert all(t.tag_name != "多线" for t in out)


def test_pipeline_switch_off_disables_library(
        session, content_fixture, mtg_settings, monkeypatch):
    """总开关关掉 → 不注入提示、不分档，全部按旧行为走 generated。"""
    vec = MOCK_EMBEDDER.embed(["多线叙事"], role="passage")[0]
    session.add(TagCatalog(tag_id="tag_cat_1", tag_name="多线叙事",
                           tag_type="narrative", description="多线叙事",
                           status="active", embedding=encode_vector(vec)))
    session.commit()
    monkeypatch.setattr(mtg_settings, "open_library_enabled", False)

    spec = _build_spec(content_fixture, run_id="align3")
    out = OpenTagProducer().produce(spec, session, MOCK_LLM, MOCK_EMBEDDER)

    assert all(t.origin == "generated" for t in out)
    report = json.loads(pathlib.Path(
        mtg_settings.mtg_report_dir, "t-align3.json").read_text(encoding="utf-8"))
    assert report["library"]["enabled"] is False
    assert report["library"]["hints"] == []

def test_duplicate_library_names_pick_deterministically():
    """库里同名多条（"其他" 出现 30 次）→ 稳定取 tag_id 最小的一条。"""
    from tagplatform.generation.mtg.catalog_align import LibraryTag, align_candidates

    vec = MOCK_EMBEDDER.embed(["其他"], role="passage")[0]
    index = [LibraryTag(tag_id="tag_b", name="其他", vector=vec),
             LibraryTag(tag_id="tag_a", name="其他", vector=vec)]
    out = align_candidates(index, MOCK_EMBEDDER, ["其他"],
                           suspect_cosine=0.87, name_overlap=0.4)
    assert out["其他"].band == "duplicate"
    assert out["其他"].matched_tag_id == "tag_a"
    assert "同名 2 条" in out["其他"].reason

    # 顺序反过来结果不变
    out2 = align_candidates(list(reversed(index)), MOCK_EMBEDDER, ["其他"],
                            suspect_cosine=0.87, name_overlap=0.4)
    assert out2["其他"].matched_tag_id == "tag_a"