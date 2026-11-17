"""模式 A 封闭式标签提取测试：库召回、输出解析与 ClosedTagProducer。

覆盖契约（v2：库来自 JSON）：
- embed_content_texts：[剧情摘要, 各场景描述] 查询文本列表；
- recall_candidates：按目录分组召回（组内跨 query 取最高分 top_k，跨组
  合并去重），排除 deprecate / 占位行，范围 dir_ids 生效，空库返回 []；
- parse_closed_output：键集与分值域校验、坏结构抛 LLMParseError；
- ClosedTagProducer：库缺失/空候选 → ValueError；结果是 catalog 行，
  带目录与挂载点（目录 applicable_scope → content/scene/character）。
"""

import json

import pytest
from conftest import MOCK_EMBEDDER, MOCK_LLM
from sqlalchemy.orm import Session

from tagplatform.generation import GenerationPolicy, GenerationSpec
from tagplatform.generation.closed import ClosedTagProducer
from tagplatform.generation.recall import embed_content_texts, recall_candidates
from tagplatform.library import TagLibrary, try_load_library
from tagplatform.llm.client import LLMResponse
from tagplatform.llm.parsing import LLMParseError, parse_closed_output
from tagplatform.prompts import get_active_prompt, render_prompt
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


def test_closed_produce_groups_by_directory(session, content_fixture):
    """候选按目录分组、逐组调用 LLM：每组 prompt 含一个组对象 JSON，结果合并。

    spy 记录每次 llm.complete 的 prompt：断言调用次数 = 目录组数、每组只含
    本组标签、组 JSON 带目录名称/描述、prompts 首段都是渲染后的正文。
    """
    _seed_library()
    spec = _build_spec(session, content_fixture, run_id="r2")
    library = try_load_library()

    calls: list[str] = []

    class _SpyLLM(MOCK_LLM.__class__):
        def complete(self, messages, run_id):
            calls.append(messages[0]["content"])
            return super().complete(messages, run_id)

    out = ClosedTagProducer().produce(spec, session, _SpyLLM(), MOCK_EMBEDDER)
    assert out  # 合并各组结果非空

    # 召回候选的目录集合 = 分组组数 = LLM 调用次数
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


def test_closed_produce_filters_and_caps_per_group(session, content_fixture):
    """组内完成过滤与截断：低置信度先滤掉，再按相关度降序取 max_tags 条。

    max_tags=1 + min_confidence=0.5：dir_genre 的 tag_003 置信度 0.3 被滤
    （即使 relevance 最高），余下取 relevance 最高的 tag_002；dir_mood 同理
    取 tag_005。目录间互不挤占：两组各留 1 条，共 2 条。
    """
    _seed_library()
    spec = _build_spec(session, content_fixture, run_id="r3",
                       policy=GenerationPolicy(max_tags=1, min_confidence=0.5))
    responses = {
        "dir_genre": [
            {"tag_id": "tag_001", "tag_name": "悬疑", "relevance": 0.4,
             "confidence": 0.9, "evidence": []},
            {"tag_id": "tag_002", "tag_name": "剧情", "relevance": 0.8,
             "confidence": 0.9, "evidence": []},
            {"tag_id": "tag_003", "tag_name": "动作", "relevance": 0.95,
             "confidence": 0.3, "evidence": []},  # 置信度不过线：先被过滤
        ],
        "dir_mood": [
            {"tag_id": "tag_004", "tag_name": "紧张", "relevance": 0.5,
             "confidence": 0.6, "evidence": []},
            {"tag_id": "tag_005", "tag_name": "温馨", "relevance": 0.7,
             "confidence": 0.6, "evidence": []},
        ],
    }

    class _ScriptedLLM:
        """按组返回固定判定结果（从 prompt 尾部的组 JSON 取 dir_id）。"""

        def complete(self, messages, run_id):
            tail = messages[0]["content"].partition(
                "\n\n## 候选标签分组\n")[2]
            dir_id = json.loads(tail)["dir_id"]
            return LLMResponse(
                content=json.dumps(responses[dir_id], ensure_ascii=False),
                usage={}, raw={}, ok=True)

    out = ClosedTagProducer().produce(spec, session, _ScriptedLLM(),
                                      MOCK_EMBEDDER)
    # 每组恰好留下各自 relevance 最高的过线行；tag_003 虽最高分但被滤掉
    assert [t.tag_id for t in out] == ["tag_002", "tag_005"]
    # 截断发生在组内：单目录上限 1，两组各 1 条（合并后不再二次截断）
    assert len(out) == 2
    assert {t.dir_id for t in out} == {"dir_genre", "dir_mood"}


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
        parse_closed_output('[{"tag_id":"x","tag_name":"n","relevance":2.0,'
                            '"confidence":0.5,"evidence":[]}]')  # relevance 越界
    with pytest.raises(LLMParseError):
        parse_closed_output("not json at all")  # 非 JSON 文本


def test_parse_closed_happy_path():
    items = parse_closed_output(json.dumps([
        {"tag_id": "tag_001", "tag_name": "悬疑", "relevance": 0.9,
         "confidence": 0.8,
         "evidence": [{"scene_id": "s1", "start_ms": 0, "end_ms": 100,
                       "description": "开场悬念"}]},
        # 边界值 [0,1] 双侧含端点
        {"tag_id": "tag_002", "tag_name": "剧情", "relevance": 1.0,
         "confidence": 0, "evidence": []},
    ], ensure_ascii=False))
    assert items[0]["tag_id"] == "tag_001"
    assert items[0]["evidence"][0]["scene_id"] == "s1"
    assert items[1]["relevance"] == 1.0 and items[1]["confidence"] == 0


def test_parse_closed_error_message_has_position():
    bad = json.dumps([
        {"tag_id": "t", "tag_name": "n", "relevance": 0.5, "confidence": 0.5,
         "evidence": []},
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
    item = {"tag_id": "tag_001", "tag_name": "悬疑", "relevance": 0.9,
            "confidence": 0.8, "evidence": []}
    items = parse_closed_output(json.dumps({"tags": [item]}, ensure_ascii=False))
    assert items == [item]
    # 多个列表值无法判断哪个是结果 → 仍然报错（不猜）
    with pytest.raises(LLMParseError, match="列表值数量为 2"):
        parse_closed_output(json.dumps({"tags": [item], "warnings": []}))
