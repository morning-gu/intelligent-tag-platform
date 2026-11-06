"""模式 B（开放式生成）测试：MovieTagGen 移植版流水线。

覆盖契约（2026-11 替换后；Task 7 v2 词表切换）：
- 多轮生成 → 跨轮归一/语义合并 → judge 过滤 → 证据校验 → ProducedTag；
- 只有 judge 判 supported 且置信度过线的候选才产出；
- 证据锚点必须落在资产真实场景里，编造的 scene_id 一律淘汰；
- 空库不报错（全部 open 自由候选）；归一命中（相似度 ≥ 阈值，含等于）行
  origin=open 且带 tag_id——v2 词表下命中行不再标 catalog（doc §6.2），
  candidate_name 保留候选原文；阈值边界（≥ 命中 / < 不命中）用 MockEmbedder
  构造可控相似度钉死；
- LLM / judge 失败显式抛错（任务置 failed，不静默放行）；
- `builds_prompt=True` + `prompt_version=mtg-v1`：runner 不再取 DB 模板。

Mock 说明：`MOCK_LLM` 对生成 prompt 固定返回三个候选（多线叙事/情感张力/
视听风格），对审核 prompt 全部判 supported、confidence=0.9；`MOCK_EMBEDDER`
是哈希伪向量，不同标签近似正交，所以语义合并不额外合并——跨轮合并靠
归一化去重（同一标签三轮出现 → runs=3）。
"""

import json
import re

import numpy as np
import pytest
from conftest import MOCK_EMBEDDER, MOCK_LLM

from tagplatform.db.models import TagCatalog, TagDefine
from tagplatform.generation import GenerationPolicy, GenerationSpec
from tagplatform.generation.mtg import keyframes as keyframes_mod
from tagplatform.generation.mtg import pipeline as mtg_pipeline
from tagplatform.generation.mtg.merge import merge_candidates
from tagplatform.generation.open_gen import OpenTagProducer
from tagplatform.llm.client import LLMResponse
from tagplatform.settings import Settings
from tagplatform.vectors import decode_vector, encode_vector, search_similar


def _build_spec(session, content_fixture, run_id: str, policy=None) -> GenerationSpec:
    """组装 spec：open 自拼 prompt，prompt_text 恒为空串（保留签名兼容旧测试）。"""
    return GenerationSpec(
        task_id=f"t-{run_id}", asset_id=content_fixture.asset.asset_id,
        content=content_fixture, policy=policy or GenerationPolicy(),
        prompt_text="", run_id=run_id,
    )


def _seed_define(session, tag_id: str, tag_name: str, description: str,
                 vec, dir_id: str = "dir_open") -> None:
    """播种一条带向量的标签定义（v2 双表：目录行先 flush 再插定义，外键
    父行先行；同一 dir_id 只建一次目录）。"""
    if session.get(TagCatalog, dir_id) is None:
        session.add(TagCatalog(dir_id=dir_id, dir_name="开放生成目录",
                               dir_type="dir_static",
                               description="open 归一命中的标签定义目录"))
        session.flush()
    session.add(TagDefine(tag_id=tag_id, tag_name=tag_name,
                          tag_type="val_fixed", description=description,
                          dir_id=dir_id, status="active",
                          embedding=encode_vector(vec)))
    session.commit()


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
    assert producer.prompt_version == "mtg-v1"


def test_open_end_to_end(session, content_fixture, mtg_settings, tmp_path):
    """三轮同名候选 → 合并成 3 条 → judge 全过 → 3 条 open 自由候选带证据。"""
    spec = _build_spec(session, content_fixture, run_id="r1")
    out = OpenTagProducer().produce(spec, session, MOCK_LLM, MOCK_EMBEDDER)

    assert [t.tag_name for t in out] == ["多线叙事", "情感张力", "视听风格"]
    # 空库（无标签定义）不命中：全部 open 自由候选（tag_id 空，v2 词表）
    assert all(t.origin == "open" and t.tag_id is None for t in out)
    assert all(t.confidence == 0.9 for t in out)
    assert all(t.candidate_type and t.candidate_description for t in out)
    assert all(t.evidence for t in out)
    scene_ids = {s.scene_id for s in content_fixture.content.scenes}
    assert all(e.scene_id in scene_ids for t in out for e in t.evidence)

    # 审计报告：三轮、合并 runs=3、judge 判决齐全
    report = json.loads((tmp_path / "audit" / "t-r1.json").read_text(encoding="utf-8"))
    assert report["version"] == "mtg-v1"
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
                          prompt_text="", run_id="r12")
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


def test_open_normalized_match_keeps_candidate_name(session, content_fixture,
                                                    mtg_settings):
    """归一命中：候选名 ≠ 定义名但余弦 ≥ 阈值（Gram-Schmidt 混合向量构造，
    手法同 test_open_match_threshold_boundary）→ 命中行 origin=open 且带
    tag_id，candidate_name 保留候选原文（doc §6.2 可溯原则——并入既有也留
    原文；候选原文与定义名不同，"原文保留"断言具区分性），
    candidate_description 不填（仅自由候选，doc §3.5）。"""
    q = MOCK_EMBEDDER.embed(["多线叙事"], role="query")[0]
    anchor = MOCK_EMBEDDER.embed(["无关锚向量"], role="query")[0]
    orthogonal = anchor - np.dot(anchor, q) * q
    orthogonal /= np.linalg.norm(orthogonal)
    coef = 0.95  # ≥ 默认阈值 0.85：不同名也命中
    vec = coef * q + np.sqrt(1 - coef ** 2) * orthogonal
    vec = vec / np.linalg.norm(vec)
    _seed_define(session, "tag_cat_1", "多线索叙事", "多线并进的叙事结构", vec)
    spec = _build_spec(session, content_fixture, run_id="r6")
    out = OpenTagProducer().produce(spec, session, MOCK_LLM, MOCK_EMBEDDER)
    hits = [t for t in out if t.tag_id is not None]
    assert len(hits) == 1
    hit = hits[0]
    assert hit.origin == "open"  # v2 词表：命中行也是 open（catalog 词退役）
    assert hit.tag_id == "tag_cat_1"  # 指向定义（其名为「多线索叙事」）
    assert hit.tag_name == "多线叙事"  # 候选原文保留，非定义名回显
    assert session.get(TagDefine, "tag_cat_1").tag_name == "多线索叙事"
    assert hit.tag_name != session.get(TagDefine, "tag_cat_1").tag_name
    assert hit.relevance_score == pytest.approx(coef, abs=1e-4)
    assert hit.candidate_type is None and hit.candidate_description is None
    free = [t for t in out if t.tag_id is None]
    assert [t.tag_name for t in free] == ["情感张力", "视听风格"]
    assert all(t.origin == "open" and t.candidate_description for t in free)


def test_open_match_threshold_boundary(session, content_fixture, tmp_path,
                                       monkeypatch):
    """阈值边界（≥ 含等于命中 / < 不命中）：库内定义向量 = a·候选查询向量 +
    b·正交方向的归一化混合（Gram-Schmidt 造正交基），与对应候选的余弦即
    混合系数 a——命中候选构到 0.9、落选候选构到 0.8；阈值取命中侧的精确
    相似度（search_similar 与流水线同路径复算，逐位一致）→ 命中侧恰以
    等号过线（钉死 ≥ 含等于），落选侧（0.8）严格低于阈值不命中。"""
    q_hit = MOCK_EMBEDDER.embed(["可证伪结构"], role="query")[0]
    q_miss = MOCK_EMBEDDER.embed(["光谱化调度"], role="query")[0]
    anchor = MOCK_EMBEDDER.embed(["无关锚向量"], role="query")[0]

    def _blend(query, coefficient):
        """与 query 余弦恰为 coefficient 的归一化混合向量。"""
        orthogonal = anchor - np.dot(anchor, query) * query
        orthogonal /= np.linalg.norm(orthogonal)
        vec = coefficient * query + np.sqrt(1 - coefficient ** 2) * orthogonal
        return vec / np.linalg.norm(vec)

    hit_vec = _blend(q_hit, 0.9)
    miss_vec = _blend(q_miss, 0.8)
    _seed_define(session, "tag_hit", "可证伪结构", "叙事结构可证伪", hit_vec)
    _seed_define(session, "tag_miss", "光谱化调度", "以色彩光谱调度情绪",
                 miss_vec)

    # 阈值 = 命中侧相似度的精确值（与流水线同一 search_similar 复算）
    stored = [("tag_hit", decode_vector(encode_vector(hit_vec))),
              ("tag_miss", decode_vector(encode_vector(miss_vec)))]
    threshold = search_similar(q_hit, stored, top_k=1)[0][1]
    settings = Settings(use_mock_llm=True,
                        dataset_root=str(tmp_path / "movienet"),
                        mtg_report_dir=str(tmp_path / "audit"),
                        open_match_threshold=threshold)
    monkeypatch.setattr(mtg_pipeline, "get_settings", lambda: settings)

    llm = _FixedGenLLM([
        {"tag": "可证伪结构", "dimension": "叙事",
         "scenes": ["scene_001"], "reason": "双层叙事互为镜像"},
        {"tag": "光谱化调度", "dimension": "风格",
         "scenes": ["scene_001"], "reason": "色彩随情绪推移"},
    ])
    spec = _build_spec(session, content_fixture, run_id="rb")
    out = OpenTagProducer().produce(spec, session, llm, MOCK_EMBEDDER,
                                    judge_client=_JudgeClient())

    by_name = {t.tag_name: t for t in out}
    hit = by_name["可证伪结构"]
    assert hit.origin == "open" and hit.tag_id == "tag_hit"  # ≥ 含等于 → 命中
    assert hit.relevance_score == threshold  # 同路径复算：逐位一致
    miss = by_name["光谱化调度"]
    assert miss.origin == "open"
    assert miss.tag_id is None and miss.relevance_score is None  # < 阈值不命中


def test_open_respects_max_final_tags(session, content_fixture, mtg_settings):
    """policy.max_tags 与 mtg_max_final_tags 取小者截断。"""
    spec = _build_spec(session, content_fixture, run_id="r7",
                       policy=GenerationPolicy(max_tags=1))
    out = OpenTagProducer().produce(spec, session, MOCK_LLM, MOCK_EMBEDDER)
    assert len(out) == 1


# ---- 层级约束（内部把关，不落库） ----


def test_open_film_scope_needs_two_scenes(session, content_fixture, mtg_settings):
    """全片级但只有单场支撑 → 淘汰；场景级单场支撑 → 保留。"""
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
