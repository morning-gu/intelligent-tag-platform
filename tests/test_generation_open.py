"""模式 B（开放式生成）测试：MovieTagGen 移植版流水线。

覆盖契约（2026-11 替换后）：
- 多轮生成 → 跨轮归一/语义合并 → judge 过滤 → 证据校验 → ProducedTag；
- 只有 judge 判 supported 且置信度过线的候选才产出；
- 证据锚点必须落在资产真实场景里，编造的 scene_id 一律淘汰；
- 空目录不报错（全部 generated）；命中目录（相似度 ≥ 阈值）标 catalog；
- LLM / judge 失败显式抛错（任务置 failed，不静默放行）；
- `builds_prompt=True` + `prompt_version=mtg-v3`：runner 不再取 DB 模板。

Mock 说明：`MOCK_LLM` 对生成 prompt 固定返回三个候选（多线叙事/情感张力/
视听风格），对审核 prompt 全部判 supported、confidence=0.9；`MOCK_EMBEDDER`
是哈希伪向量，不同标签近似正交，所以语义合并不额外合并——跨轮合并靠
归一化去重（同一标签三轮出现 → runs=3）。
"""

import json
import re

import pytest
from conftest import MOCK_EMBEDDER, MOCK_LLM

from tagplatform.generation import GenerationPolicy, GenerationSpec
from tagplatform.generation.mtg import keyframes as keyframes_mod
from tagplatform.generation.mtg import pipeline as mtg_pipeline
from tagplatform.generation.mtg.merge import merge_candidates
from tagplatform.generation.open_gen import OpenTagProducer
from tagplatform.llm.client import LLMResponse
from tagplatform.library import try_load_library
from tagplatform.settings import Settings
from tests.library_fixture import add_tags


def _build_spec(session, content_fixture, run_id: str, policy=None) -> GenerationSpec:
    """组装 spec：open 自拼 prompt，prompt_text 恒为空串（保留签名兼容旧测试）。"""
    return GenerationSpec(
        task_id=f"t-{run_id}", asset_id=content_fixture.asset.asset_id,
        content=content_fixture, policy=policy or GenerationPolicy(),
        prompt_text="", run_id=run_id, library=try_load_library(),
    )


@pytest.fixture
def mtg_settings(tmp_path, monkeypatch):
    """隔离 settings：mock 判定、报告落临时目录、数据集指向空目录。"""
    settings = Settings(
        use_mock_llm=True,
        dataset_root=str(tmp_path / "movienet"),
        mtg_report_dir=str(tmp_path / "audit"),
    )
    monkeypatch.setattr(mtg_pipeline, "get_settings", lambda: settings)
    return settings


class _JudgeClient:
    """固定判决的 judge 桩：verdict/confidence/scenes/quote 由构造参数决定。

    echo_full=True 复现真实调用的坏习惯：把整行候选（含维度/依据）抄进
    tag 字段——对齐逻辑必须仍能匹配回候选。
    quote 缺省时从审核 prompt 的首个场景行截一段原文（模拟"引用原文"）。
    """

    def __init__(self, verdict="supported", confidence=0.9, scenes=None,
                 omit: str | None = None, echo_full: bool = False,
                 quote: str | None = "", importance_map: dict | None = None):
        self.verdict = verdict
        self.confidence = confidence
        self.scenes = scenes
        self.omit = omit
        self.echo_full = echo_full
        self.quote = quote
        self.importance_map = importance_map or {}
        self.calls = 0

    def complete(self, messages, run_id):
        self.calls += 1
        text = messages[0]["content"]
        rows = [line[2:] for line in text.splitlines()
                if line.startswith("- ") and "｜" in line]
        match = re.search(r"^- \S+ \[\d+-\d+\] (.+)$", text, re.MULTILINE)
        quote = self.quote
        if quote == "":  # 未显式指定 → 从首个场景行截原文
            quote = match.group(1)[:12] if match else ""
        items = []
        for row in rows:
            tag = row.split("｜")[0]
            if tag == self.omit:
                continue
            items.append({
                "tag": row if self.echo_full else tag,
                "verdict": self.verdict,
                "importance": self.importance_map.get(tag, "core"),
                "confidence": self.confidence,
                "scenes": self.scenes if self.scenes is not None else [],
                "quote": quote,
                "reason": "桩判定",
            })
        content = json.dumps(items, ensure_ascii=False)
        return LLMResponse(content=content, usage={"prompt_tokens": 10,
                                                  "completion_tokens": 5},
                           raw={}, ok=True)


class _FailingLLM:
    def complete(self, messages, run_id):
        return LLMResponse(content="", usage={}, raw={"error": "boom"}, ok=False)


class _GarbageJudge:
    def complete(self, messages, run_id):
        return LLMResponse(content="不是 JSON", usage={}, raw={}, ok=True)


class _FixedGenLLM:
    """固定生成输出的桩：每轮返回给定候选（用于证据/去重用例）。"""

    def __init__(self, items):
        self.items = items

    def complete(self, messages, run_id):
        return LLMResponse(content=json.dumps(self.items, ensure_ascii=False),
                           usage={"prompt_tokens": 100, "completion_tokens": 20},
                           raw={}, ok=True)


# ---- 主链路 ----


def test_open_producer_declares_self_built_prompt():
    producer = OpenTagProducer()
    assert producer.mode == "open"
    assert producer.builds_prompt is True
    assert producer.prompt_version == "mtg-v3"


def test_open_end_to_end(session, content_fixture, mtg_settings, tmp_path):
    """三轮同名候选 → 合并成 3 条 → judge 全过 → 3 条 generated 带证据。"""
    spec = _build_spec(session, content_fixture, run_id="r1")
    out = OpenTagProducer().produce(spec, session, MOCK_LLM, MOCK_EMBEDDER)

    assert [t.tag_name for t in out] == ["多线叙事", "情感张力", "视听风格"]
    assert all(t.origin == "generated" and t.tag_id is None for t in out)
    assert all(t.confidence == 0.9 for t in out)
    assert all(t.candidate_type and t.candidate_description for t in out)
    assert all(t.evidence for t in out)
    scene_ids = {s.scene_id for s in content_fixture.content.scenes}
    assert all(e.scene_id in scene_ids for t in out for e in t.evidence)

    # 审计报告：三轮、合并 runs=3、judge 判决齐全
    report = json.loads((tmp_path / "audit" / "t-r1.json").read_text(encoding="utf-8"))
    assert report["version"] == "mtg-v3"
    assert len(report["runs"]) == 3
    assert {m["runs"] for m in report["merged"]} == {3}
    assert len(report["accepted"]) == 3


def test_open_judge_rejected_dropped(session, content_fixture, mtg_settings):
    """judge 判 insufficient → 不进审核队列。"""
    spec = _build_spec(session, content_fixture, run_id="r2")
    out = OpenTagProducer().produce(
        spec, session, MOCK_LLM, MOCK_EMBEDDER,
        judge_client=_JudgeClient(verdict="insufficient"))
    assert out == []


def test_open_judge_low_confidence_dropped(session, content_fixture, mtg_settings):
    """judge 判定把握低于地板（0.5）→ 淘汰；不低于则保留并如实记置信度。"""
    spec = _build_spec(session, content_fixture, run_id="r3")
    dropped = OpenTagProducer().produce(
        spec, session, MOCK_LLM, MOCK_EMBEDDER,
        judge_client=_JudgeClient(confidence=0.4))
    assert dropped == []

    kept = OpenTagProducer().produce(
        _build_spec(session, content_fixture, run_id="r3b"),
        session, MOCK_LLM, MOCK_EMBEDDER,
        judge_client=_JudgeClient(confidence=0.6))
    assert len(kept) == 3
    assert {t.confidence for t in kept} == {0.6}


def test_open_missing_verdict_dropped(session, content_fixture, mtg_settings):
    """judge 漏判某候选（omit）→ 该候选按未返回处理，淘汰。"""
    spec = _build_spec(session, content_fixture, run_id="r4")
    out = OpenTagProducer().produce(
        spec, session, MOCK_LLM, MOCK_EMBEDDER,
        judge_client=_JudgeClient(omit="情感张力"))
    assert [t.tag_name for t in out] == ["多线叙事", "视听风格"]


def test_open_judge_echoing_full_line_still_matches(session, content_fixture,
                                                   mtg_settings):
    """judge 把整行候选抄进 tag（真实调用出现过）→ 仍能对齐，不误判"未返回"。"""
    spec = _build_spec(session, content_fixture, run_id="r10")
    out = OpenTagProducer().produce(
        spec, session, MOCK_LLM, MOCK_EMBEDDER,
        judge_client=_JudgeClient(echo_full=True))
    assert [t.tag_name for t in out] == ["多线叙事", "情感张力", "视听风格"]


def test_open_judge_object_wrapped_output_still_parsed(session, content_fixture,
                                                       mtg_settings):
    """judge 把判决包成对象（{"verdicts":[...]} / 以标签为键）→ 仍能解析。"""
    class _WrappedJudge:
        def __init__(self, payload):
            self.payload = payload

        def complete(self, messages, run_id):
            return LLMResponse(content=json.dumps(self.payload, ensure_ascii=False),
                               usage={"prompt_tokens": 10, "completion_tokens": 5},
                               raw={}, ok=True)

    def verdict(tag):
        return {"tag": tag, "verdict": "supported", "importance": "core",
                "confidence": 0.9, "scenes": ["scene_001"],
                "quote": "两名人物在仓库中发生激烈争执", "reason": "桩"}

    for payload in ({"verdicts": [verdict("多线叙事"), verdict("情感张力"),
                                  verdict("视听风格")]},
                    {"多线叙事": {"verdict": "supported", "importance": "core",
                                  "confidence": 0.9, "scenes": ["scene_001"],
                                  "quote": "两名人物在仓库中发生激烈争执"},
                     "情感张力": {"verdict": "supported", "importance": "core",
                                  "confidence": 0.9, "scenes": ["scene_001"],
                                  "quote": "两名人物在仓库中发生激烈争执"},
                     "视听风格": {"verdict": "supported", "importance": "core",
                                  "confidence": 0.9, "scenes": ["scene_001"],
                                  "quote": "两名人物在仓库中发生激烈争执"}}):
        spec = _build_spec(session, content_fixture, run_id="r11")
        out = OpenTagProducer().produce(
            spec, session, MOCK_LLM, MOCK_EMBEDDER,
            judge_client=_WrappedJudge(payload))
        assert len(out) == 3


def test_open_evidence_prefers_judge_scenes(session, content_fixture, mtg_settings):
    """judge 给了场景就只用它，不并生成侧场景（证据卡只留判定依据）。"""
    content = _scene_content(content_fixture, "tt0000003", 2)
    generated = _FixedGenLLM([{
        "tag": "多线叙事", "dimension": "叙事",
        "scenes": ["tt0000003_story_000"], "reason": "生成侧"}])
    spec = GenerationSpec(task_id="t-r12", asset_id=content.asset.asset_id,
                          content=content, policy=GenerationPolicy(),
                          prompt_text="", run_id="r12",
                          library=try_load_library())
    out = OpenTagProducer().produce(
        spec, session, generated, MOCK_EMBEDDER,
        judge_client=_JudgeClient(scenes=["tt0000003_story_001"]))
    assert len(out) == 1
    assert [e.scene_id for e in out[0].evidence] == ["tt0000003_story_001"]


def test_open_without_valid_evidence_dropped(session, content_fixture, mtg_settings):
    """生成与 judge 都给不出真实 scene_id → 无证据锚点，淘汰。"""
    garbage = _FixedGenLLM([{"tag": "空中楼阁", "dimension": "主题",
                             "scenes": ["不存在_scene"], "reason": "编的"}])
    spec = _build_spec(session, content_fixture, run_id="r5")
    out = OpenTagProducer().produce(
        spec, session, garbage, MOCK_EMBEDDER,
        judge_client=_JudgeClient(scenes=[]))
    assert out == []


def test_open_catalog_match_sets_catalog_origin(session, content_fixture, mtg_settings):
    """目录里有同文本标签 → 命中（相似度 1.0）落 origin=catalog。"""
    add_tags([{"tag_id": "tag_cat_1", "dir_id": "dir_theme",
               "tag_name": "多线叙事", "tag_type": "val_fixed",
               "description": "多线叙事", "status": "active"}])
    spec = _build_spec(session, content_fixture, run_id="r6")
    out = OpenTagProducer().produce(spec, session, MOCK_LLM, MOCK_EMBEDDER)
    hits = [t for t in out if t.origin == "catalog"]
    assert len(hits) == 1
    assert hits[0].tag_id == "tag_cat_1"
    assert hits[0].relevance_score == pytest.approx(1.0)
    assert hits[0].candidate_type is None and hits[0].candidate_description is None
    assert [t.tag_name for t in out if t.origin == "generated"] == ["情感张力", "视听风格"]


def test_open_respects_max_final_tags(session, content_fixture, mtg_settings):
    """policy.max_tags 与 mtg_max_final_tags 取小者截断。"""
    spec = _build_spec(session, content_fixture, run_id="r7",
                       policy=GenerationPolicy(max_tags=1))
    out = OpenTagProducer().produce(spec, session, MOCK_LLM, MOCK_EMBEDDER)
    assert len(out) == 1


# ---- 层级约束（内部把关 + 层级落库） ----


def test_open_film_scope_needs_two_scenes(session, content_fixture, mtg_settings):
    """全片级但只有单场支撑 → 淘汰；场景级单场支撑 → 保留，且层级随
    产出带出（film/scene 落库到 asset_tag.level）。"""
    content = _scene_content(content_fixture, "tt0000009", 2)
    spec = _build_spec(session, content, run_id="rf")
    llm = _FixedGenLLM([
        {"tag": "道德堕落", "scope": "film", "dimension": "主题",
         "scenes": ["tt0000009_story_000"], "reason": "只引一场"},
        {"tag": "开场旁白", "scope": "scene", "dimension": "叙事",
         "scenes": ["tt0000009_story_000"], "reason": "局部手法"},
        {"tag": "家族权力更迭", "scope": "film", "dimension": "主题",
         "scenes": ["tt0000009_story_000", "tt0000009_story_001"],
         "reason": "两场共同支撑"},
    ])
    out = OpenTagProducer().produce(spec, session, llm, MOCK_EMBEDDER,
                                    judge_client=_JudgeClient())
    names = {tag.tag_name for tag in out}
    assert names == {"开场旁白", "家族权力更迭"}
    levels = {tag.tag_name: tag.level for tag in out}
    assert levels == {"开场旁白": "scene", "家族权力更迭": "film"}


# ---- 队列治理：judge 引用原文 + 重要度排序 ----


def test_open_judge_without_quote_downgraded(session, content_fixture,
                                             mtg_settings):
    """judge 判 supported 但引用不到原文 → 按 insufficient 处理（淘汰）。"""
    spec = _build_spec(session, content_fixture, run_id="rq1")
    out = OpenTagProducer().produce(
        spec, session, MOCK_LLM, MOCK_EMBEDDER,
        judge_client=_JudgeClient(quote="这句原文根本不在影片内容里"))
    assert out == []


def test_open_importance_orders_queue(session, content_fixture, mtg_settings):
    """队列先按 judge 重要度排：core 排在 secondary 前（同把握同轮数时）。"""
    spec = _build_spec(session, content_fixture, run_id="rq2")
    llm = _FixedGenLLM([
        {"tag": "次要标签", "dimension": "情绪", "scenes": ["scene_001"],
         "reason": "次要"},
        {"tag": "核心标签", "dimension": "主题", "scenes": ["scene_001"],
         "reason": "核心"},
    ])
    out = OpenTagProducer().produce(
        spec, session, llm, MOCK_EMBEDDER,
        judge_client=_JudgeClient(importance_map={"核心标签": "core",
                                                  "次要标签": "secondary"}))
    assert [tag.tag_name for tag in out][0] == "核心标签"


# ---- 失败语义 ----


def test_open_llm_failure_raises(session, content_fixture, mtg_settings):
    spec = _build_spec(session, content_fixture, run_id="r8")
    with pytest.raises(RuntimeError, match="LLM 调用失败"):
        OpenTagProducer().produce(spec, session, _FailingLLM(), MOCK_EMBEDDER)


def test_open_judge_parse_failure_raises(session, content_fixture, mtg_settings):
    """judge 两次都解析失败 → JudgeError（任务应 failed，不放行）。"""
    from tagplatform.generation.mtg.judge import JudgeError
    spec = _build_spec(session, content_fixture, run_id="r9")
    with pytest.raises(JudgeError):
        OpenTagProducer().produce(spec, session, MOCK_LLM, MOCK_EMBEDDER,
                                  judge_client=_GarbageJudge())


# ---- 合并 ----


def test_merge_dedups_identical_across_runs():
    """同一标签三轮出现 → 单条候选，runs=3，别名去重。"""
    entries = [{"tag": "多线叙事", "dimension": "叙事", "scenes": ["s1"],
                "run": run, "reason": "r"} for run in range(3)]
    entries.append({"tag": "多线 叙事", "dimension": "叙事", "scenes": ["s2"],
                    "run": 0, "reason": "r"})
    merged = merge_candidates(entries, MOCK_EMBEDDER, 0.9)
    assert len(merged) == 1
    assert merged[0].tag == "多线叙事"
    assert merged[0].runs == 3
    assert set(merged[0].scenes) == {"s1", "s2"}


def test_merge_semantic_clusters_near_duplicates():
    """语义相近（构造向量）的候选合并成一条，代表词取轮数多者。"""
    import numpy as np

    class _PairEmbedder:
        name, dim = "pair", 2

        def embed(self, texts, *, role):
            # "甲" 与 "乙" 相似度 0.99，"丙" 与它们正交
            table = {"甲": [1.0, 0.0], "乙": [0.99, 0.14], "丙": [0.0, 1.0]}
            return np.array([table[t] for t in texts], dtype="float32")

    entries = [
        {"tag": "甲", "dimension": "主题", "scenes": ["s1"], "run": 0, "reason": ""},
        {"tag": "甲", "dimension": "主题", "scenes": ["s1"], "run": 1, "reason": ""},
        {"tag": "乙", "dimension": "主题", "scenes": ["s2"], "run": 1, "reason": ""},
        {"tag": "丙", "dimension": "风格", "scenes": ["s3"], "run": 0, "reason": ""},
    ]
    merged = merge_candidates(entries, _PairEmbedder(), 0.9)
    assert len(merged) == 2
    first = next(m for m in merged if m.tag == "甲")
    assert first.runs == 2 and set(first.aliases) == {"甲", "乙"}


# ---- 关键帧装配 ----


def _write_dataset(tmp_path, movie_id="tt0000001", story=None, frames=None):
    root = tmp_path / "movienet"
    ann = root / "annotation_data" / "annotation"
    ann.mkdir(parents=True)
    (ann / f"{movie_id}.json").write_text(
        json.dumps({"story": story or []}), encoding="utf-8")
    frames_dir = root / "frames" / "frames" / movie_id
    frames_dir.mkdir(parents=True)
    for name in frames or []:
        (frames_dir / name).write_bytes(b"jpg")
    return root


def _scene_content(content_fixture, movie_id: str, count: int):
    """复制 fixture 的首场景成 count 个场景，场景 id 用 movie_id 前缀。"""
    content = content_fixture.model_copy(deep=True)
    content.asset.asset_id = f"movie_{movie_id}"
    base = content.content.scenes[0]
    scenes = []
    for i in range(count):
        scene = base.model_copy(deep=True)
        scene.scene_id = f"{movie_id}_story_{i:03d}"
        scene.time_range.start_ms = i * 60000
        scene.time_range.end_ms = i * 60000 + 60000
        scenes.append(scene)
    content.content.scenes = scenes
    return content


def test_plan_scene_frames_by_scene_id(tmp_path, content_fixture):
    story = [
        {"duration": [0, 60], "shot": [0, 2]},
        {"duration": [120, 180], "shot": [3, 5]},
    ]
    frames = [f"shot_{i:04d}_img_0.jpg" for i in range(6)]
    root = _write_dataset(tmp_path, "tt0000001", story, frames)
    content = _scene_content(content_fixture, "tt0000001", 2)

    plan = keyframes_mod.plan_scene_frames(content, str(root), per_scene=2,
                                           max_frames=24)
    assert len(plan["tt0000001_story_000"]) == 2
    assert len(plan["tt0000001_story_001"]) == 2
    assert all(p.exists() for paths in plan.values() for p in paths)


def test_plan_scene_frames_caps_total(tmp_path, content_fixture):
    story = [{"duration": [i * 60, i * 60 + 60], "shot": [i, i]}
             for i in range(10)]
    frames = [f"shot_{i:04d}_img_0.jpg" for i in range(10)]
    root = _write_dataset(tmp_path, "tt0000002", story, frames)
    content = _scene_content(content_fixture, "tt0000002", 10)

    plan = keyframes_mod.plan_scene_frames(content, str(root), per_scene=2,
                                           max_frames=6)
    assert sum(len(v) for v in plan.values()) == 6
    # 覆盖优先：每个场景先拿到 1 张
    assert sum(1 for v in plan.values() if v) == 6


def test_plan_scene_frames_missing_annotation_is_empty(tmp_path, content_fixture):
    root = tmp_path / "empty"
    root.mkdir()
    assert keyframes_mod.plan_scene_frames(content_fixture, str(root)) == {}
