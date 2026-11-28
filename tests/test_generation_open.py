"""模式 B（开放式生成）测试：MovieTagGen 移植版流水线。

覆盖契约（2026-11 替换后）：
- 多轮生成 → 跨轮归一/语义合并 → judge 三维评分过滤 → 证据校验 → ProducedTag；
- 只有三维分数（relevance≥3 / semantics≥3 / value≥2）与置信度都过线的候选才产出；
- 证据锚点必须落在资产真实场景里，编造的 scene_id 一律淘汰；
- 空目录不报错（全部 generated）；命中目录（相似度 ≥ 阈值）标 catalog；
- LLM / judge 失败显式抛错（任务置 failed，不静默放行）；
- `builds_prompt=True` + `prompt_version=mtg-v15-unified-verdict`：runner 不再取 DB 模板。

Mock 说明：`MOCK_LLM` 对生成 prompt 固定返回三个候选（多线叙事/情感张力/
视听风格），对审核 prompt 按融合协议全部给过线三维分（relevance 4 /
semantics 3 / value 三方向 3，id 键 + 逐维 {score, reason}）、confidence=0.9；
`MOCK_EMBEDDER` 是哈希伪向量，不同标签近似正交，所以语义合并不额外合并——
归一化去重负责合并同名词。
"""

import json
import re

import pytest
from conftest import MOCK_EMBEDDER, MOCK_LLM

from tagplatform.services.generation import GenerationPolicy, GenerationSpec
from tagplatform.services.generation.open.mtg import keyframes as keyframes_mod
from tagplatform.services.generation.open.mtg import pipeline as mtg_pipeline
from tagplatform.services.generation.open.mtg.merge import MergedCandidate
from tagplatform.services.generation.open.producer import OpenTagProducer
from tagplatform.capabilities.llm.client import LLMResponse
from tagplatform.services.tags.library import try_load_library
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
    """固定评分的 judge 桩：按融合协议回三维分（id 键 + 逐维 {score, reason}）。

    echo_full=True 复现真实调用的坏习惯：把整行候选（含维度/依据）抄进
    tag 字段——对齐逻辑必须仍能匹配回候选。
    quote 缺省时从所报依据场景的场景行截一段原文（模拟"引用原文"）。
    scenes 显式给定时覆盖全部候选的引用场景；缺省引用候选行标注的依据
    场景（引擎按候选来源场景圈定 allowed_scenes，引到外面会被核验压分）。
    value_map 按标签名给 value 三方向分（队列排序用例用）。
    """

    def __init__(self, relevance=4, semantics=3, value=3, confidence=0.9,
                 scenes=None, omit: str | None = None, echo_full: bool = False,
                 quote: str | None = "", value_map: dict | None = None):
        self.relevance = relevance
        self.semantics = semantics
        self.value = value
        self.confidence = confidence
        self.scenes = scenes
        self.omit = omit
        self.echo_full = echo_full
        self.quote = quote
        self.value_map = value_map or {}
        self.calls = 0

    def complete(self, messages, run_id):
        self.calls += 1
        text = messages[0]["content"]
        candidates = text.partition("## 候选标签\n")[2].partition("## 影片内容\n")[0]
        rows = [line[2:] for line in candidates.splitlines()
                if line.startswith("- ") and "｜" in line]
        excerpts = dict(re.findall(r"^- (\S+) \[\d+-\d+\] (.+)$", text, re.MULTILINE))
        items = []
        for row in rows:
            tag = row.split("｜")[0]
            if tag == self.omit:
                continue
            dirs = re.search(r"(?:〔|｜)目录 ([^〕｜]+)", row)
            identifier = re.search(r"｜候选 ID ([^〕｜]+)", row)
            source = re.search(r"｜依据场景 ([^〕｜]+)", row)
            marked = source[1].split("、") if source else []
            scene_id = marked[0] if marked else next(iter(excerpts), "")
            cited = (self.scenes if self.scenes is not None
                     else marked or ([scene_id] if scene_id else []))
            quote = excerpts.get(scene_id, "")[:12] if self.quote == "" else self.quote
            val = self.value_map.get(tag, self.value)
            directions = (
                val if isinstance(val, dict)
                else {"presentation": val, "search": val, "recommendation": val}
            )
            items.append({
                **({"id": identifier[1]} if identifier else {}),
                "tag": row if self.echo_full else tag,
                "relevance": {"score": self.relevance, "reason": "桩判定"},
                "semantics": {"score": self.semantics, "reason": "桩判定"},
                "value": {key: {"score": score, "reason": "桩判定"}
                          for key, score in directions.items()},
                "confidence": self.confidence,
                "scenes": cited,
                "quote": quote,
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
        items = self.items(run_id) if callable(self.items) else self.items
        return LLMResponse(content=json.dumps(items, ensure_ascii=False),
                           usage={"prompt_tokens": 100, "completion_tokens": 20},
                           raw={}, ok=True)


# ---- 主链路 ----


def test_open_end_to_end(session, content_fixture, mtg_settings, tmp_path):
    """单场资产只生成场景级标签，judge 全过后输出带证据的候选。"""
    spec = _build_spec(session, content_fixture, run_id="r1")
    out = OpenTagProducer().produce(spec, session, MOCK_LLM, MOCK_EMBEDDER)

    assert len(out) == 1
    assert out[0].tag_name in {"多线叙事", "情感张力", "视听风格"}
    assert all(t.origin == "generated" and t.tag_id is None for t in out)
    assert all(t.confidence == 0.9 for t in out)
    assert all(t.candidate_type and t.candidate_description for t in out)
    assert all(t.evidence for t in out)
    scene_ids = {s.scene_id for s in content_fixture.content.scenes}
    assert all(e.scene_id in scene_ids for t in out for e in t.evidence)

    # 单场不具备全片级标签的跨场证据条件。
    report = json.loads((tmp_path / "audit" / "t-r1.json").read_text(encoding="utf-8"))
    assert report["version"] == OpenTagProducer.prompt_version
    assert [run["scope"] for run in report["runs"]] == ["scene"]
    assert {m["runs"] for m in report["merged"]} == {1}
    assert {tag.level for tag in out} == {"scene"}
    assert len(report["accepted"]) == 1
    assert report["clustering"]["input"] == 9
    assert len(report["clustering"]["clusters"]) == 1
    # judge 原始响应随审计落盘（引擎统一键族 batches）：整片归零时能分清
    # null / 漏字段 / 格式漂移。
    assert [row["attempt"] for row in report["judge_trace"]["batches"]] == [0]
    assert json.loads(report["judge_trace"]["batches"][0]["raw"])
    assert report["judge_trace"]["errors"] == {} and report["judge_trace"]["salvaged"] == 0


def test_open_judge_rejected_dropped(session, content_fixture, mtg_settings):
    """judge 给低 relevance 分（<3）→ 不进审核队列。"""
    spec = _build_spec(session, content_fixture, run_id="r2")
    out = OpenTagProducer().produce(
        spec, session, MOCK_LLM, MOCK_EMBEDDER,
        judge_client=_JudgeClient(relevance=1))
    assert out == []


def test_open_judge_null_score_dropped(session, content_fixture, mtg_settings):
    """judge 某维给 null（材料不足以区分相邻档）→ 按未过线淘汰，不猜分放行。"""
    spec = _build_spec(session, content_fixture, run_id="r2b")
    out = OpenTagProducer().produce(
        spec, session, MOCK_LLM, MOCK_EMBEDDER,
        judge_client=_JudgeClient(relevance=None))
    assert out == []


def test_open_judge_semantics_floor(session, content_fixture, mtg_settings):
    """semantics=2（名称边界模糊）< 3 → 淘汰；=3 保留。"""
    spec = _build_spec(session, content_fixture, run_id="r2c")
    out = OpenTagProducer().produce(
        spec, session, MOCK_LLM, MOCK_EMBEDDER,
        judge_client=_JudgeClient(semantics=2))
    assert out == []
    kept = OpenTagProducer().produce(
        _build_spec(session, content_fixture, run_id="r2d"),
        session, MOCK_LLM, MOCK_EMBEDDER,
        judge_client=_JudgeClient(semantics=3))
    assert len(kept) == 1


def test_open_judge_value_floor(session, content_fixture, mtg_settings):
    """加权聚合价值分 < 2.0 → 淘汰；>= 2.0 → 保留（支持两位小数阈值）。"""
    spec = _build_spec(session, content_fixture, run_id="r2e")
    out = OpenTagProducer().produce(
        spec, session, MOCK_LLM, MOCK_EMBEDDER,
        judge_client=_JudgeClient(value=1))
    assert out == []
    # content 权重 (0.30, 0.40, 0.30): (2, 2, 1) -> 1.70 < 2.0 淘汰
    out_float = OpenTagProducer().produce(
        _build_spec(session, content_fixture, run_id="r2e2",
                    policy=GenerationPolicy(dir_ids=["dir_theme"])),
        session, MOCK_LLM, MOCK_EMBEDDER,
        judge_client=_JudgeClient(
            value={"presentation": 2, "search": 2, "recommendation": 1}))
    assert out_float == []
    # content 权重 (0.30, 0.40, 0.30): (3, 2, 1) -> 2.00 >= 2.0 保留
    kept = OpenTagProducer().produce(
        _build_spec(session, content_fixture, run_id="r2f",
                    policy=GenerationPolicy(dir_ids=["dir_theme"])),
        session, MOCK_LLM, MOCK_EMBEDDER,
        judge_client=_JudgeClient(
            value={"presentation": 3, "search": 2, "recommendation": 1}))
    assert len(kept) == 1


def test_open_judge_low_confidence_not_dropped(session, content_fixture, mtg_settings):
    """judge 只按三维分/摘录/目录契合过滤，低 confidence 不再触发淘汰。"""
    spec = _build_spec(session, content_fixture, run_id="r3")
    low = OpenTagProducer().produce(
        spec, session, MOCK_LLM, MOCK_EMBEDDER,
        judge_client=_JudgeClient(confidence=0.2))
    assert len(low) == 1
    assert {t.confidence for t in low} == {0.2}

    kept = OpenTagProducer().produce(
        _build_spec(session, content_fixture, run_id="r3b"),
        session, MOCK_LLM, MOCK_EMBEDDER,
        judge_client=_JudgeClient(confidence=0.6))
    assert len(kept) == 1
    assert {t.confidence for t in kept} == {0.6}


def test_open_missing_verdict_dropped(session, content_fixture, mtg_settings):
    """judge 漏判某候选（omit）→ 该候选按未返回处理，淘汰。"""
    spec = _build_spec(session, content_fixture, run_id="r4")
    out = OpenTagProducer().produce(
        spec, session, MOCK_LLM, MOCK_EMBEDDER,
        judge_client=_JudgeClient(omit="情感张力"))
    assert len(out) == 1 and out[0].tag_name in {"多线叙事", "视听风格"}


def test_open_judge_echoing_full_line_still_matches(session, content_fixture,
                                                   mtg_settings):
    """judge 把整行候选抄进 tag（真实调用出现过）→ 仍能对齐，不误判"未返回"。"""
    spec = _build_spec(session, content_fixture, run_id="r10")
    out = OpenTagProducer().produce(
        spec, session, MOCK_LLM, MOCK_EMBEDDER,
        judge_client=_JudgeClient(echo_full=True))
    assert len(out) == 1
    assert out[0].tag_name in {"多线叙事", "情感张力", "视听风格"}


def test_open_judge_object_wrapped_output_still_parsed(session, content_fixture,
                                                       mtg_settings):
    """judge 把判决包成对象（{"verdicts":[...]} / 以标签为键）→ 仍能解析。"""
    class WrappedJudge(_JudgeClient):
        def __init__(self, keyed):
            super().__init__()
            self.keyed = keyed

        def complete(self, messages, run_id):
            response = super().complete(messages, run_id)
            verdicts = json.loads(response.content)
            payload = ({"verdicts": verdicts} if not self.keyed
                       else {row["id"]: row for row in verdicts})
            return LLMResponse(content=json.dumps(payload), usage=response.usage,
                               raw={}, ok=True)

    for keyed in (False, True):
        spec = _build_spec(session, content_fixture, run_id="r11")
        out = OpenTagProducer().produce(
            spec, session, MOCK_LLM, MOCK_EMBEDDER, judge_client=WrappedJudge(keyed))
        assert len(out) == 1


def test_open_scene_evidence_stays_with_source(session, content_fixture, mtg_settings):
    """场景级标签只挂原候选的证据场景。

    生成侧把逐场候选的 scenes 强制改写为请求场景（item 里另写的 scenes 被
    覆盖），judge 按候选行标注的依据场景引用——证据链两头都只认来源场景。
    """
    content = _scene_content(content_fixture, "tt0000003", 2)
    item = {"tag": "多线叙事", "dimension": "叙事",
            "scenes": ["tt0000003_story_001"], "reason": "生成侧"}
    generated = _FixedGenLLM(lambda run: [item] if ":scene-001-" in run else [])
    spec = _build_spec(session, content, "r12", GenerationPolicy(dir_ids=["dir_scene"]))
    out = OpenTagProducer().produce(
        spec, session, generated, MOCK_EMBEDDER, judge_client=_JudgeClient())
    assert len(out) == 1
    assert [e.scene_id for e in out[0].evidence] == ["tt0000003_story_000"]


def test_open_film_without_valid_evidence_dropped(session, content_fixture, mtg_settings):
    """全片生成无法引用两个真实场景，输出在进入 Judge 前淘汰。"""
    content = _scene_content(content_fixture, "tt0000005", 2)
    items = [{"tag": "空中楼阁", "dimension": "主题", "scenes": ["不存在_scene"]}]
    garbage = _FixedGenLLM(lambda run: items if ":film-dir-" in run else [])
    spec = _build_spec(session, content, "r5", GenerationPolicy(dir_ids=["dir_theme"]))
    judge = _JudgeClient()
    assert OpenTagProducer().produce(
        spec, session, garbage, MOCK_EMBEDDER, judge_client=judge) == []
    assert judge.calls == 0


def test_open_catalog_match_sets_catalog_origin(session, content_fixture, mtg_settings):
    """目录里有同名标签 → 按同名规则命中并落 origin=catalog。"""
    add_tags([{"tag_id": "tag_cat_1", "dir_id": "dir_theme",
               "tag_name": "多线叙事", "tag_type": "val_fixed",
               "description": "多线叙事", "status": "active"}])
    spec = _build_spec(session, content_fixture, run_id="r6")
    out = OpenTagProducer().produce(spec, session, MOCK_LLM, MOCK_EMBEDDER)
    hits = [t for t in out if t.origin == "catalog"]
    assert len(hits) == 1
    assert hits[0].tag_id == "tag_cat_1"
    assert isinstance(hits[0].relevance_score, float)
    assert -1.0 <= hits[0].relevance_score <= 1.0
    assert hits[0].candidate_type is None and hits[0].candidate_description is None


def test_open_respects_max_final_tags(session, content_fixture, mtg_settings):
    """任务显式填写的总上限真正截断多条产出（未设限时产出 >1 条）。"""
    content = _scene_content(content_fixture, "tt0000007", 2)
    full = OpenTagProducer().produce(
        _build_spec(session, content, run_id="r7-full",
                    policy=GenerationPolicy(max_tags=30)),
        session, MOCK_LLM, MOCK_EMBEDDER,
    )
    assert len(full) > 1

    capped = OpenTagProducer().produce(
        _build_spec(session, content, run_id="r7-cap",
                    policy=GenerationPolicy(max_tags=1)),
        session, MOCK_LLM, MOCK_EMBEDDER,
    )
    assert len(capped) == 1


# ---- 层级约束（内部把关 + 层级落库） ----


def test_open_film_scope_needs_two_scenes(session, content_fixture, mtg_settings):
    """全片级但只有单场支撑 → 淘汰；场景级单场支撑 → 保留，且层级随
    产出带出（film/scene 落库到 asset_tag.level）。"""
    content = _scene_content(content_fixture, "tt0000009", 2)
    spec = _build_spec(session, content, "rf", GenerationPolicy(dir_ids=["dir_theme"]))
    scenes = [s.scene_id for s in content.content.scenes]
    film_items = [
        {"tag": "道德堕落", "scenes": scenes[:1]},
        {"tag": "家族权力更迭", "scenes": scenes, "reason": "两场共同支撑"},
    ]
    llm = _FixedGenLLM(lambda run: film_items if ":film-dir-" in run else [
        {"tag": "开场旁白", "scope": "film", "reason": "局部手法"},
    ])
    out = OpenTagProducer().produce(spec, session, llm, MOCK_EMBEDDER,
                                    judge_client=_JudgeClient())
    assert {tag.tag_name: tag.level for tag in out} == {
        "开场旁白": "scene", "家族权力更迭": "film",
    }


def test_open_two_rounds_use_isolated_scenes_and_share_final_budget(
        session, content_fixture, mtg_settings, tmp_path, monkeypatch):
    content = _scene_content(content_fixture, "tt0000018", 3)
    for index, scene in enumerate(content.content.scenes):
        scene.description = f"仅属于场景{index}的独立识别文字"
    scenes = content.content.scenes
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"test frame")
    monkeypatch.setattr(keyframes_mod, "plan_scene_frames",
                        lambda *args, **kwargs: {scenes[0].scene_id: (frame,)})
    spec = _build_spec(session, content, "two-rounds", GenerationPolicy(max_tags=3, dir_ids=["dir_theme"]))
    calls = []

    class TwoRoundLLM:
        def complete(self, messages, run_id):
            parts = messages[0]["content"]
            text = parts[0]["text"]
            calls.append(run_id)
            if ":film-dir-" in run_id:
                assert content.asset.title in text
                assert all(scene.description in text for scene in scenes)
                context = json.loads(text.split("## 第一轮逐场候选\n")[1])
                assert [item["scene_id"] for item in context] == [s.scene_id for s in scenes]
                assert [len(item["tags"]) for item in context] == [1, 1, 0]
                assert context[0]["tags"][0]["tag"] == "开场冲突"
                assert context[0]["tags"][0]["reason"] == "场景理由"
                assert "最多 5 条" in parts[-1]["text"]
                items = [
                    {"tag": "伪跨场", "scope": "film", "scenes": [scenes[0].scene_id, "ghost"]},
                    *[{"tag": tag, "scope": "film", "scenes": [s.scene_id for s in scenes]}
                      for tag in ("秩序崩解", "人物蜕变", "超出预算")],
                ]
            elif ":scene-" in run_id:
                index = int(run_id.split(":scene-", 1)[1].split("-dir-", 1)[0]) - 1
                assert scenes[index].description in text
                assert content.asset.title in text
                assert content.content.summary in text
                assert all(s.description not in text for s in scenes if s != scenes[index])
                assert sum(p["type"] == "image_url" for p in parts) == (1 if index == 0 else 0)
                assert "最多 3 条" in parts[-1]["text"]
                tags = ["开场冲突", "孤立处境"]
                items = ([{"tag": tags[index], "scope": "scene",
                           "scenes": [scenes[index].scene_id], "reason": "场景理由"}]
                         if index < 2 else [])
            else:
                return MOCK_LLM.complete(messages, run_id)
            return LLMResponse(content=json.dumps({"tags": items}), usage={}, raw={}, ok=True)

    out = OpenTagProducer().produce(spec, session, TwoRoundLLM(), MOCK_EMBEDDER,
                                    judge_client=_JudgeClient())
    assert [call.rsplit(":", 1)[1] for call in calls[:4]] == [
        "scene-001-dir-001", "scene-002-dir-001", "scene-003-dir-001", "film-dir-001"]
    assert {(tag.tag_name, tag.level) for tag in out} == {
        ("秩序崩解", "film"), ("人物蜕变", "film"), ("超出预算", "film")}
    report = json.loads((tmp_path / "audit" / "t-two-rounds.json").read_text(encoding="utf-8"))
    assert [(run["scope"], run["calls"], run["items"]) for run in report["runs"]] == [
        ("scene", 3, 2), ("film", 1, 3)]


# ---- 队列治理：judge 引用原文 + value 排序 ----


def test_open_judge_without_quote_downgraded(session, content_fixture,
                                             mtg_settings):
    """judge 给高 relevance 分但引用不到原文 → 降档到 1（淘汰）。"""
    spec = _build_spec(session, content_fixture, run_id="rq1")
    out = OpenTagProducer().produce(
        spec, session, MOCK_LLM, MOCK_EMBEDDER,
        judge_client=_JudgeClient(quote="这句原文根本不在影片内容里"))
    assert out == []


def test_open_value_orders_queue(session, content_fixture, mtg_settings):
    """队列先按 judge 加权 value 精确小数分降序排：3.10 排在 2.80 前。"""
    content = _scene_content(content_fixture, "tt0000030", 2)
    spec = _build_spec(session, content, "rq2", GenerationPolicy(dir_ids=["dir_theme"]))
    scenes = [s.scene_id for s in content.content.scenes]
    items = [{"tag": tag, "desc": f"{tag}通用释义", "scenes": scenes} for tag in ("次要标签", "核心标签")]
    llm = _FixedGenLLM(lambda run: items if ":film-dir-" in run else [])
    out = OpenTagProducer().produce(
        spec, session, llm, MOCK_EMBEDDER,
        judge_client=_JudgeClient(value_map={
            # content 权重 0.30/0.40/0.30 -> 核心标签=3.10, 次要标签=2.80
            "核心标签": {"presentation": 3, "search": 4, "recommendation": 2},
            "次要标签": {"presentation": 4, "search": 2, "recommendation": 2},
        }))
    assert [tag.tag_name for tag in out] == ["核心标签", "次要标签"]


# ---- 失败语义 ----


def test_open_llm_failure_raises(session, content_fixture, mtg_settings):
    spec = _build_spec(session, content_fixture, run_id="r8")
    with pytest.raises(RuntimeError, match="LLM 调用失败"):
        OpenTagProducer().produce(spec, session, _FailingLLM(), MOCK_EMBEDDER)


def test_open_judge_parse_failure_raises(session, content_fixture, mtg_settings, tmp_path):
    """judge 两次都解析失败 → JudgeError（任务应 failed，不放行）。"""
    from tagplatform.services.generation.open.mtg.judge import JudgeError
    spec = _build_spec(session, content_fixture, run_id="r9")
    with pytest.raises(JudgeError):
        OpenTagProducer().produce(spec, session, MOCK_LLM, MOCK_EMBEDDER,
                                  judge_client=_GarbageJudge())

    audit = json.loads((tmp_path / "audit" / "t-r9.json").read_text(encoding="utf-8"))
    assert {row["tag"] for row in audit["merged"]} == {"多线叙事", "情感张力", "视听风格"}
    assert all(row["scope"] == "scene" for row in audit["merged"])
    # 引擎统一 trace 键族：整批重试一轮共 2 批，漏判逐候选落 errors（dict）。
    assert len(audit["judge_trace"]["batches"]) == 2
    assert len(audit["judge_trace"]["errors"]) == len(audit["merged"])


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


# ---- settings 透传（开放评测 per-run 副本的传递通路） ----


def test_run_open_generation_honors_explicit_settings(session, content_fixture,
                                                     mtg_settings, tmp_path):
    """显式传 run 级 settings 副本：审计重定向 + 文本模态，原报告目录零写入。

    开放评测给每次 run 传 `get_settings()` 的 model_copy（只改模态与审计
    目录），不得影响进程级配置——所以副本生效的同时，get_settings 指向
    的原报告目录必须一个字节都不写。
    """
    spec = _build_spec(session, content_fixture, run_id="r-explicit")
    run_settings = mtg_settings.model_copy(update={
        "mtg_modalities": "script,dialogue",
        "mtg_report_dir": str(tmp_path)})
    out = OpenTagProducer().produce(spec, session, MOCK_LLM, MOCK_EMBEDDER,
                                    settings=run_settings)

    assert len(out) == 1
    assert out[0].tag_name in {"多线叙事", "情感张力", "视听风格"}
    # 1) 审计恰一份，落在重定向目录（task_id 命名）。
    audits = list(tmp_path.glob("*.json"))
    assert [p.name for p in audits] == ["t-r-explicit.json"]
    # 2) 文本模态：视频帧相关字段为 0 / 只剩文本两模态。
    report = json.loads(audits[0].read_text(encoding="utf-8"))
    assert report["frames"] == 0
    assert report["params"]["modalities"] == ["dialogue", "script"]
    # 3) settings 原 mtg_report_dir（回退路径的目标目录）无新文件——
    #    连目录都不该建出来。
    audit_dir = tmp_path / "audit"
    assert not audit_dir.exists() or list(audit_dir.iterdir()) == []


def test_run_open_generation_settings_none_falls_back(session, content_fixture,
                                                      mtg_settings, tmp_path):
    """settings=None：回落 get_settings()，行为与现状一致。

    沿用 `mtg_settings` 夹具的 monkeypatch 风格：get_settings 返回定制
    settings，settings=None 时审计必须落进它的报告目录——证明回退被使用。
    """
    spec = _build_spec(session, content_fixture, run_id="r-fallback")
    out = mtg_pipeline.run_open_generation(spec, session, MOCK_LLM,
                                           MOCK_EMBEDDER, settings=None)

    assert len(out) == 1
    assert out[0].tag_name in {"多线叙事", "情感张力", "视听风格"}
    assert (tmp_path / "audit" / "t-r-fallback.json").exists()


# ---- 审计投影与版本（评审归一 Task 6：mock 路径）----


def test_mtg_version_bumped_for_unified_verdict():
    """评审归一接引擎：口径版本升 mtg-v15-unified-verdict。"""
    assert mtg_pipeline.MTG_VERSION == "mtg-v15-unified-verdict"
    assert OpenTagProducer.prompt_version == "mtg-v15-unified-verdict"


def test_mock_pipeline_audit_verdict_rows_are_flat_projection(
        session, content_fixture, mtg_settings, tmp_path):
    """mock 路径审计 verdicts 行 = 扁平投影：数值键保留（前端/漏斗不破），
    新增理由与核验键（relevance_reason/value_reasons/verify_fail 等）。"""
    spec = _build_spec(session, content_fixture, run_id="r15")
    OpenTagProducer().produce(spec, session, MOCK_LLM, MOCK_EMBEDDER)
    report = json.loads((tmp_path / "audit" / "t-r15.json").read_text(encoding="utf-8"))
    rows = report["verdicts"]
    assert rows
    row = rows[0]
    # 扁平数值键（open_report._judge_score_summary 与前端 JudgeVerdictRow 消费）
    assert isinstance(row["relevance"], int) and row["relevance"] == 4
    assert isinstance(row["semantics"], int) and row["semantics"] == 3
    assert isinstance(row["value"], float) and row["value"] >= 2.0
    assert set(row["value_scores"]) == {"presentation", "search", "recommendation"}
    # 新增键：逐维理由、价值理由、派生理由、核验标注与证据直通
    assert row["relevance_reason"] == "模拟内容评分"
    assert row["semantics_reason"] == "模拟语义评分"
    assert "内容呈现" in row["value_reasons"]
    assert row["verify_fail"] is None
    assert row["confidence"] == 0.9
    assert row["quote"] and row["reason"] and row["scenes"]
    assert row["tag_kind"] in {"content", "scene", "character"}
    assert isinstance(row["candidate_id"], str)


# ---- 小节 4 过滤段直测（构造终判 dict，钉淘汰原因映射）----


def _final_verdict(**overrides):
    """finalize_verdict 终判形状的最小构造（过滤段只读这些键）。"""
    verdict = {
        "relevance": {"score": 4, "reason": "内容强相关"},
        "semantics": {"score": 3, "reason": "语义清楚"},
        "value": {"score": 3.1, "reason": "价值汇总", "tag_kind": "content"},
        "quote": "原文摘录",
        "scenes": ["scene_001", "scene_002"],
        "confidence": 0.9,
        "reason": "派生理由",
        "verify_fail": None,
    }
    verdict.update(overrides)
    return verdict


def _merged(tag, key, scope="film", scenes=("scene_001", "scene_002")):
    return MergedCandidate(tag=tag, dimension=None, scenes=scenes, runs=1,
                           aliases=(tag,), reason="生成依据", scope=scope,
                           dir_raw="dir_theme", candidate_id=key)


def test_filter_drops_quote_fail_with_dedicated_reason(content_fixture, mtg_settings):
    """引擎核验压分（quote 失败）→ 专属淘汰原因，不吞成"未过线"。"""
    verdicts = {"c1": _final_verdict(
        verify_fail="quote",
        relevance={"score": 1, "reason": "未引用可核对原文，相关性高分缺少逐字依据"})}
    accepted, dropped = mtg_pipeline._filter_candidates(
        mtg_settings, content_fixture, verdicts, [_merged("标签甲", "c1")])
    assert accepted == []
    assert dropped[0]["why"] == "高相关分无逐字摘录支撑"


def test_filter_drops_scenes_fail_without_evidence(content_fixture, mtg_settings):
    verdicts = {"c1": _final_verdict(verify_fail="scenes", scenes=[])}
    accepted, dropped = mtg_pipeline._filter_candidates(
        mtg_settings, content_fixture, verdicts, [_merged("标签甲", "c1")])
    assert accepted == []
    assert dropped[0]["why"] == "无有效场景证据"


def test_filter_drops_film_single_cited_scene(content_fixture, mtg_settings):
    """film 级 verify_fail=="scenes" 且只剩单场 → 全片级单场支撑淘汰。"""
    verdicts = {"c1": _final_verdict(
        verify_fail="scenes",
        relevance={"score": 1, "reason": "有效引用场景不足，无法核验所给相关性高分"},
        scenes=["scene_001"])}
    accepted, dropped = mtg_pipeline._filter_candidates(
        mtg_settings, content_fixture, verdicts, [_merged("标签甲", "c1")])
    assert accepted == []
    assert dropped[0]["why"] == "全片级但只有单场支撑"


def test_filter_drops_score_below_with_axis_notes(content_fixture, mtg_settings):
    """未压分的低分（无 verify_fail）→ 未过线前缀 + 数值轴标注。"""
    verdicts = {"c1": _final_verdict(
        relevance={"score": 2, "reason": "偏题"}, verify_fail=None)}
    accepted, dropped = mtg_pipeline._filter_candidates(
        mtg_settings, content_fixture, verdicts, [_merged("标签甲", "c1")])
    assert accepted == []
    why = dropped[0]["why"]
    assert why.startswith("judge 评分未过线")
    assert "relevance=2 < 3" in why


def test_filter_annotates_null_axis_as_undecidable(content_fixture, mtg_settings):
    """None 轴（无法判断）同样走未过线前缀，并带「无法判断」标注。"""
    verdicts = {"c1": _final_verdict(
        relevance={"score": None, "reason": "材料缺失"}, verify_fail=None)}
    accepted, dropped = mtg_pipeline._filter_candidates(
        mtg_settings, content_fixture, verdicts, [_merged("标签甲", "c1")])
    assert accepted == []
    why = dropped[0]["why"]
    assert why.startswith("judge 评分未过线")
    assert "relevance=无法判断" in why


def test_filter_accepts_passing_scene_verdict(content_fixture, mtg_settings):
    """过线判决（证据场景真实）→ 接受，证据取自判决 scenes。"""
    verdicts = {"c1": _final_verdict(scenes=["scene_001"])}
    accepted, dropped = mtg_pipeline._filter_candidates(
        mtg_settings, content_fixture, verdicts,
        [_merged("标签甲", "c1", scope="scene", scenes=("scene_001",))])
    assert dropped == []
    candidate, verdict, evidence = accepted[0]
    assert candidate.candidate_id == "c1"
    assert [e.scene_id for e in evidence] == ["scene_001"]


def test_quote_fail_reason_counts_as_filter_stage_drop():
    """新专属淘汰原因必须进漏斗的过滤段分类，否则漏斗推导失真。"""
    assert mtg_pipeline.is_filter_stage_drop("高相关分无逐字摘录支撑") is True
    assert mtg_pipeline.is_filter_stage_drop("judge 评分未过线（relevance=无法判断）") is True
