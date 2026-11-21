"""模式 A 封闭式标签提取测试：混合召回、输出解析与 ClosedTagProducer。

覆盖契约：
- embed_content_texts：[剧情摘要, 各场景描述] 查询文本列表；
- recall_candidates：向量 + BM25（标签名/描述/别名）双通道，按 tag_id
  归一去重，RRF 排序并返回全局 top_k；排除 deprecate / 占位行，范围
  dir_ids 生效，空库返回 []；候选 score 保留余弦供闭环终分使用；
- parse_closed_output：键集与分值域校验、坏结构抛 LLMParseError；
- ClosedTagProducer：库缺失/空候选 → ValueError；dir_asset 目录的候选
  召回后剔除（不建组、不调 LLM、不产出，范围内全是 asset 目录按空
  候选显式失败）；终分 Sf = G·(0.2·Semb + 0.5·Sapp + 0.3·Sevi)（门控
  Sapp≥0.75，全局选择 max_tags=全局上限 M），Sf 与标签成对绑定
  （relevance_score）；结果是 catalog 行，带目录与挂载点（目录
  applicable_scope → content/scene/character）。
"""

import json

import numpy as np
import pytest
from conftest import MOCK_EMBEDDER, MOCK_LLM
from sqlalchemy.orm import Session

from tagplatform.services.generation import Evidence, GenerationPolicy, GenerationSpec
from tagplatform.services.generation.closed import producer as closed_module
from tagplatform.services.generation.closed.producer import ClosedTagProducer
from tagplatform.services.generation.closed.recall import embed_content_texts, recall_candidates
from tagplatform.services.tags.library import (
    DIR_ASSET,
    Directory,
    TagDef,
    TagLibrary,
    try_load_library,
)
from tagplatform.capabilities.llm.client import LLMResponse
from tagplatform.services.generation.closed.parse import LLMParseError, parse_closed_output
from tagplatform.capabilities.prompts import get_prompt, render_prompt
from tagplatform.services.generation.assemble import _render_content_text
from tagplatform.settings import Settings
from tests.library_fixture import library_root, write_library

def _evi(ec: int, es: int, ef: int,
         entries: list[dict] | None = None) -> dict:
    """嵌套三模态 evidence 对象（v8 契约；entries 同进三模态节）。

    entries 为 None 时各模态节 evidences 皆为空列表——绝大多数用例只
    关心分值与门控/选择语义，证据展开有专项用例。
    """
    entries = entries or []
    return {m: {"evidence_strength": s, "evidences": list(entries)}
            for m, s in (("content", ec), ("scene", es), ("frame", ef))}


# 两个目录 × 各 3 条：dir_genre（静态）/ dir_mood（动态）
_LIB_TAGS = [
    ("tag_001", "悬疑", "dir_genre", "推理"),
    ("tag_002", "剧情", "dir_genre", "人物关系驱动"),
    ("tag_003", "动作", "dir_genre", "追逐打斗"),
    ("tag_004", "紧张", "dir_mood", "情绪紧绷"),
    ("tag_005", "温馨", "dir_mood", "柔和治愈"),
    ("tag_006", "压抑", "dir_mood", "低沉沉重"),
]


def _seed_library(*, status: dict | None = None,
                  extra: list[dict] | None = None,
                  dirs: list[dict] | None = None,
                  extra_dirs: list[dict] | None = None,
                  extra_tags: list[tuple] | None = None) -> None:
    """写一套干净的两目录库（dir_mood 动态、dir_genre 静态）。

    `dirs` 传入整份目录列表时原样落库（`extra` 仍是追加标签），供
    dir_asset 过滤等需要额外目录类型的用例使用；extra_dirs/extra_tags
    供挂载用例追加 scene/character scope 目录与标签（主目录集保持
    content scope 不变，老用例零改动）。
    """
    status = status or {}
    all_dirs = (_DEFAULT_DIRS if dirs is None else dirs) \
        + list(extra_dirs or [])
    tags = [{"tag_id": tag_id, "dir_id": dir_id, "tag_name": name,
             "tag_type": "val_fixed", "description": desc,
             "status": status.get(tag_id, "active")}
            for tag_id, name, dir_id, desc in _LIB_TAGS]
    tags += [{"tag_id": tag_id, "dir_id": dir_id, "tag_name": name,
              "tag_type": "val_fixed", "description": desc, "status": "active"}
             for tag_id, name, dir_id, desc in extra_tags or []]
    write_library(library_root(), dirs=all_dirs,
                  tags=tags + list(extra or []))


# 两目录默认库：dir_genre（静态）/ dir_mood（动态）
_DEFAULT_DIRS = [
    {"dir_id": "dir_genre", "parent_id": None, "level": 1,
     "dir_name": "主类型", "dir_type": "dir_static",
     "applicable_scope": "content", "description": "故事类型", "aliases": []},
    {"dir_id": "dir_mood", "parent_id": None, "level": 1,
     "dir_name": "情绪基调", "dir_type": "dir_dynamic",
     "applicable_scope": "content",
     "description": "情绪与氛围", "aliases": []},
]

# dir_asset 目录 + 其下两条标签（元数据导入来源，closed 不打分）
_ASSET_DIR = {"dir_id": "dir_actor", "parent_id": None, "level": 1,
              "dir_name": "演员关键词", "dir_type": DIR_ASSET,
              "applicable_scope": "content",
              "description": "联网搜索获取的演员标签", "aliases": []}
_ASSET_TAGS = [
    {"tag_id": "tag_101", "dir_id": "dir_actor", "tag_name": "张三",
     "tag_type": "val_fixed", "description": "主演", "status": "active"},
    {"tag_id": "tag_102", "dir_id": "dir_actor", "tag_name": "李四",
     "tag_type": "val_fixed", "description": "配角", "status": "active"},
]


def _seed_library_with_asset() -> None:
    """两目录默认库 + 一个 dir_asset 目录（挂两条会被召回的标签）。"""
    _seed_library(dirs=_DEFAULT_DIRS + [dict(_ASSET_DIR)],
                  extra=list(_ASSET_TAGS))


# ---- embed_content_texts ----


def test_embed_content_texts(content_fixture):
    queries = embed_content_texts(content_fixture)
    assert queries[0] == content_fixture.content.summary
    assert queries[1:] == [s.description for s in content_fixture.content.scenes]


# ---- recall_candidates：混合召回 / 全局 top-k / 去重 / 范围 ----


def test_closed_recall_global_top_k_and_dedup():
    _seed_library()
    cats = recall_candidates(try_load_library(), MOCK_EMBEDDER, ["悬疑推理"],
                             top_k=4)
    assert len({c.tag_id for c in cats}) == len(cats) == 4
    # 每条都带目录与挂载粒度（新词归属与 target_type 的依据）
    assert all(c.dir_id and c.scope == "content" for c in cats)


class _FixedRecallEmbedder:
    # name 是 Embedder 协议字段（library 的向量缓存键按它区分模型）——
    # 与其余替身（RecordingEmbedder/_FixedEmbedder 等）同约定
    name = "fixed-recall"
    dim = 2

    def __init__(self, passage_vectors: dict[str, list[float]]):
        self.passage_vectors = passage_vectors

    def embed(self, texts: list[str], *, role: str) -> np.ndarray:
        if role == "passage":
            vectors = [self.passage_vectors[text] for text in texts]
        else:
            vectors = [[1.0, 0.0] for _ in texts]
        return np.asarray(vectors, dtype=np.float32)


def _hybrid_recall_library() -> TagLibrary:
    return TagLibrary(
        [
            Directory("dir_vector", "向量目录"),
            Directory("dir_keyword", "关键词目录"),
        ],
        [
            TagDef("tag_vector", "dir_vector", "悬疑", description="制造谜团"),
            TagDef(
                "tag_revenge",
                "dir_keyword",
                "复仇",
                description="主角追查凶手",
                aliases=("报仇",),
            ),
        ],
    )


def test_closed_recall_fuses_keyword_alias_and_normalizes_to_tag_id():
    embedder = _FixedRecallEmbedder({
        "悬疑": [1.0, 0.0],
        "复仇": [0.8, 0.6],
    })
    candidates = recall_candidates(
        _hybrid_recall_library(), embedder, ["报仇"], top_k=1)

    assert len(candidates) == 1
    assert candidates[0].tag_id == "tag_revenge"
    assert candidates[0].tag_name == "复仇"
    assert candidates[0].score == pytest.approx(0.8)


def test_closed_recall_bm25_matches_tag_description():
    embedder = _FixedRecallEmbedder({
        "悬疑": [1.0, 0.0],
        "复仇": [0.8, 0.6],
    })
    candidates = recall_candidates(
        _hybrid_recall_library(), embedder, ["追查凶手"], top_k=1)

    assert [candidate.tag_id for candidate in candidates] == ["tag_revenge"]


def test_closed_recall_empty_library():
    assert recall_candidates(TagLibrary([], []), MOCK_EMBEDDER, ["任何"],
                             top_k=5) == []


def test_closed_recall_excludes_deprecate_and_placeholder():
    _seed_library(status={"tag_001": "deprecate"},
                  extra=[{"tag_id": "tag_bad", "dir_id": "dir_genre",
                          "tag_name": "按 YYYY-MM-DD 填写",
                          "tag_type": "val_fixed", "description": "模板占位",
                          "status": "active"}])
    cats = recall_candidates(try_load_library(), MOCK_EMBEDDER, ["悬疑推理"],
                             top_k=50)
    ids = {c.tag_id for c in cats}
    assert "tag_001" not in ids and "tag_bad" not in ids
    assert "tag_003" in ids  # 其余有效行正常召回


def test_closed_recall_respects_dir_scope():
    _seed_library()
    cats = recall_candidates(try_load_library(), MOCK_EMBEDDER, ["悬疑推理"],
                             dir_ids=["dir_mood"], top_k=50)
    assert cats and {c.dir_id for c in cats} == {"dir_mood"}
    assert all(c.scope == "content" for c in cats)


# ---- ClosedTagProducer：produce 七步流程（召回 → asset 过滤 → 判空 → 分组 → 逐组 LLM → 终分 → 映射） ----


def _build_spec(session: Session, content_fixture, run_id: str,
                policy=None) -> GenerationSpec:
    """按 runner 职责模拟组装：渲染 closed prompt（候选分组由 produce 注入）。

    四层重构 Task 3 起模板为本地常量（get_prompt），session 仅随签名保留。
    """
    library = try_load_library()
    policy = policy or GenerationPolicy()
    prompt_text = render_prompt(
        get_prompt("closed"),
        content=_render_content_text(content_fixture),
    )
    return GenerationSpec(
        task_id="t1", asset_id=content_fixture.asset.asset_id,
        content=content_fixture, policy=policy,
        prompt_text=prompt_text, run_id=run_id, library=library,
    )


class _ScriptedLLM:
    """按组返回固定判定结果（从 prompt 尾部的组 JSON 取 dir_id）。

    组 JSON 用 raw_decode 解析前缀：v7 重试 prompt 在 JSON 之后追加格式
    纠正提示，整段 json.loads 会因尾巴文本报 Extra data。
    """

    def __init__(self, responses: dict[str, list[dict]]):
        self._responses = responses

    def complete(self, messages, run_id):
        tail = messages[0]["content"].partition(
            "\n\n## 候选标签分组\n")[2]
        dir_id = json.JSONDecoder().raw_decode(tail)[0]["dir_id"]
        return LLMResponse(
            content=json.dumps(self._responses[dir_id], ensure_ascii=False),
            usage={}, raw={}, ok=True)


def test_closed_produce_end_to_end(session, content_fixture):
    _seed_library()
    spec = _build_spec(session, content_fixture, run_id="r2")
    out = ClosedTagProducer().produce(spec, session, MOCK_LLM, MOCK_EMBEDDER)
    assert out and all(t.origin == "catalog" and t.tag_id for t in out)
    assert all(t.confidence >= 0.7 for t in out) and len(out) <= 30
    assert any(t.tag_id == "tag_001" for t in out)  # 必选中 run_id（r2）
    # catalog 来源：candidate_* 恒为 None；目录与挂载点来自库
    assert all(t.candidate_type is None and t.candidate_description is None
               for t in out)
    assert all(t.dir_id in {"dir_genre", "dir_mood"} for t in out)
    assert all(t.target_type == "content" and t.target_id
               == content_fixture.asset.asset_id for t in out)
    # 不编造目录外标签
    assert {t.tag_id for t in out} <= {tid for tid, *_ in _LIB_TAGS}


def test_closed_produce_groups_by_directory(session, content_fixture,
                                            monkeypatch):
    """候选按目录分组、逐组调用 LLM：每组 prompt 含一个组对象 JSON，结果合并。

    spy 记录每次 llm.complete 的 prompt：断言调用次数 = 目录组数、每组只含
    本组标签、组 JSON 带目录名称/描述、prompts 首段都是渲染后的正文。
    批大小固定放大（50 > 组成员数），每组只发一批——本测聚焦分组语义，
    分批切分由 test_closed_produce_batches_within_group 专测。
    """
    _seed_library()
    monkeypatch.setattr(closed_module, "get_settings",
                        lambda: Settings(closed_batch_size=50))
    spec = _build_spec(session, content_fixture, run_id="r2")
    library = try_load_library()

    calls: list[str] = []

    class _SpyLLM(MOCK_LLM.__class__):
        def complete(self, messages, run_id):
            calls.append(messages[0]["content"])
            return super().complete(messages, run_id)

    out = ClosedTagProducer().produce(spec, session, _SpyLLM(), MOCK_EMBEDDER)
    assert out  # 合并各组结果非空

    # 召回候选的目录集合 = 分组组数 = LLM 调用数（批大小 50 → 每组一批）
    candidates = recall_candidates(
        library, MOCK_EMBEDDER, embed_content_texts(content_fixture),
        top_k=50)
    dir_ids = {c.dir_id for c in candidates}
    assert len(calls) == len(dir_ids) >= 2

    seen_tags: set[str] = set()
    for prompt in calls:
        body, _, tail = prompt.partition("\n\n## 候选标签分组\n")
        assert body == spec.prompt_text  # 渲染正文在前、逐字不变
        group = json.loads(tail)
        directory = library.directory(group["dir_id"])
        assert group["dir_name"] == directory.dir_name
        assert group["dir_description"] == directory.description
        member_ids = {t["tag_id"] for t in group["tags"]}
        assert member_ids == {c.tag_id for c in candidates
                              if c.dir_id == group["dir_id"]}
        for tag in group["tags"]:
            assert set(tag) == {"tag_id", "tag_name", "description"}
        seen_tags |= member_ids
    # 全部候选都恰好进了某一组
    assert seen_tags == {c.tag_id for c in candidates}


def test_closed_produce_batches_within_group(session, content_fixture,
                                             monkeypatch):
    """组内分批（v5）：批大小 2 下每目录按召回序连续切片、逐批调用。

    批次感知 scripted LLM：按 prompt 尾部组 JSON 的 tags 子集查表回放
    （_ScriptedLLM 按 dir_id 回放、不感知批次，无法验证切分）。断言：
    每目录调用数 = ceil(成员数/2)、各批目录信息一致且 tags 为连续切片、
    并集 = 组成员；批只切调用，组级合并（门控 A≥4 + tag_id 去重保最高
    分）语义不变。
    """
    _seed_library()
    monkeypatch.setattr(closed_module, "get_settings",
                        lambda: Settings(closed_batch_size=2))
    spec = _build_spec(session, content_fixture, run_id="r5",
                       policy=GenerationPolicy(max_tags=2))
    # dir_genre 3 条 → 2 批；dir_mood 3 条 → 2 批。tag_001/tag_005 的
    # Sapp/Sevi 拉满（A=5,E=5）组内最高；A=3 的 4 条被门控（A≥4）丢弃。
    # 全局 M=2：两组各出 1 条保底胜者恰填满 M。若错做成"逐批截断"或
    # 门控沿用 A≥3，3 条分 2 批、6 条全存活 → 输出多于 2 条，断言可分辨
    responses = {
        "tag_001": [{"tag_id": "tag_001", "applicability": 5,
                     "evidence": _evi(5, 5, 5)}],
        "tag_002": [{"tag_id": "tag_002", "applicability": 3,
                     "evidence": _evi(3, 3, 3)}],
        "tag_003": [{"tag_id": "tag_003", "applicability": 3,
                     "evidence": _evi(3, 3, 3)}],
        "tag_004": [{"tag_id": "tag_004", "applicability": 3,
                     "evidence": _evi(3, 3, 3)}],
        "tag_005": [{"tag_id": "tag_005", "applicability": 5,
                     "evidence": _evi(5, 5, 5)}],
        "tag_006": [{"tag_id": "tag_006", "applicability": 3,
                     "evidence": _evi(3, 3, 3)}],
    }

    class _BatchScriptedLLM:
        """按批内首个 tag_id 回放（批次感知）：记录 prompt 供切分断言。"""

        def __init__(self):
            self.calls: list[str] = []

        def complete(self, messages, run_id):
            prompt = messages[0]["content"]
            self.calls.append(prompt)
            tail = prompt.partition("\n\n## 候选标签分组\n")[2]
            group = json.loads(tail)
            items = [dict(row) for tag in group["tags"]
                     for row in responses[tag["tag_id"]]]
            return LLMResponse(content=json.dumps(items, ensure_ascii=False),
                               usage={}, raw={}, ok=True)

    llm = _BatchScriptedLLM()
    out = ClosedTagProducer().produce(spec, session, llm, MOCK_EMBEDDER)
    library = try_load_library()
    candidates = recall_candidates(
        library, MOCK_EMBEDDER, embed_content_texts(content_fixture),
        top_k=50)
    group_order = list(dict.fromkeys(c.dir_id for c in candidates))

    # 每目录调用数 = ceil(成员数/2)，全局总调用数 = 各目录批数之和
    calls_by_dir: dict[str, list[dict]] = {d: [] for d in group_order}
    for prompt in llm.calls:
        body, _, tail = prompt.partition("\n\n## 候选标签分组\n")
        assert body == spec.prompt_text
        group = json.loads(tail)
        directory = library.directory(group["dir_id"])
        assert group["dir_name"] == directory.dir_name
        assert group["dir_description"] == directory.description
        assert len(group["tags"]) <= 2
        calls_by_dir[group["dir_id"]].append(group)
    for dir_id in group_order:
        members = [c for c in candidates if c.dir_id == dir_id]
        batches = calls_by_dir[dir_id]
        assert len(batches) == -(-len(members) // 2)  # ceil
        # 各批 tags 为召回序连续切片，并集恰为组成员
        sliced = [t["tag_id"] for b in batches for t in b["tags"]]
        assert sliced == [c.tag_id for c in members]
        for batch in batches:
            for tag in batch["tags"]:
                assert set(tag) == {"tag_id", "tag_name", "description"}

    # 合并后门控：两组各留全场 Sf 最高的 tag_001/tag_005（跨批竞争胜出，
    # 证明门控与去重发生在组级合并之后而非逐批）；期望值用镜像召回 Semb 现算
    semb = {c.tag_id: c.score for c in candidates}
    for tid in ("tag_001", "tag_005"):
        expected_sf = 0.2 * semb[tid] + 0.5 * 1.0 + 0.3 * 1.0
        row = next(t for t in out if t.tag_id == tid)
        assert row.relevance_score == pytest.approx(expected_sf)
    assert [t.tag_id for t in out] == ["tag_001", "tag_005"]


def test_closed_produce_scores_and_selects_globally(session, content_fixture):
    """终分 Sf 全局降序、max_tags=1 时组保底间按 Sf 竞争截断。

    期望值不手算 mock 余弦：用与 produce 同口径的镜像召回拿 Semb，按
    Sf = 0.2·Semb + 0.5·Sapp + 0.3·Sevi 现算（mock 确定性）。
    """
    _seed_library()
    spec = _build_spec(session, content_fixture, run_id="r3",
                       policy=GenerationPolicy(max_tags=1))
    responses = {
        "dir_genre": [
            {"tag_id": "tag_001", "applicability": 3,
             "evidence": _evi(5, 5, 5)},
            {"tag_id": "tag_002", "applicability": 4,
             "evidence": _evi(4, 4, 4)},
            {"tag_id": "tag_003", "applicability": 5,
             "evidence": _evi(2, 2, 2)},
        ],
        "dir_mood": [
            {"tag_id": "tag_004", "applicability": 4,
             "evidence": _evi(3, 3, 3)},
            {"tag_id": "tag_005", "applicability": 5,
             "evidence": _evi(4, 4, 4, [{
                 "scene_id": content_fixture.content.scenes[0].scene_id,
                 "evidence": "场景摘要",
             }])},
        ],
    }

    out = ClosedTagProducer().produce(spec, session,
                                      _ScriptedLLM(responses), MOCK_EMBEDDER)
    # 镜像召回：与 produce 同参数，拿每个候选的 Semb（mock 确定性）
    candidates = recall_candidates(
        try_load_library(), MOCK_EMBEDDER,
        embed_content_texts(content_fixture), top_k=50)
    semb = {c.tag_id: c.score for c in candidates}

    def _sf(item: dict) -> float:
        # v8：Sevi 为三模态融合分（默认权重 0.3/0.3/0.4）
        sev = (0.3 * (item["evidence"]["content"]["evidence_strength"] - 1) / 4
               + 0.3 * (item["evidence"]["scene"]["evidence_strength"] - 1) / 4
               + 0.4 * (item["evidence"]["frame"]["evidence_strength"] - 1) / 4)
        return 0.2 * semb[item["tag_id"]] \
            + 0.5 * (item["applicability"] - 1) / 4 + 0.3 * sev

    # 门控 A≥4：tag_001（A=3）整条丢弃；两组各剩 tag_002/tag_003 与
    # tag_004/tag_005，全局 M=1 → 两组保底按 Sf 竞争，只留最高 1 条
    gated = [it for group in responses.values()
             for it in group if it["applicability"] >= 4]
    winner = max(gated, key=_sf)
    assert [t.tag_id for t in out] == [winner["tag_id"]]
    assert len(out) == 1
    # Sf 与标签成对（relevance_score）；confidence 为三模态融合 Sevi
    assert out[0].relevance_score == pytest.approx(_sf(winner))
    assert out[0].confidence == pytest.approx(
        0.3 * (winner["evidence"]["content"]["evidence_strength"] - 1) / 4
        + 0.3 * (winner["evidence"]["scene"]["evidence_strength"] - 1) / 4
        + 0.4 * (winner["evidence"]["frame"]["evidence_strength"] - 1) / 4)
    # Sf 理论值域 [0.375, 1]：A=4 保底 0.5·0.75
    assert 0.375 <= out[0].relevance_score <= 1.0
    # 带场景 evidence 的判定完整落行（解析契约：scene_id → 场景时间轴展开）
    if winner["tag_id"] == "tag_005":
        scene = content_fixture.content.scenes[0]
        assert out[0].evidence == [Evidence(
            scene_id=scene.scene_id,
            start_ms=scene.time_range.start_ms,
            end_ms=scene.time_range.end_ms,
            description="场景摘要",
        )] * 3  # _evi(entries) 同条目进三模态节 → 展平后 3 条


def test_closed_produce_global_selection_rules(session, content_fixture):
    """全局选择规则：每组保底 1 条 + 剩余按 Sf 降序补足 M−N。"""
    _seed_library()
    # 设计：高分组（dir_genre）3 条全过门控、低分组（dir_mood）只有 1 条
    # 过门控作组代表。M=3：两组代表保底（每组最高分），第三条从剩余 2 条
    # 中取全局 Sf 最高——即使它来自高分组、低分组的组内次高不顶替它。
    spec = _build_spec(session, content_fixture, run_id="r5",
                       policy=GenerationPolicy(max_tags=3))
    responses = {
        "dir_genre": [
            {"tag_id": "tag_001", "applicability": 5,
             "evidence": _evi(5, 5, 5)},  # 全场最高
            {"tag_id": "tag_002", "applicability": 5,
             "evidence": _evi(5, 5, 5)},  # 与 001 同 API 分，
            #  胜负取决于 Semb——不做假设，断言从镜像召回现算
            {"tag_id": "tag_003", "applicability": 4,
             "evidence": _evi(4, 4, 4)},  # 组内垫底
        ],
        "dir_mood": [
            # 组代表候选 tag_004：A=4/三模态 4，但 dir_mood 只有它过门控，
            #  即使全局 Sf 低于 dir_genre 次高，组保底也必须保它
            {"tag_id": "tag_004", "applicability": 4,
             "evidence": _evi(4, 4, 4)},
            {"tag_id": "tag_005", "applicability": 3,
             "evidence": _evi(5, 5, 5)},  # 门控外：丢弃
        ],
    }
    out = ClosedTagProducer().produce(spec, session,
                                      _ScriptedLLM(responses), MOCK_EMBEDDER)
    candidates = recall_candidates(
        try_load_library(), MOCK_EMBEDDER,
        embed_content_texts(content_fixture), top_k=50)
    semb = {c.tag_id: c.score for c in candidates}

    def _sf(item: dict) -> float:
        sev = (0.3 * (item["evidence"]["content"]["evidence_strength"] - 1) / 4
               + 0.3 * (item["evidence"]["scene"]["evidence_strength"] - 1) / 4
               + 0.4 * (item["evidence"]["frame"]["evidence_strength"] - 1) / 4)
        return (0.2 * semb[item["tag_id"]]
                + 0.5 * (item["applicability"] - 1) / 4 + 0.3 * sev)

    gated = [it for g in responses.values() for it in g
             if it["applicability"] >= 4]
    assert len(out) == 3
    # 低分组代表 tag_004 必在：组保底不受高分组次高挤压
    assert {t.tag_id for t in out} == {"tag_001", "tag_002", "tag_004"}
    # 另外两条 = dir_genre 的组最高与次高（本场景恰为组内 Sf 前二）
    genre_top2 = sorted(
        (it for it in responses["dir_genre"] if it["applicability"] >= 4),
        key=_sf, reverse=True)[:2]
    assert {t.tag_id for t in out} == {it["tag_id"] for it in genre_top2} | {"tag_004"}
    # 输出整体按 Sf 降序（入库行序 = 相关度降序口径）
    scores = [t.relevance_score for t in out]
    assert scores == sorted(scores, reverse=True)

    def _sevi(item: dict) -> float:
        return (0.3 * (item["evidence"]["content"]["evidence_strength"] - 1) / 4
                + 0.3 * (item["evidence"]["scene"]["evidence_strength"] - 1) / 4
                + 0.4 * (item["evidence"]["frame"]["evidence_strength"] - 1) / 4)

    for row in out:
        src = next(it for it in gated if it["tag_id"] == row.tag_id)
        assert row.relevance_score == pytest.approx(_sf(src))
        # confidence = 三模态融合 Sevi（与 Sf 里的 Sevi 项同口径）
        assert row.confidence == pytest.approx(_sevi(src))


def test_closed_produce_groups_compete_when_exceeding_max(
        session, content_fixture):
    """N>M：组保底间按 Sf 竞争截断，严格不超 M，且不再补入。"""
    _seed_library()
    spec = _build_spec(session, content_fixture, run_id="r6",
                       policy=GenerationPolicy(max_tags=1))
    responses = {
        "dir_genre": [{"tag_id": "tag_003", "applicability": 5,
                       "evidence": _evi(1, 1, 1)}],
        "dir_mood": [{"tag_id": "tag_004", "applicability": 5,
                      "evidence": _evi(5, 5, 5)}],
    }
    out = ClosedTagProducer().produce(spec, session,
                                      _ScriptedLLM(responses), MOCK_EMBEDDER)
    # tag_004 的 Sf = 0.2·Semb + 0.5 + 0.3 ≥ 0.8；tag_003 的 Sevi=0，
    # Sf 上界 0.2 + 0.5 + 0 = 0.7 < 0.8 → 保底竞争 tag_004 胜出
    assert [t.tag_id for t in out] == ["tag_004"]
    assert len(out) == 1  # 严格 ≤ M


def test_closed_produce_fabricated_id_filtered_before_scoring(
        session, content_fixture):
    """编造 tag_id 在打分前剔除：不占组保底名额，次高真实标签顶替入选。"""
    _seed_library()
    spec = _build_spec(session, content_fixture, run_id="r7",
                       policy=GenerationPolicy(max_tags=10))
    responses = {
        "dir_genre": [
            # 编造 id 给满分：若参与选择会抢走本组唯一保底名额
            {"tag_id": "tag_fake", "applicability": 5,
             "evidence": _evi(5, 5, 5)},
            {"tag_id": "tag_001", "applicability": 4,
             "evidence": _evi(4, 4, 4)},
        ],
        "dir_mood": [
            # 越组引用（tag_001 属 dir_genre）：同样前置剔除，不重复入选
            {"tag_id": "tag_001", "applicability": 5,
             "evidence": _evi(5, 5, 5)},
            {"tag_id": "tag_004", "applicability": 4,
             "evidence": _evi(4, 4, 4)},
        ],
    }
    out = ClosedTagProducer().produce(spec, session,
                                      _ScriptedLLM(responses), MOCK_EMBEDDER)
    # tag_fake 丢弃；dir_genre 保底 = tag_001；dir_mood 的越组 tag_001 被剔，
    # 保底 = tag_004 → 全局 {tag_001, tag_004}，tag_001 只出现一次
    assert {t.tag_id for t in out} == {"tag_001", "tag_004"}
    assert [t.tag_id for t in out].count("tag_001") == 1


def test_closed_produce_parse_retry_recovers(
        session, content_fixture, caplog, monkeypatch):
    """v7 组级解析重试：某组首次返回缺键元素 → 记 warning 后带提示重调
    本组一次，重试返回合法 → 任务不失败，该组正常贡献，其他组零重调。

    批大小 50（整组一批）：断言的是"组级"重试次数；默认批大小 1 下每
    标签一批，调用计数会变成批数×重试，无法验证组级语义。"""
    import logging

    _seed_library()
    monkeypatch.setattr(closed_module, "get_settings",
                        lambda: Settings(closed_batch_size=50))
    spec = _build_spec(session, content_fixture, run_id="r11",
                       policy=GenerationPolicy(max_tags=10))
    good = {
        "dir_genre": [
            {"tag_id": "tag_001", "applicability": 4,
             "evidence": _evi(4, 4, 4)}],
        "dir_mood": [
            {"tag_id": "tag_004", "applicability": 4,
             "evidence": _evi(4, 4, 4)}],
    }

    class _FlakyLLM(_ScriptedLLM):
        """dir_genre 首次返回缺 applicability 的坏元素，重调返回合法；
        dir_mood 始终合法（用调用计数区分，与 run_id 无关）。"""

        def __init__(self):
            super().__init__(good)
            self.calls: dict[str, int] = {}
            self.retry_prompt: str | None = None

        def complete(self, messages, run_id):
            tail = messages[0]["content"].partition(
                "\n\n## 候选标签分组\n")[2]
            # 重试 prompt 在组 JSON 后追加 hint：raw_decode 只解析前缀
            group = json.JSONDecoder().raw_decode(tail)[0]
            dir_id = group["dir_id"]
            self.calls[dir_id] = self.calls.get(dir_id, 0) + 1
            if dir_id == "dir_genre" and self.calls[dir_id] == 1:
                # 复现线上失败形态：缺 applicability 键（合法 JSON 非截断）
                return LLMResponse(
                    content=json.dumps(
                        [{"tag_id": "tag_001",
                          "evidence": _evi(5, 5, 5)}],
                        ensure_ascii=False),
                    usage={}, raw={}, ok=True)
            if "无法解析" in messages[0]["content"]:
                self.retry_prompt = messages[0]["content"]
            return super().complete(messages, run_id)

    llm = _FlakyLLM()
    with caplog.at_level(logging.WARNING,
                         logger="tagplatform.services.generation.closed.producer"):
        out = ClosedTagProducer().produce(spec, session,
                                          llm, MOCK_EMBEDDER)
    # 重试后该组正常贡献：两组标签都在，任务未失败
    assert {t.tag_id for t in out} == {"tag_001", "tag_004"}
    # 坏组恰好调了 2 次（1 次失败 + 1 次重试），好组 1 次（零重调）
    assert llm.calls == {"dir_genre": 2, "dir_mood": 1}
    # 首次解析失败有 warning（含 dir_id 与错误语义）
    assert any("解析失败" in r.message and "dir_genre" in r.message
               for r in caplog.records)
    # 重试 prompt = 原 prompt + 格式纠正提示（组对象 JSON 仍在可解析前缀）
    assert llm.retry_prompt is not None
    tail = llm.retry_prompt.partition("\n\n## 候选标签分组\n")[2]
    group = json.JSONDecoder().raw_decode(tail)[0]
    assert group["dir_id"] == "dir_genre"


def test_closed_produce_parse_retry_exhaustion_raises(
        session, content_fixture, monkeypatch):
    """v7 重试耗尽：连续两次坏输出 → 抛 LLMParseError，消息含 dir_id 与
    最后一次原始输出摘录（任务 error 字段可定位是哪个组、什么输出）。

    批大小 50（整组一批）：默认 1 下每个单标签批都会坏、都会重试，调用
    计数与错误消息的组语义断言无法成立。"""
    _seed_library()
    monkeypatch.setattr(closed_module, "get_settings",
                        lambda: Settings(closed_batch_size=50))
    spec = _build_spec(session, content_fixture, run_id="r12",
                       policy=GenerationPolicy(max_tags=10))
    bad = [{"tag_id": "tag_001", "evidence": _evi(5, 5, 5)}]

    class _AlwaysBadLLM(_ScriptedLLM):
        def __init__(self):
            super().__init__({})
            self.calls: dict[str, int] = {}

        def complete(self, messages, run_id):
            tail = messages[0]["content"].partition(
                "\n\n## 候选标签分组\n")[2]
            group = json.JSONDecoder().raw_decode(tail)[0]
            dir_id = group["dir_id"]
            self.calls[dir_id] = self.calls.get(dir_id, 0) + 1
            return LLMResponse(
                content=json.dumps(bad, ensure_ascii=False),
                usage={}, raw={}, ok=True)

    llm = _AlwaysBadLLM()
    with pytest.raises(LLMParseError, match="dir_genre") as excinfo:
        ClosedTagProducer().produce(spec, session, llm, MOCK_EMBEDDER)
    # 坏组恰好重试了一次（2 次调用）后放弃
    assert llm.calls["dir_genre"] == 2
    # 错误消息含最后一次原始输出摘录（坏元素文本在内，可对账定位）
    assert "重试后仍解析失败" in str(excinfo.value)
    assert "evidence_strength" in str(excinfo.value)


def _seed_scoped_library() -> None:
    """三目录库：dir_genre/dir_mood（content）+ dir_char（character）。"""
    _seed_library(extra_dirs=[{
        "dir_id": "dir_char", "parent_id": None, "level": 1,
        "dir_name": "角色标签", "dir_type": "dir_dynamic",
        "applicable_scope": "character", "description": "角色原型与人设",
        "aliases": []}], extra_tags=[
        ("tag_101", "英雄", "dir_char", "承担主线使命的正派角色")])


def test_closed_produce_unmountable_dropped_before_selection(
        session, content_fixture, caplog):
    """v6 前置修复：挂载失败的判定在全局选择**前**淘汰，不占 M 名额。

    复现线上"选满 30 只出 28"：角色目录标签在多角色内容上解析不出唯一
    挂载点。M=2 时若仍按老流程（先选后排挂载），tag_101 会以组头身份占走
    一个名额再被丢弃，输出只剩 1 条；前置后由可挂载的组头顶替，输出 2 条。
    分值设计让胜负与 mock Semb 无关：A5/E5 组 Sf ∈ [0.6, 1.0]、A4/E1 组
    ∈ [0.175, 0.575]，区间不重叠，结果确定。
    """
    import logging

    _seed_scoped_library()
    # 多角色场景：character scope 定位不到唯一角色（挂载必败）
    for scene in content_fixture.content.scenes:
        scene.character_ids = ["char_a", "char_b"]
    spec = _build_spec(session, content_fixture, run_id="r9",
                       policy=GenerationPolicy(max_tags=2))
    responses = {
        # 角色目录组头：全局最高分，老流程必占 M 名额后被丢弃
        "dir_char": [
            {"tag_id": "tag_101", "applicability": 5,
             "evidence": _evi(5, 5, 5, [{"scene_id": "scene_001",
                                         "evidence": "主角孤胆"}])}],
        "dir_genre": [
            {"tag_id": "tag_001", "applicability": 5,
             "evidence": _evi(5, 5, 5)}],
        "dir_mood": [
            {"tag_id": "tag_004", "applicability": 4,
             "evidence": _evi(1, 1, 1)}],
    }
    with caplog.at_level(logging.INFO, logger="tagplatform.services.generation.closed.producer"):
        out = ClosedTagProducer().produce(spec, session,
                                          _ScriptedLLM(responses), MOCK_EMBEDDER)
    # tag_101 挂载失败在前置阶段淘汰：M=2 名额由两个可挂载组头占满
    assert len(out) == 2
    assert {t.tag_id for t in out} == {"tag_001", "tag_004"}
    assert all(t.target_type == "content" for t in out)
    # 淘汰有 warning、选择完成有 INFO 汇总（两类丢弃各可追溯一条日志）
    assert any("解析不出挂载点" in r.message for r in caplog.records)
    assert any("闭环全局选择完成" in r.message for r in caplog.records)


def test_closed_produce_unmountable_scene_unknown_evidence_backfills(
        session, content_fixture):
    """v6 前置修复（scene 维度）：证据引用未知场景 → 挂载失败前置淘汰，
    M=1 名额回填给次高的可挂载组头（老流程会选它占位后再丢，输出 0 条）。"""
    _seed_library(extra_dirs=[{
        "dir_id": "dir_scene", "parent_id": None, "level": 1,
        "dir_name": "名场面", "dir_type": "dir_dynamic",
        "applicable_scope": "scene", "description": "具体情节事件",
        "aliases": []}], extra_tags=[
        ("tag_201", "剧情反转", "dir_scene", "关键时刻的情节反转")])
    spec = _build_spec(session, content_fixture, run_id="r10",
                       policy=GenerationPolicy(max_tags=1))
    responses = {
        # 全局最高分，但证据场景不存在 → 挂载不出 scene 挂载点
        "dir_scene": [
            {"tag_id": "tag_201", "applicability": 5,
             "evidence": _evi(5, 5, 5, [{"scene_id": "scene_999",
                                         "evidence": "不存在的场景"}])}],
        "dir_genre": [
            {"tag_id": "tag_001", "applicability": 4,
             "evidence": _evi(1, 1, 1)}],
        "dir_mood": [],
    }
    out = ClosedTagProducer().produce(spec, session,
                                      _ScriptedLLM(responses), MOCK_EMBEDDER)
    # tag_201 前置淘汰，M=1 回填给 tag_001（老流程：tag_201 占位后丢弃 → 空）
    assert [t.tag_id for t in out] == ["tag_001"]
    assert out[0].target_type == "content"


def test_closed_produce_gate_drops_applicability_below_four(
        session, content_fixture):
    """门控 G：Sapp<0.75（A≤3）→ G=0 整条丢弃；A=4 恰在门线上保留。"""
    _seed_library()
    spec = _build_spec(session, content_fixture, run_id="r4",
                       policy=GenerationPolicy(max_tags=10))
    responses = {
        "dir_genre": [
            {"tag_id": "tag_001", "applicability": 4,
             "evidence": _evi(5, 5, 5)},
            {"tag_id": "tag_002", "applicability": 3,
             "evidence": _evi(5, 5, 5)},
        ],
        "dir_mood": [
            {"tag_id": "tag_004", "applicability": 5,
             "evidence": _evi(5, 5, 5)},
            {"tag_id": "tag_005", "applicability": 1,
             "evidence": _evi(5, 5, 5)},
        ],
    }
    out = ClosedTagProducer().produce(spec, session,
                                      _ScriptedLLM(responses), MOCK_EMBEDDER)
    # max_tags=10 富余：A∈{1,3} 被丢只能因门控，A=4 恰好放行
    assert {t.tag_id for t in out} == {"tag_001", "tag_004"}
    semb = {c.tag_id: c.score for c in recall_candidates(
        try_load_library(), MOCK_EMBEDDER,
        embed_content_texts(content_fixture), top_k=50)}
    by_tag = {t.tag_id: t for t in out}
    # 权重算术显式展开：Sf = 0.2·Semb + 0.5·((4−1)/4) + 0.3·Sevi_fused，
    # Sevi_fused = 0.3·1 + 0.3·1 + 0.4·1（三模态全 5 分 → 融合 1.0）
    assert by_tag["tag_001"].relevance_score == pytest.approx(
        0.2 * semb["tag_001"] + 0.5 * 0.75 + 0.3 * 1.0)
    assert by_tag["tag_001"].confidence == pytest.approx(1.0)


def test_closed_produce_dedups_repeated_judgments(session, content_fixture):
    """同组重复判定同一标签：只保留最高分一条，不产生重复行。"""
    _seed_library()
    spec = _build_spec(session, content_fixture, run_id="r8",
                       policy=GenerationPolicy(max_tags=10))
    responses = {
        "dir_genre": [
            {"tag_id": "tag_001", "applicability": 4,
             "evidence": _evi(4, 4, 4)},
            # 同标签重复判定、分数更低 → 被去重
            {"tag_id": "tag_001", "applicability": 4,
             "evidence": _evi(2, 2, 2)},
        ],
        "dir_mood": [
            {"tag_id": "tag_004", "applicability": 4,
             "evidence": _evi(4, 4, 4)},
            # 同标签重复判定、分数更高 → 覆盖前一条
            {"tag_id": "tag_004", "applicability": 5,
             "evidence": _evi(5, 5, 5)},
        ],
    }
    out = ClosedTagProducer().produce(spec, session,
                                      _ScriptedLLM(responses), MOCK_EMBEDDER)
    assert [t.tag_id for t in out].count("tag_001") == 1
    assert [t.tag_id for t in out].count("tag_004") == 1
    candidates = recall_candidates(
        try_load_library(), MOCK_EMBEDDER,
        embed_content_texts(content_fixture), top_k=50)
    semb = {c.tag_id: c.score for c in candidates}
    by_tag = {t.tag_id: t for t in out}
    # tag_004 保留的是高分判定：Sf 用 A=5/三模态全 5 现算（Sevi=1.0）
    assert by_tag["tag_004"].relevance_score == pytest.approx(
        0.2 * semb["tag_004"] + 0.5 * 1.0 + 0.3 * 1.0)
    assert by_tag["tag_004"].confidence == pytest.approx(1.0)


def test_closed_no_candidates_raises(session, content_fixture):
    """空库：召回零候选 → 显式 ValueError（先导库再生成），不调 LLM。"""
    spec = GenerationSpec(task_id="t1", asset_id=content_fixture.asset.asset_id,
                          content=content_fixture, policy=GenerationPolicy(),
                          prompt_text="", run_id="r1",
                          library=TagLibrary([], []))
    with pytest.raises(ValueError, match="候选标签为空"):
        ClosedTagProducer().produce(spec, session, MOCK_LLM, MOCK_EMBEDDER)


def test_closed_produce_skips_dir_asset(session, content_fixture, monkeypatch):
    """dir_asset 目录的候选召回后剔除：不建组、不调 LLM、不出现在产出。

    库里三个目录（静态 + 动态 + asset），asset 目录挂两条会被召回的标签
    （镜像召回先证明确实召回了它们），produce 后断言：LLM 调用次数 =
    非 asset 目录组数、没有任何 prompt 提到 asset 目录或其标签、产出里
    没有 asset 标签（即便 ScriptedLLM 想返回它也不行——组都不存在）。
    """
    _seed_library_with_asset()
    # 批大小放大（50 > 组成员数）每组只发一批：本测聚焦 asset 过滤语义，
    # 调用数 = 组数；分批切分由 test_closed_produce_batches_within_group 专测
    monkeypatch.setattr(closed_module, "get_settings",
                        lambda: Settings(closed_batch_size=50))
    spec = _build_spec(session, content_fixture, run_id="r2")
    library = try_load_library()

    # 前置：asset 目录的两条标签确实在召回范围内（过滤发生在 produce 内）
    recalled = recall_candidates(
        library, MOCK_EMBEDDER, embed_content_texts(content_fixture), top_k=50)
    assert {c.tag_id for c in recalled} >= {"tag_101", "tag_102"}
    asset_ids = {c.tag_id for c in recalled if c.dir_id == "dir_actor"}
    assert asset_ids == {"tag_101", "tag_102"}

    calls: list[str] = []

    class _SpyLLM(MOCK_LLM.__class__):
        def complete(self, messages, run_id):
            calls.append(messages[0]["content"])
            return super().complete(messages, run_id)

    out = ClosedTagProducer().produce(spec, session, _SpyLLM(), MOCK_EMBEDDER)

    # 只为非 asset 目录建组调 LLM（静态 + 动态 = 2 组）
    assert len(calls) == 2
    group_dirs = {json.loads(
        p.partition("\n\n## 候选标签分组\n")[2])["dir_id"] for p in calls}
    assert group_dirs == {"dir_genre", "dir_mood"}
    # 任何 prompt 都不提 asset 目录与其标签
    for prompt in calls:
        assert "dir_actor" not in prompt
        assert "张三" not in prompt and "李四" not in prompt
    # 产出不含 asset 标签
    assert {t.tag_id for t in out} <= {tid for tid, *_ in _LIB_TAGS}
    assert all(t.dir_id != "dir_actor" for t in out)


def test_closed_all_dir_asset_raises(session, content_fixture):
    """范围内全是 dir_asset 目录：过滤后候选为空 → 复用空候选显式失败。"""
    write_library(library_root(),
                  dirs=[dict(_ASSET_DIR)],
                  tags=list(_ASSET_TAGS))
    spec = _build_spec(session, content_fixture, run_id="r2",
                       policy=GenerationPolicy(dir_ids=["dir_actor"]))
    with pytest.raises(ValueError, match="候选标签为空"):
        ClosedTagProducer().produce(spec, session, MOCK_LLM, MOCK_EMBEDDER)


def test_closed_without_library_raises(session, content_fixture):
    spec = GenerationSpec(task_id="t1", asset_id=content_fixture.asset.asset_id,
                          content=content_fixture, policy=GenerationPolicy(),
                          prompt_text="", run_id="r1", library=None)
    with pytest.raises(ValueError, match="标签库未就绪"):
        ClosedTagProducer().produce(spec, session, MOCK_LLM, MOCK_EMBEDDER)


# ---- parse_closed_output：模式 A 契约校验（v8 嵌套三模态 evidence） ----


def _ev_dict(ec: int, es: int, ef: int,
             entries: list[dict] | None = None) -> dict:
    """与 _evi 同构的显式字面量（解析层测试不经过 _evi，避免同源假绿）。"""
    entries = entries or []
    return {
        "content": {"evidence_strength": ec, "evidences": list(entries)},
        "scene": {"evidence_strength": es, "evidences": list(entries)},
        "frame": {"evidence_strength": ef, "evidences": list(entries)},
    }


def test_parse_closed_bad_structure():
    with pytest.raises(LLMParseError):
        parse_closed_output('{"not": "a list"}')
    with pytest.raises(LLMParseError):
        parse_closed_output('[{"tag_id": "x"}]')  # 缺键
    with pytest.raises(LLMParseError):
        parse_closed_output(json.dumps([
            {"tag_id": "x", "applicability": 6,
             "evidence": _ev_dict(4, 4, 4)}]))  # applicability 越界
    with pytest.raises(LLMParseError):
        parse_closed_output(json.dumps([
            {"tag_id": "x", "applicability": 4.5,
             "evidence": _ev_dict(4, 4, 4)}]))  # 非整数
    with pytest.raises(LLMParseError):
        parse_closed_output(json.dumps([
            {"tag_id": "x", "applicability": 4,
             "evidence": _ev_dict(4, 6, 4)}]))  # 模态分越界
    with pytest.raises(LLMParseError):
        parse_closed_output(json.dumps([
            {"tag_id": "x", "applicability": 4,
             "evidence": {"content": {"evidence_strength": 4,
                                      "evidences": []},
                          # 缺 frame 模态节
                          "scene": {"evidence_strength": 4,
                                    "evidences": []}}}]))
    with pytest.raises(LLMParseError):
        parse_closed_output(json.dumps([
            {"tag_id": "x", "applicability": 4,
             "evidence": _ev_dict(4, 4, 4, [
                 {"scene_id": "s1"}])}]))  # 证据条目缺 evidence 键
    with pytest.raises(LLMParseError):
        parse_closed_output("not json at all")  # 非 JSON 文本


def test_parse_closed_happy_path():
    items = parse_closed_output(json.dumps([
        {"tag_id": "tag_001", "applicability": 5,
         "evidence": _ev_dict(4, 4, 4, [{"scene_id": "s1",
                                         "evidence": "开场悬念"}])},
        # 边界值 [1,5] 双侧含端点
        {"tag_id": "tag_002", "applicability": 1,
         "evidence": _ev_dict(1, 1, 1)},
    ], ensure_ascii=False))
    assert items[0]["tag_id"] == "tag_001"
    assert (items[0]["evidence"]["content"]["evidences"][0]["scene_id"]
            == "s1")
    assert items[1]["applicability"] == 1
    assert items[1]["evidence"]["frame"]["evidence_strength"] == 1


def test_parse_closed_strips_markdown_code_fence():
    # 生产实测（2026-11-20 e2e，Qwen3.8-27B temperature=0.7）：模型会把 JSON
    # 包进 ```json 围栏，裸 json.loads 在首字符反引号上抛
    # "Expecting value: line 1 column 1 (char 0)" → 任务失败。
    # 围栏出现与否随采样波动（同 payload 上一次裸 JSON、这一次带围栏）。
    body = json.dumps([
        {"tag_id": "tag_001", "applicability": 5,
         "evidence": _ev_dict(4, 4, 4, [{"scene_id": "s1",
                                         "evidence": "开场悬念"}])},
    ], ensure_ascii=False)
    fenced = f"```json\n{body}\n```"
    assert parse_closed_output(fenced)[0]["tag_id"] == "tag_001"
    # 无语言标注的裸围栏同样要剥
    bare = f"```\n{body}\n```"
    assert parse_closed_output(bare)[0]["tag_id"] == "tag_001"
    # 围栏前后有模型补充的空白也要容忍
    padded = f"\n{fenced}\n"
    assert parse_closed_output(padded)[0]["tag_id"] == "tag_001"


def test_parse_closed_drops_bare_string_evidence_entries():
    # 728 批次全量重放实锤（2026-11-20，147.3 vLLM 温度 0）：725 过 2 挂，
    # 唯一 parse 失败模式 = 模型在 evidences 数组里插裸 scene_id 字符串
    # （紧跟一条已描述过同场景的对象，重复引用的畸形产物）。旧契约整条
    # 抛错 → 21 分钟的 728 调用任务全军覆没。非对象证据项降级为丢弃，
    # 与 runner 对未知 scene_id 证据的忽略语义一致。
    items = parse_closed_output(json.dumps([
        {"tag_id": "452", "applicability": 5,
         "evidence": _ev_dict(5, 5, 5, [
             {"scene_id": "scene_001", "evidence": "黑白转彩色"},
             "scene_002",
             {"scene_id": "scene_002", "evidence": "魔幻国度"},
         ])},
    ], ensure_ascii=False))
    assert items[0]["tag_id"] == "452"
    # 畸形裸字符串在三个模态节里各丢一条，每节留 2 条
    for modality in ("content", "scene", "frame"):
        assert len(items[0]["evidence"][modality]["evidences"]) == 2
        assert (items[0]["evidence"][modality]["evidences"][1]["scene_id"]
                == "scene_002")
    # 全部证据都被丢弃时项仍合法（evidences 空数组本就是合法契约）
    items2 = parse_closed_output(json.dumps([
        {"tag_id": "709", "applicability": 1,
         "evidence": _ev_dict(5, 5, 5, ["scene_001", "scene_002"])},
    ], ensure_ascii=False))
    assert all(items2[0]["evidence"][m]["evidences"] == []
               for m in ("content", "scene", "frame"))


def test_parse_closed_evidence_key_singular_alias():
    # 闭环评测首跑实锤（2026-11-20，147.3 vLLM 温度 0 + no-think）：模型
    # 偶发把模态节的证据数组键写成单数 evidence（与内层证据条目键
    # evidence 同名，一字之差），报「缺少键: ['evidences']」。值为列表
    # 时按别名归一为 evidences（v8：顶层单数 evidence 是嵌套对象本身）。
    items = parse_closed_output(json.dumps([
        {"tag_id": "619", "applicability": 2,
         "evidence": {
             "content": {"evidence_strength": 4,
                         "evidence": [{"scene_id": "scene_054",
                                       "evidence": "马头威胁"}]},
             "scene": {"evidence_strength": 4,
                       "evidence": [{"scene_id": "scene_148",
                                     "evidence": "收费站伏击"}]},
             "frame": {"evidence_strength": 4,
                       "evidence": [{"scene_id": "scene_054",
                                     "evidence": "关键帧佐证"}]},
         }},
    ], ensure_ascii=False))
    assert items[0]["tag_id"] == "619"
    for modality, sid in (("content", "scene_054"), ("scene", "scene_148"),
                          ("frame", "scene_054")):
        assert (items[0]["evidence"][modality]["evidences"][0]["scene_id"]
                == sid)
    # 顶层单对象（server.log 实锤的完整生产形态）：单元素直返兜底
    single = parse_closed_output(json.dumps(
        {"tag_id": "553", "applicability": 4,
         "evidence": _ev_dict(3, 3, 3, [{"scene_id": "scene_010",
                                         "evidence": "压迫感"}])},
        ensure_ascii=False))
    assert single[0]["tag_id"] == "553"
    assert (single[0]["evidence"]["content"]["evidences"][0]["scene_id"]
            == "scene_010")
    # 模态节内两键并存时 evidences 优先，别名值不覆盖
    both = parse_closed_output(json.dumps([
        {"tag_id": "9", "applicability": 3,
         "evidence": {
             "content": {"evidence_strength": 3,
                         "evidence": [{"scene_id": "scene_999",
                                       "evidence": "不应被采用"}],
                         "evidences": [{"scene_id": "scene_001",
                                        "evidence": "正主"}]},
             "scene": {"evidence_strength": 3, "evidences": []},
             "frame": {"evidence_strength": 3, "evidences": []},
         }},
    ], ensure_ascii=False))
    assert (both[0]["evidence"]["content"]["evidences"][0]["scene_id"]
            == "scene_001")


def test_parse_closed_evidence_alias_stays_strict_for_non_list():
    # 别名仅容忍「值为列表」的键名漂移；模态节内单数键值是字符串等非
    # 列表形态仍按缺 evidences 键严格拒绝，不给畸形数据开口子。
    with pytest.raises(LLMParseError, match="缺少键"):
        parse_closed_output(json.dumps([
            {"tag_id": "x", "applicability": 4,
             "evidence": {
                 "content": {"evidence_strength": 4,
                             "evidence": "一段文字"},
                 "scene": {"evidence_strength": 4, "evidences": []},
                 "frame": {"evidence_strength": 4, "evidences": []},
             }},
        ], ensure_ascii=False))
    # 别名路径下证据条目里的裸字符串沿用 PR#25 丢弃语义
    items = parse_closed_output(json.dumps([
        {"tag_id": "7", "applicability": 3,
         "evidence": {
             "content": {"evidence_strength": 3, "evidences": [
                 {"scene_id": "scene_001", "evidence": "实锤"}]},
             "scene": {"evidence_strength": 3, "evidence": [
                 "scene_001",
                 {"scene_id": "scene_002", "evidence": "实锤"}]},
             "frame": {"evidence_strength": 3, "evidences": []},
         }},
    ], ensure_ascii=False))
    assert (items[0]["evidence"]["scene"]["evidences"][0]["scene_id"]
            == "scene_002")


def test_parse_closed_error_message_has_position():
    bad = json.dumps([
        {"tag_id": "t", "applicability": 4,
         "evidence": _ev_dict(4, 4, 4)},
        {"tag_id": "x"},  # 第 1 个（0 起）元素缺键
    ], ensure_ascii=False)
    with pytest.raises(LLMParseError) as exc_info:
        parse_closed_output(bad)
    assert "第 1 个" in str(exc_info.value)


def test_parse_closed_accepts_wrapped_array():
    """模型把数组包进单键对象（{"tags": [...]}）时按该数组解析。

    2026-11 线上实测：deepseek 在 JSON 模式下偶发返回包装对象，直接判
    "不是数组"会让整个任务失败；对象里恰好一个列表值即可安全解包。
    """
    item = {"tag_id": "tag_001", "applicability": 4,
            "evidence": _ev_dict(4, 4, 4)}
    items = parse_closed_output(json.dumps({"tags": [item]}, ensure_ascii=False))
    assert items == [item]
    # 多个列表值无法判断哪个是结果 → 仍然报错（不猜）
    with pytest.raises(LLMParseError, match="列表值数量为 2"):
        parse_closed_output(json.dumps({"tags": [item], "warnings": []}))


def test_parse_closed_evidences_key_alias_dict_value():
    """旧顶层键名 evidences 挂新嵌套结构（值为 dict）→ 归一为 evidence。"""
    items = parse_closed_output(json.dumps([
        {"tag_id": "8", "applicability": 4,
         "evidences": _ev_dict(4, 4, 4, [{"scene_id": "scene_001",
                                          "evidence": "旧键名"}])},
    ], ensure_ascii=False))
    assert items[0]["tag_id"] == "8"
    assert (items[0]["evidence"]["frame"]["evidences"][0]["evidence"]
            == "旧键名")


def test_parse_closed_flat_format_rejected():
    """旧扁平四键输出（顶层 evidence_strength/evidences 列表）→ 严格
    报错触发重试：新契约下顶层必须有嵌套 evidence 对象；打分口径已变，
    静默放行旧格式会按缺键崩溃或算错融合 Sevi。"""
    with pytest.raises(LLMParseError, match="缺少键"):
        parse_closed_output(json.dumps([
            {"tag_id": "x", "applicability": 4,
             "evidence_strength": 4, "evidences": []},
        ], ensure_ascii=False))
