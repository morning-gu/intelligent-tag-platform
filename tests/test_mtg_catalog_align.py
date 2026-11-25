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

from tagplatform.capabilities.storage.models import TagCatalog, TagDefine
from tagplatform.services.tags.library import (
    library_from_session,
    library_index,
    try_load_library,
)
from tests.library_fixture import add_tags
from tagplatform.services.generation import GenerationPolicy, GenerationSpec
from tagplatform.services.generation.open.mtg import pipeline as mtg_pipeline
from tagplatform.services.generation.open.mtg.catalog_align import (
    BAND_DUPLICATE,
    BAND_NEW,
    BAND_SUSPECT,
    LibraryTag,
    align_candidates,
    recall_library_hints,
    summarize_bands,
)
from tagplatform.services.generation.open.producer import OpenTagProducer
from tagplatform.settings import Settings
from tagplatform.capabilities.vectors import encode_vector


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


def _add_catalog(session, tag_id, name, *, dir_id="dir_1", status="active",
                 description="", embedding=True):
    """插一行 tag_define（v2 标签本体）；embedding=False 模拟"没算过向量"的历史行。"""
    if session.get(TagCatalog, dir_id) is None:
        session.add(TagCatalog(dir_id=dir_id, dir_name="类型", dir_type="dir_static",
                               description="d", applicable_scope="content"))
        session.flush()
    session.add(TagDefine(
        tag_id=tag_id, tag_name=name, tag_type="val_fixed", description=description,
        dir_id=dir_id, status=status,
        embedding=(encode_vector(MOCK_EMBEDDER.embed([f"{name}。{description}"], role="passage")[0])
                   if embedding else None),
    ))


# ---------- 索引 ----------

def test_library_index_reuses_persisted_embeddings_and_skips_invalid(session):
    """有效持久化向量直接复用（零重嵌）；废弃行与模板占位行不进索引。"""
    _add_catalog(session, "tag_leaf", "悬疑", description="制造悬念的氛围")
    _add_catalog(session, "tag_dep", "废弃标签", status="deprecated")
    _add_catalog(session, "tag_bad", "按 YYYY-MM-DD 填写")
    session.commit()

    class _NoCallEmbedder:
        name = "nocall"
        dim = MOCK_EMBEDDER.dim

        def embed(self, texts, *, role):
            raise AssertionError(f"不应重新嵌入：{texts}")

    library = library_from_session(session)
    index = library_index(library, _NoCallEmbedder(), dir_ids=["dir_1"])
    assert [t.name for t in index] == ["悬疑"]
    expected = MOCK_EMBEDDER.embed(["悬疑。制造悬念的氛围"], role="passage")[0]
    assert np.allclose(index[0].vector, expected)


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
        prompt_text="", run_id=run_id, library=try_load_library(),
    )


def _seed_json(tag_id: str, name: str, *, dir_id: str = "dir_theme") -> None:
    """往 JSON 标签库塞一条标签（流水线只认库，不认 tag_catalog）。"""
    add_tags([{"tag_id": tag_id, "dir_id": dir_id, "tag_name": name,
               "tag_type": "val_fixed", "description": name,
               "status": "active"}])


@pytest.fixture
def mtg_settings(tmp_path, monkeypatch):
    settings = Settings(
        use_mock_llm=True,
        dataset_root=str(tmp_path / "movienet"),
        mtg_report_dir=str(tmp_path / "audit"),
    )
    monkeypatch.setattr(mtg_pipeline, "get_settings", lambda: settings)
    return settings


def _two_scenes(content):
    original = content.content.scenes[0]
    second = original.model_copy(update={"scene_id": "scene_002"})
    return content.model_copy(update={"content": content.content.model_copy(
        update={"scenes": [original, second]})})


def test_pipeline_marks_duplicate_and_keeps_generated(
        session, content_fixture, mtg_settings):
    _seed_json("tag_cat_1", "多线叙事")
    content = _two_scenes(content_fixture)
    spec = _build_spec(content, run_id="align1",
                       policy=GenerationPolicy(dir_ids=["dir_theme"]))

    out = OpenTagProducer().produce(spec, session, MOCK_LLM, MOCK_EMBEDDER)
    by_name = {tag.tag_name: tag for tag in out}
    assert by_name["多线叙事"].origin == "catalog"
    assert by_name["多线叙事"].tag_id == "tag_cat_1"
    assert any(tag.origin == "generated" for tag in out)
    report = json.loads(pathlib.Path(mtg_settings.mtg_report_dir, "t-align1.json")
                        .read_text(encoding="utf-8"))
    assert report["library"]["bands"][BAND_DUPLICATE] == 1
    assert report["library"]["size"] == 3
    assert report["library"]["hints"]


def test_pipeline_ranks_library_and_generated_items_under_same_stage_quota(
        session, content_fixture, mtg_settings):
    _seed_json("tag_cat_1", "多线叙事")
    content = _two_scenes(content_fixture)
    spec = _build_spec(content, run_id="align2",
                       policy=GenerationPolicy(max_tags=2, dir_ids=["dir_theme"]))

    out = OpenTagProducer().produce(spec, session, MOCK_LLM, MOCK_EMBEDDER)
    assert len(out) == 2
    assert all(tag.origin == "generated" and tag.level == "film" for tag in out)
    report = json.loads(pathlib.Path(mtg_settings.mtg_report_dir, "t-align2.json")
                        .read_text(encoding="utf-8"))
    assert any(row["tag"] == "多线叙事" and row["band"] == BAND_DUPLICATE and row["stage"] == 2
               for row in report["library"]["truncated"])


def test_library_index_backfills_missing_and_wrong_dimension_embeddings(session):
    """缺失或维度不符的向量按统一文本口径（名。描述，passage）补算，有效行直接复用。"""
    _add_catalog(session, "tag_ok", "复仇", description="复仇母题")
    _add_catalog(session, "tag_noemb", "没向量", description="缺失向量", embedding=False)
    _add_catalog(session, "tag_old", "背叛", description="旧维度向量")
    row = session.get(TagDefine, "tag_old")
    row.embedding = encode_vector(np.zeros(3, dtype="float32") + 0.5)
    session.commit()

    calls: list[tuple[str, list[str]]] = []

    class _TrackingEmbedder:
        name = "tracking"
        dim = MOCK_EMBEDDER.dim

        def embed(self, texts, *, role):
            calls.append((role, list(texts)))
            return MOCK_EMBEDDER.embed(texts, role=role)

    library = library_from_session(session)
    index = library_index(library, _TrackingEmbedder(), dir_ids=["dir_1"])

    assert {tag.name for tag in index} == {"复仇", "没向量", "背叛"}
    assert all(tag.vector.shape == (MOCK_EMBEDDER.dim,) for tag in index)
    assert calls == [("passage", ["没向量。缺失向量", "背叛。旧维度向量"])]


class _VerifyClient:
    """复核桩：按构造参数决定"是否同一概念"。"""

    def __init__(self, same: bool):
        self.same = same
        self.calls = 0

    def complete(self, messages, run_id):
        from tagplatform.capabilities.llm.client import LLMResponse
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
    _seed_json("tag_cat_1", "多线")
    monkeypatch.setattr(mtg_pipeline, "_make_verify_client",
                        lambda settings, llm: _VerifyClient(same=True))

    spec = _build_spec(content_fixture, run_id="align4")
    out = OpenTagProducer().produce(spec, session, MOCK_LLM, MOCK_EMBEDDER)

    replaced = [t for t in out if t.origin == "catalog"]
    assert replaced and replaced[0].tag_name == "多线"
    assert replaced[0].tag_id == "tag_cat_1"
    report = json.loads(pathlib.Path(
        mtg_settings.mtg_report_dir, "t-align4.json").read_text(encoding="utf-8"))
    replacement, = report["library"]["replacements"]
    assert replacement["generated"] == "多线叙事"
    assert replacement["library"] == "多线"
    assert replacement["tag_id"] == "tag_cat_1"
    assert replacement["candidate_id"] == report["accepted"][0]["candidate_id"]


def test_pipeline_keeps_candidate_when_verify_says_different(
        session, content_fixture, mtg_settings, monkeypatch):
    """复核判定"不是同一概念" → 保留生成的原词，不替换。"""
    _seed_json("tag_cat_1", "多线")
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
    # 复核否定 → 分档改回 new：它确实是一条新标签，不该继续当疑似压排名
    row = next(r for r in report["library"]["align"] if r["tag"] == "多线叙事")
    assert row["band"] == "new" and row["same_concept"] is False
    assert "复核判非同一概念" in row["reason"]


def test_pipeline_replace_switch_off_keeps_generated(
        session, content_fixture, mtg_settings, monkeypatch):
    """替换开关关掉 → 疑似即使复核通过也不替换（同名仍算库内标签）。"""
    _seed_json("tag_cat_1", "多线")
    monkeypatch.setattr(mtg_pipeline, "_make_verify_client",
                        lambda settings, llm: _VerifyClient(same=True))
    monkeypatch.setattr(mtg_settings, "open_library_replace_enabled", False)

    spec = _build_spec(content_fixture, run_id="align6")
    out = OpenTagProducer().produce(spec, session, MOCK_LLM, MOCK_EMBEDDER)

    assert all(t.tag_name != "多线" for t in out)


def test_pipeline_switch_off_disables_library(
        session, content_fixture, mtg_settings, monkeypatch):
    """总开关关掉 → 不注入提示、不分档，全部按旧行为走 generated。"""
    _seed_json("tag_cat_1", "多线叙事")
    monkeypatch.setattr(mtg_settings, "open_library_enabled", False)

    spec = _build_spec(content_fixture, run_id="align3")
    out = OpenTagProducer().produce(spec, session, MOCK_LLM, MOCK_EMBEDDER)

    assert out
    assert all(t.origin == "generated" for t in out)
    report = json.loads(pathlib.Path(
        mtg_settings.mtg_report_dir, "t-align3.json").read_text(encoding="utf-8"))
    assert report["library"]["enabled"] is False
    assert report["library"]["hints"] == []

def test_duplicate_library_names_pick_deterministically():
    """库里同名多条（"其他" 出现 30 次）→ 稳定取 tag_id 最小的一条。"""
    from tagplatform.services.generation.open.mtg.catalog_align import LibraryTag, align_candidates

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