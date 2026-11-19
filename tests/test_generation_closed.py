"""模式 A 封闭式标签提取测试：库召回、输出解析与 ClosedTagProducer。

覆盖契约（v2：库来自 JSON）：
- embed_content_texts：[剧情摘要, 各场景描述] 查询文本列表；
- recall_candidates：按目录分组召回（组内跨 query 取最高分 top_k，跨组
  合并去重），排除 deprecate / 占位行，范围 dir_ids 生效，空库返回 []；
- parse_closed_output：键集与分值域校验、坏结构抛 LLMParseError；
- ClosedTagProducer：库缺失/空候选 → ValueError；终分 Sf = G·(0.2·Semb +
  0.5·Sapp + 0.3·Sevi)（门控 Sapp≥0.5，组内按 Sf 降序截断），Sf 与标签
  成对绑定（relevance_score）；结果是 catalog 行，带目录与挂载点（目录
  applicable_scope → content/scene/character）。
"""

import json

import pytest
from conftest import MOCK_EMBEDDER, MOCK_LLM
from sqlalchemy.orm import Session

from tagplatform.generation import Evidence, GenerationPolicy, GenerationSpec
from tagplatform.generation import closed as closed_module
from tagplatform.generation.closed import ClosedTagProducer
from tagplatform.generation.recall import embed_content_texts, recall_candidates
from tagplatform.library import TagLibrary, try_load_library
from tagplatform.llm.client import LLMResponse
from tagplatform.llm.parsing import LLMParseError, parse_closed_output
from tagplatform.prompts import get_active_prompt, render_prompt
from tagplatform.settings import Settings
from tagplatform.tasks.runner import _render_content_text
from tests.library_fixture import library_root, write_library

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
                  extra: list[dict] | None = None) -> None:
    """写一套干净的两目录库（dir_mood 动态、dir_genre 静态）。"""
    status = status or {}
    dirs = [
        {"dir_id": "dir_genre", "parent_id": None, "level": 1,
         "dir_name": "主类型", "dir_type": "dir_static",
         "applicable_scope": "content", "description": "故事类型", "aliases": []},
        {"dir_id": "dir_mood", "parent_id": None, "level": 1,
         "dir_name": "情绪基调", "dir_type": "dir_dynamic",
         "applicable_scope": "content",
         "description": "情绪与氛围", "aliases": []},
    ]
    tags = [{"tag_id": tag_id, "dir_id": dir_id, "tag_name": name,
             "tag_type": "val_fixed", "description": desc,
             "status": status.get(tag_id, "active")}
            for tag_id, name, dir_id, desc in _LIB_TAGS]
    write_library(library_root(), dirs=dirs,
                  tags=tags + list(extra or []))


# ---- embed_content_texts ----


def test_embed_content_texts(content_fixture):
    queries = embed_content_texts(content_fixture)
    assert queries[0] == content_fixture.content.summary
    assert queries[1:] == [s.description for s in content_fixture.content.scenes]


# ---- recall_candidates：按目录分组 / 去重 / 范围 ----


def test_closed_recall_groups_and_dedup():
    _seed_library()
    cats = recall_candidates(try_load_library(), MOCK_EMBEDDER, ["悬疑推理"],
                             top_k=2)
    # 每组 top-2 × 2 组 = 4，跨组按 tag_id 去重
    assert len({c.tag_id for c in cats}) == len(cats) == 4
    assert {c.dir_id for c in cats} == {"dir_genre", "dir_mood"}
    # 每条都带目录与挂载粒度（新词归属与 target_type 的依据）
    assert all(c.dir_id and c.scope == "content" for c in cats)


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


# ---- ClosedTagProducer：produce 六步流程 ----


def _build_spec(session: Session, content_fixture, run_id: str,
                policy=None) -> GenerationSpec:
    """按 runner 职责模拟组装：渲染 closed prompt（候选分组由 produce 注入）。"""
    library = try_load_library()
    policy = policy or GenerationPolicy()
    prompt_text = render_prompt(
        get_active_prompt(session, "closed"),
        content=_render_content_text(content_fixture),
    )
    return GenerationSpec(
        task_id="t1", asset_id=content_fixture.asset.asset_id,
        content=content_fixture, policy=policy,
        prompt_text=prompt_text, run_id=run_id, library=library,
    )


class _ScriptedLLM:
    """按组返回固定判定结果（从 prompt 尾部的组 JSON 取 dir_id）。"""

    def __init__(self, responses: dict[str, list[dict]]):
        self._responses = responses

    def complete(self, messages, run_id):
        tail = messages[0]["content"].partition(
            "\n\n## 候选标签分组\n")[2]
        dir_id = json.loads(tail)["dir_id"]
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
    并集 = 组成员；批只切调用，组级合并门控截断语义不变。
    """
    _seed_library()
    monkeypatch.setattr(closed_module, "get_settings",
                        lambda: Settings(closed_batch_size=2))
    spec = _build_spec(session, content_fixture, run_id="r5",
                       policy=GenerationPolicy(max_tags=1))
    # dir_genre 3 条 → 2 批；dir_mood 3 条 → 2 批。tag_001/tag_005 的 Sf
    # 组内最高（A=5,E=5 拉满）；6 条全过门控，正确实现组级合并后每目录
    # 只留 1 条胜者。若错做成"逐批截断"，3 条分 2 批、每批都有过门控的
    # 标签 → 每目录至少多漏 1 条，下面的输出断言即可分辨
    responses = {
        "tag_001": [{"tag_id": "tag_001", "applicability": 5,
                     "evidence_strength": 5, "evidences": []}],
        "tag_002": [{"tag_id": "tag_002", "applicability": 3,
                     "evidence_strength": 3, "evidences": []}],
        "tag_003": [{"tag_id": "tag_003", "applicability": 3,
                     "evidence_strength": 3, "evidences": []}],
        "tag_004": [{"tag_id": "tag_004", "applicability": 3,
                     "evidence_strength": 3, "evidences": []}],
        "tag_005": [{"tag_id": "tag_005", "applicability": 5,
                     "evidence_strength": 5, "evidences": []}],
        "tag_006": [{"tag_id": "tag_006", "applicability": 3,
                     "evidence_strength": 3, "evidences": []}],
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
    # 证明截断发生在组级合并之后而非逐批）；期望值用镜像召回 Semb 现算
    semb = {c.tag_id: c.score for c in candidates}
    for tid in ("tag_001", "tag_005"):
        expected_sf = 0.2 * semb[tid] + 0.5 * 1.0 + 0.3 * 1.0
        row = next(t for t in out if t.tag_id == tid)
        assert row.relevance_score == pytest.approx(expected_sf)
    assert [t.tag_id for t in out] == ["tag_001", "tag_005"]


def test_closed_produce_scores_and_caps_per_group(session, content_fixture):
    """终分 Sf 门内降序、组内截断 max_tags=1；Sf 与标签成对绑定。

    期望值不手算 mock 余弦：用与 produce 同口径的镜像召回拿 Semb，按
    Sf = 0.2·Semb + 0.5·Sapp + 0.3·Sevi 现算每组胜者（mock 确定性）。
    """
    _seed_library()
    spec = _build_spec(session, content_fixture, run_id="r3",
                       policy=GenerationPolicy(max_tags=1))
    responses = {
        "dir_genre": [
            {"tag_id": "tag_001", "applicability": 3,
             "evidence_strength": 5, "evidences": []},
            {"tag_id": "tag_002", "applicability": 4,
             "evidence_strength": 4, "evidences": []},
            {"tag_id": "tag_003", "applicability": 5,
             "evidence_strength": 2, "evidences": []},
        ],
        "dir_mood": [
            {"tag_id": "tag_004", "applicability": 4,
             "evidence_strength": 3, "evidences": []},
            {"tag_id": "tag_005", "applicability": 5,
             "evidence_strength": 4, "evidences": [{
                 "scene_id": content_fixture.content.scenes[0].scene_id,
                 "evidence": "场景摘要",
             }]},
        ],
    }

    out = ClosedTagProducer().produce(spec, session,
                                      _ScriptedLLM(responses), MOCK_EMBEDDER)
    # 镜像召回：与 produce 同参数，拿每个候选的 Semb（mock 确定性）
    candidates = recall_candidates(
        try_load_library(), MOCK_EMBEDDER,
        embed_content_texts(content_fixture), top_k=50)
    semb = {c.tag_id: c.score for c in candidates}
    group_order = list(dict.fromkeys(c.dir_id for c in candidates))

    def _sf(item: dict) -> float:
        return (0.2 * semb[item["tag_id"]]
                + 0.5 * (item["applicability"] - 1) / 4
                + 0.3 * (item["evidence_strength"] - 1) / 4)

    # A=3 恰过门控（Sapp=0.5 → G=1）参与排序；组内按 Sf 取最高，组序=召回序
    expected = [
        max((it for it in responses[d] if it["applicability"] >= 3), key=_sf)
        for d in group_order
    ]
    assert [t.tag_id for t in out] == [it["tag_id"] for it in expected]
    # 截断发生在组内：单目录上限 1，两组各 1 条（合并后不再二次截断）
    assert len(out) == 2
    assert {t.dir_id for t in out} == {"dir_genre", "dir_mood"}
    # Sf 与标签成对（relevance_score）；confidence 仍为 (E−1)/4 独立读数
    for row, it in zip(out, expected):
        assert row.relevance_score == pytest.approx(_sf(it))
        assert row.confidence == (it["evidence_strength"] - 1) / 4
    scene = content_fixture.content.scenes[0]
    by_tag = {t.tag_id: t for t in out}
    assert by_tag["tag_005"].evidence == [Evidence(
        scene_id=scene.scene_id,
        start_ms=scene.time_range.start_ms,
        end_ms=scene.time_range.end_ms,
        description="场景摘要",
    )]


def test_closed_produce_gate_drops_applicability_below_three(
        session, content_fixture):
    """门控 G：Sapp<0.5（A≤2）→ G=0 整条丢弃；A=3 恰在门线上保留。"""
    _seed_library()
    spec = _build_spec(session, content_fixture, run_id="r4",
                       policy=GenerationPolicy(max_tags=10))
    responses = {
        "dir_genre": [
            {"tag_id": "tag_001", "applicability": 3,
             "evidence_strength": 5, "evidences": []},
            {"tag_id": "tag_002", "applicability": 2,
             "evidence_strength": 5, "evidences": []},
        ],
        "dir_mood": [
            {"tag_id": "tag_004", "applicability": 5,
             "evidence_strength": 5, "evidences": []},
            {"tag_id": "tag_005", "applicability": 1,
             "evidence_strength": 5, "evidences": []},
        ],
    }
    out = ClosedTagProducer().produce(spec, session,
                                      _ScriptedLLM(responses), MOCK_EMBEDDER)
    # max_tags=10 富余：A∈{1,2} 被丢只能因门控，A=3 恰好放行
    assert {t.tag_id for t in out} == {"tag_001", "tag_004"}
    semb = {c.tag_id: c.score for c in recall_candidates(
        try_load_library(), MOCK_EMBEDDER,
        embed_content_texts(content_fixture), top_k=50)}
    by_tag = {t.tag_id: t for t in out}
    # 权重算术显式展开：Sf = 0.2·Semb + 0.5·((3−1)/4) + 0.3·((5−1)/4)
    assert by_tag["tag_001"].relevance_score == pytest.approx(
        0.2 * semb["tag_001"] + 0.5 * 0.5 + 0.3 * 1.0)
    assert by_tag["tag_001"].confidence == 1.0


def test_closed_no_candidates_raises(session, content_fixture):
    """空库：召回零候选 → 显式 ValueError（先导库再生成），不调 LLM。"""
    spec = GenerationSpec(task_id="t1", asset_id=content_fixture.asset.asset_id,
                          content=content_fixture, policy=GenerationPolicy(),
                          prompt_text="", run_id="r1",
                          library=TagLibrary([], []))
    with pytest.raises(ValueError, match="候选标签为空"):
        ClosedTagProducer().produce(spec, session, MOCK_LLM, MOCK_EMBEDDER)


def test_closed_without_library_raises(session, content_fixture):
    spec = GenerationSpec(task_id="t1", asset_id=content_fixture.asset.asset_id,
                          content=content_fixture, policy=GenerationPolicy(),
                          prompt_text="", run_id="r1", library=None)
    with pytest.raises(ValueError, match="标签库未就绪"):
        ClosedTagProducer().produce(spec, session, MOCK_LLM, MOCK_EMBEDDER)


# ---- parse_closed_output：模式 A 契约校验 ----


def test_parse_closed_bad_structure():
    with pytest.raises(LLMParseError):
        parse_closed_output('{"not": "a list"}')
    with pytest.raises(LLMParseError):
        parse_closed_output('[{"tag_id": "x"}]')  # 缺键
    with pytest.raises(LLMParseError):
        parse_closed_output('[{"tag_id":"x","applicability":6,'
                            '"evidence_strength":4,"evidences":[]}]')
    with pytest.raises(LLMParseError):
        parse_closed_output('[{"tag_id":"x","applicability":4.5,'
                            '"evidence_strength":4,"evidences":[]}]')
    with pytest.raises(LLMParseError):
        parse_closed_output('[{"tag_id":"x","applicability":4,'
                            '"evidence_strength":4,"evidences":[{"scene_id":"s1"}]}]')
    with pytest.raises(LLMParseError):
        parse_closed_output("not json at all")  # 非 JSON 文本


def test_parse_closed_happy_path():
    items = parse_closed_output(json.dumps([
        {"tag_id": "tag_001", "applicability": 5, "evidence_strength": 4,
         "evidences": [{"scene_id": "s1", "evidence": "开场悬念"}]},
        # 边界值 [1,5] 双侧含端点
        {"tag_id": "tag_002", "applicability": 1, "evidence_strength": 1,
         "evidences": []},
    ], ensure_ascii=False))
    assert items[0]["tag_id"] == "tag_001"
    assert items[0]["evidences"][0]["scene_id"] == "s1"
    assert items[1]["applicability"] == 1


def test_parse_closed_strips_markdown_code_fence():
    # 生产实测（2026-11-20 e2e，Qwen3.8-27B temperature=0.7）：模型会把 JSON
    # 包进 ```json 围栏，裸 json.loads 在首字符反引号上抛
    # "Expecting value: line 1 column 1 (char 0)" → 任务失败。
    # 围栏出现与否随采样波动（同 payload 上一次裸 JSON、这一次带围栏）。
    body = json.dumps([
        {"tag_id": "tag_001", "applicability": 5, "evidence_strength": 4,
         "evidences": [{"scene_id": "s1", "evidence": "开场悬念"}]},
    ], ensure_ascii=False)
    fenced = f"```json\n{body}\n```"
    assert parse_closed_output(fenced)[0]["tag_id"] == "tag_001"
    # 无语言标注的裸围栏同样要剥
    bare = f"```\n{body}\n```"
    assert parse_closed_output(bare)[0]["tag_id"] == "tag_001"
    # 围栏前后有模型补充的空白也要容忍
    padded = f"\n{fenced}\n"
    assert parse_closed_output(padded)[0]["tag_id"] == "tag_001"


def test_parse_closed_error_message_has_position():
    bad = json.dumps([
        {"tag_id": "t", "applicability": 4, "evidence_strength": 4,
         "evidences": []},
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
            "evidence_strength": 4, "evidences": []}
    items = parse_closed_output(json.dumps({"tags": [item]}, ensure_ascii=False))
    assert items == [item]
    # 多个列表值无法判断哪个是结果 → 仍然报错（不猜）
    with pytest.raises(LLMParseError, match="列表值数量为 2"):
        parse_closed_output(json.dumps({"tags": [item], "warnings": []}))
