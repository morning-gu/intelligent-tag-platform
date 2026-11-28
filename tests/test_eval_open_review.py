"""开放评测整片盲评（统一判决引擎封装）：行契约、批编排口径与客户端契约。

评审侧是 `mtg.verdicts.run_verdicts` 的第二消费者（Task 7）：解析/证据核验/
value 聚合/批编排全部由引擎承载，这里钉的是 open_review 的包装与行回填——
引擎行为本身已在 test_mtg_verdicts.py 钉死。行契约：成功行 = 终判各键 +
`evidence_scene_ids`（= scenes）+ `pass`（verdict_pass 复算）+ `evaluated`；
失败行 = 三维 {score: None, reason: 错误串}、pass None、evaluated False。
"""
from __future__ import annotations

import json

import pytest

from tagplatform.capabilities.llm import LLMResponse, MockLLMClient, OpenAIClient
from tagplatform.capabilities.prompts.open_review import REVIEW_PROMPT
from tagplatform.schemas import ContentJSON
from tagplatform.services.eval.open_review import (
    ReviewRequestError,
    make_review_client,
    review_calibre,
    review_movie_tags,
)
from tagplatform.settings import Settings

QUOTE = "我要逃离这个被安排好的世界"


def _content() -> ContentJSON:
    return ContentJSON.model_validate({
        "schema_version": "1.0",
        "asset": {"asset_id": "m1", "asset_type": "movie", "title": "逃离摄影棚",
                  "duration_ms": 60000},
        "content": {
            "summary": "一名察觉生活处处被安排的男子决意查出真相并逃离。",
            "characters": [{"character_id": "c1", "name": "陈晓",
                            "description": "察觉异常的男主角"}],
            "scenes": [{
                "scene_id": "scene_001",
                "time_range": {"start_ms": 0, "end_ms": 30000},
                "description": "陈晓在小镇街道晨跑，电台广播精准说出他的行踪",
                "dialogues": [{"speaker": "陈晓", "text": QUOTE + "。",
                               "start_ms": 1000, "end_ms": 2000}],
            }, {"scene_id": "scene_002",
                "time_range": {"start_ms": 30000, "end_ms": 60000},
                "description": "陈晓从摄影棚出口逃离"}],
        },
        "metadata": {"source": "test", "recognition_status": "done"},
    })


def _rows(count: int = 2) -> list[dict]:
    head = [
        {"id": "t0", "name": "逃离安排", "desc": "主角察觉生活被全程安排并决意逃离",
         "level": "film", "target_type": "content",
         "dir_path": "内容类标签/主题", "dir_description": "描述影片的核心主题",
         "scene_ids": [],
         "judge_reference": {"relevance": 4, "semantics": 3, "value": 3.0,
                             "reason": "judge 依据", "quote": QUOTE}},
        {"id": "t1", "name": "街道晨跑", "desc": "清晨在小镇街道跑步的情节",
         "level": "scene", "target_type": "scene",
         "dir_path": "补充类标签/场景桥段", "dir_description": "描述关键剧情桥段",
         "scene_ids": ["scene_001"],
         "judge_reference": {"relevance": 4, "semantics": 3, "value": 3.0,
                             "reason": "judge 依据", "quote": QUOTE}},
    ]
    tail = [{"id": f"t{i}", "name": f"标签{i}", "desc": f"第 {i} 条测试标签",
             "level": "film", "target_type": "content", "scene_ids": []} for i in range(2, count)]
    return (head + tail)[:count]


def _v(r=4, s=3, v=3, scenes=("scene_001", "scene_002"), quote=QUOTE,
       confidence=0.85, reason="台词有据"):
    """融合判决条目（VERDICT_OUTPUT_SPEC 形状）：无 pass、无总评 reason。

    value 为三方向对象（v 传 dict 时整体覆盖）；默认三方向各 3 分 → content
    聚合 3.0 ≥ 楼层 2，pass 由分数复算。
    """
    if isinstance(v, dict):
        value_block = v
    else:
        value_block = {
            "presentation": {"score": v, "reason": "呈现理由"},
            "search": {"score": v, "reason": "搜索理由"},
            "recommendation": {"score": v, "reason": "推荐理由"},
        }
    return {"relevance": {"score": r, "reason": reason},
            "semantics": {"score": s, "reason": "名称清晰"},
            "value": value_block,
            "quote": quote,
            "scenes": list(scenes),
            "confidence": confidence}


class StubClient:
    """按目标标签名返回整批固定判决（融合协议 {"verdicts": [...]}）。

    by_name 映射名→判决 dict（或 Exception 抛出 / None 漏判）；default
    兜底。handler 给出时优先（信封/截断/畸形用例）。记录每次调用消息。
    """

    def __init__(self, by_name=None, default=None, handler=None):
        self.by_name = by_name or {}
        self.default = default
        self.handler = handler
        self.calls: list[list[dict]] = []
        self.run_ids: list[str] = []

    def complete(self, messages, run_id):
        self.calls.append(messages)
        self.run_ids.append(run_id)
        if self.handler is not None:
            return self.handler(len(self.calls), messages)
        rows = []
        for target in _targets(messages):
            verdict = self.by_name.get(target["name"], self.default)
            if isinstance(verdict, Exception):
                raise verdict
            if verdict is not None:
                rows.append({"id": target["id"], **verdict})
        return json.dumps({"verdicts": rows}, ensure_ascii=False)


def _targets(messages):
    text = messages[1]["content"].rsplit("## 待评估标签\n", 1)[1]
    return json.JSONDecoder().raw_decode(text)[0]


class MainLLM:
    model = "main-model"


# ---- 请求形状 ----


def test_batch_message_has_full_labels_without_context_block():
    client = StubClient(default=_v())
    review_movie_tags(client, _content(), _rows(), run_id="r1:m1")
    assert len(client.calls) == 1
    assert client.run_ids == ["r1:m1:t0-t1"]
    assert client.calls[0][0]["content"] == REVIEW_PROMPT
    user = client.calls[0][1]["content"]
    # 材料在前、待评估标签视图在后，无「其他标签」上下文块（v14 瘦身）
    assert user.startswith("电影：《逃离摄影棚》")
    assert "- scene_001 [0-30000]" in user
    assert "该片其他标签（上下文）" not in user
    assert [target["id"] for target in _targets(client.calls[0])] == ["t0", "t1"]
    assert _targets(client.calls[0])[1]["scene_ids"] == ["scene_001"]
    assert "judge_reference" not in user


def test_format_retry_appends_hint_only_on_retry():
    seen: list[str] = []

    def handler(_i, messages):
        seen.append(messages[1]["content"])
        if len(seen) == 1:
            return "不是 JSON"
        return json.dumps({"verdicts": [{"id": "t0", **_v()}]}, ensure_ascii=False)

    review_movie_tags(StubClient(handler=handler), _content(), _rows(1), run_id="r1:m1")
    assert "上一响应缺少可用判决" not in seen[0]
    assert "上一响应缺少可用判决" in seen[1]


def test_derived_subset_request_carries_retry_hint_at_attempt_zero():
    """legacy 语义复现：整批之外的派生请求（拆批/漏判子集）attempt 0 也带 hint。"""
    seen: list[tuple[list[dict], str]] = []

    def handler(_i, messages):
        seen.append((_targets(messages), messages[1]["content"]))
        targets = _targets(messages)
        if len(targets) == 2:
            return json.dumps({"verdicts": [{"id": "t0", **_v()}]})
        return json.dumps({"verdicts": [{"id": targets[0]["id"], **_v()}]})

    review_movie_tags(StubClient(handler=handler), _content(), _rows(), run_id="r1:m1")
    assert "上一响应缺少可用判决" not in seen[0][1]
    assert [t["id"] for t in seen[1][0]] == ["t1"]
    assert "上一响应缺少可用判决" in seen[1][1]
    assert "该片其他标签（上下文）" not in seen[1][1]


# ---- 成功路径与行契约 ----


def test_normal_pass_recomputed_and_fail():
    client = StubClient(by_name={"逃离安排": _v(), "街道晨跑": _v(r=2, s=4)})
    rows = _rows()
    result = review_movie_tags(client, _content(), rows, run_id="r1:m1")
    assert result is rows
    assert result[0]["pass"] is True
    assert result[0]["relevance"] == {"score": 4, "reason": "台词有据"}
    assert result[0]["quote"] == QUOTE
    assert result[0]["confidence"] == 0.85
    assert result[0]["scenes"] == ["scene_001", "scene_002"]
    assert result[0]["evidence_scene_ids"] == ["scene_001", "scene_002"]
    assert result[1]["pass"] is False
    assert result[1]["relevance"]["score"] == 2


@pytest.mark.parametrize(("verdict", "expected"), [
    (_v(4, 3, 3), True),        # (4, 3, 3.0) → True
    (_v(2, 4, 3), False),       # relevance 低于楼层 3
    (_v(4, 2, 3), False),       # semantics 低于楼层 3
    (_v(4, 3, 0), False),       # value 聚合 0.0 低于楼层 2
    (_v(4, None, 3), None),     # semantics null（无法判断）→ None
])
def test_pass_recomputed_from_scores(verdict, expected):
    result = review_movie_tags(StubClient(default=verdict), _content(),
                               _rows(1), run_id="r1:m1")[0]
    assert result["pass"] is expected
    assert result["evaluated"] is True


def test_success_row_contract_keys_and_derived_reason():
    result = review_movie_tags(StubClient(default=_v()), _content(), _rows(1),
                               run_id="r1:m1")[0]
    assert set(result) >= {"relevance", "semantics", "value", "quote", "scenes",
                           "confidence", "reason", "evidence_scene_ids",
                           "pass", "evaluated"}
    assert "verify_fail" not in result
    assert result["value"]["score"] == 3.0
    assert result["value"]["tag_kind"] == "content"
    assert result["value"]["dimensions"]["presentation"] == {"score": 3, "reason": "呈现理由"}
    assert result["reason"] == ("相关性(4)：台词有据；语义(3)：名称清晰；"
                                "内容呈现(3)：呈现理由；内容搜索(3)：搜索理由；"
                                "内容推荐(3)：推荐理由")


def test_review_forwards_directory_and_aggregates_value_by_tag_kind():
    rows = [
        {"id": "t0", "name": "逃离安排", "desc": "通用释义", "level": "film",
         "target_type": "content", "dir_path": "内容类/主题", "dir_description": "主题定义",
         "scene_ids": []},
        {"id": "t1", "name": "街道晨跑", "desc": "通用释义", "level": "scene",
         "target_type": "scene", "dir_path": "补充类/场景", "dir_description": "场景定义",
         "scene_ids": ["scene_001"]},
        {"id": "t2", "name": "警觉逃亡者", "desc": "通用释义", "level": "scene",
         "target_type": "character", "dir_path": "补充类/角色", "dir_description": "角色定义",
         "scene_ids": ["scene_001"]},
    ]
    raw_value = {
        "presentation": {"score": 4, "reason": "看点鲜明"},
        "search": {"score": 2, "reason": "可检索"},
        "recommendation": {"score": 0, "reason": "无推荐语境"},
    }
    client = StubClient(default=_v(v=raw_value))
    out = review_movie_tags(client, _content(), rows, run_id="r1:m1")
    sent = _targets(client.calls[0])
    assert sent[0]["dir_path"] == "内容类/主题"
    assert sent[0]["dir_description"] == "主题定义"
    assert [item["target_type"] for item in sent] == ["content", "scene", "character"]
    # content: 0.30*4 + 0.40*2 + 0.30*0 = 2.0
    assert out[0]["value"]["score"] == 2.0
    assert out[0]["value"]["tag_kind"] == "content"
    assert out[0]["value"]["dimensions"]["presentation"] == {"score": 4, "reason": "看点鲜明"}
    # scene: 0.50*4 + 0.40*2 + 0.10*0 = 2.8
    assert out[1]["value"]["score"] == 2.8
    assert out[1]["value"]["tag_kind"] == "scene"
    # character: 0.55*4 + 0.30*2 + 0.15*0 = 2.8
    assert out[2]["value"]["score"] == 2.8
    assert out[2]["value"]["tag_kind"] == "character"


def test_legacy_scalar_value_is_protocol_error_and_retries_whole_batch():
    calls = {"n": 0}

    def handler(_i, messages):
        calls["n"] += 1
        targets = _targets(messages)
        if calls["n"] == 1:
            bad = {**_v(), "value": {"score": 3, "reason": "旧标量格式"}}
            return json.dumps({"verdicts": [{"id": targets[0]["id"], **bad}]})
        return json.dumps({"verdicts": [{"id": targets[0]["id"], **_v()}]})

    out = review_movie_tags(StubClient(handler=handler), _content(), _rows(1), run_id="r1:m1")
    assert calls["n"] == 2
    assert out[0]["evaluated"] is True
    assert out[0]["value"]["score"] == 3.0


def test_legacy_extra_fields_ignored_but_missing_scenes_presses():
    """旧协议遗留字段（pass/总评/evidence_scene_ids）被解析器忽略：不判漏，
    但 scenes 未按协议上报 → 场景计数核验失败压 1。"""
    legacy = {key: val for key, val in _v().items() if key != "scenes"}
    legacy.update({"pass": True, "reason": "模拟总评",
                   "evidence_scene_ids": ["scene_001", "scene_002"]})
    client = StubClient(by_name={"逃离安排": legacy})
    result = review_movie_tags(client, _content(), _rows(1), run_id="r1:m1")
    assert result[0]["evaluated"] is True
    assert result[0]["relevance"]["score"] == 1
    assert "引用场景不足" in result[0]["relevance"]["reason"]


def test_missing_desc_keeps_low_semantics_defect_but_nulls_high_score():
    rows = [
        {"id": "t0", "name": "不合目录词", "desc": None, "level": "film", "scene_ids": []},
        {"id": "t1", "name": "清晰词", "desc": "", "level": "film", "scene_ids": []},
    ]
    client = StubClient(by_name={"不合目录词": _v(s=1), "清晰词": _v(s=3)})
    out = review_movie_tags(client, _content(), rows, run_id="r1:m1")
    assert out[0]["semantics"]["score"] == 1
    assert out[1]["semantics"] == {"score": None, "reason": "缺少释义"}


def test_whitespace_only_desc_counts_as_missing_desc():
    """终审修复：legacy .strip() 语义——desc 仅空白 = 无释义（缺释义路径，
    open_review 恒传 semantics_null_without_desc=True），semantics 高分置 null
    而非因非空字符串被当作「有释义」放行。"""
    row = {"id": "t0", "name": "空白释义", "desc": "   ", "level": "film",
           "scene_ids": []}
    result = review_movie_tags(StubClient(by_name={"空白释义": _v(s=3)}),
                               _content(), [row], run_id="r1:m1")[0]
    assert result["evaluated"] is True
    assert result["semantics"] == {"score": None, "reason": "缺少释义"}


# ---- 证据核验（引擎复算在行上的投影）----


def test_film_single_valid_scene_presses_relevance_to_one():
    client = StubClient(default=_v(scenes=["scene_001", "scene_001", "invented"]))
    result = review_movie_tags(client, _content(), _rows(1), run_id="r1:m1")[0]
    assert result["evaluated"] is True
    assert result["relevance"]["score"] == 1
    assert "引用场景不足" in result["relevance"]["reason"]
    assert result["scenes"] == ["scene_001"]
    assert result["evidence_scene_ids"] == ["scene_001"]
    assert result["pass"] is False


def test_quote_not_in_material_presses_relevance_to_one():
    client = StubClient(default=_v(quote="完全不存在于材料里的逐字摘录"))
    result = review_movie_tags(client, _content(), _rows(1), run_id="r1:m1")[0]
    assert result["relevance"]["score"] == 1
    assert "未引用" in result["relevance"]["reason"]
    assert result["reason"] == result["relevance"]["reason"]
    assert result["pass"] is False


def test_scene_evidence_outside_target_scene_ids_presses_relevance():
    client = StubClient(default=_v(scenes=["scene_002"]))
    result = review_movie_tags(client, _content(), [_rows()[1]], run_id="r1:m1")[0]
    assert result["relevance"]["score"] == 1
    assert result["evidence_scene_ids"] == []
    assert result["pass"] is False


def test_scene_level_empty_scene_ids_falls_back_to_haystack_no_count_check():
    row = {"id": "t0", "name": "无来源场景", "desc": "通用释义", "level": "scene",
           "scene_ids": []}
    client = StubClient(default=_v())
    result = review_movie_tags(client, _content(), [row], run_id="r1:m1")[0]
    assert result["relevance"]["score"] == 4
    assert result["evidence_scene_ids"] == []
    assert result["pass"] is True


def test_low_or_null_relevance_skips_verification():
    result = review_movie_tags(StubClient(default=_v(r=2, quote="完全不存在")),
                               _content(), _rows(1), run_id="r1:m1")[0]
    assert result["relevance"]["score"] == 2


def test_honors_modalities():
    client = StubClient(default=_v(quote="陈晓在小镇街道晨跑"))
    result = review_movie_tags(client, _content(), _rows(1), run_id="r1:m1",
                               modalities=("script",))
    assert result[0]["pass"] is True
    assert "台词：" not in client.calls[0][1]["content"]


def test_empty_material_raises():
    with pytest.raises(ReviewRequestError, match="评审材料为空"):
        review_movie_tags(StubClient(default=_v()), _content(), _rows(),
                          run_id="r1:m1", modalities=("dialogue",))


# ---- 漏判 / 失败语义 ----


def test_partial_miss_marks_none():
    client = StubClient(by_name={"逃离安排": _v(), "街道晨跑": None})
    result = review_movie_tags(client, _content(), _rows(), run_id="r1:m1")
    assert result[0]["pass"] is True
    assert result[1]["pass"] is None
    assert result[1]["relevance"]["score"] is None
    assert result[1]["evaluated"] is False
    assert result[1]["evidence_scene_ids"] == []
    # 空数组载荷的错误文案族位移（引擎 parse_verdicts 出串，见 run_verdicts）
    assert result[1]["relevance"]["reason"] == "评审响应无法解析：审核输出为空数组"
    assert result[1]["reason"] == result[1]["relevance"]["reason"]


def test_explicit_null_semantics_is_not_missed_but_pass_is_none():
    result = review_movie_tags(StubClient(default=_v(s=None)), _content(),
                               _rows(1), run_id="r1:m1")[0]
    assert result["evaluated"] is True
    assert result["semantics"]["score"] is None
    assert result["pass"] is None


def test_all_missed_raises():
    with pytest.raises(ReviewRequestError):
        review_movie_tags(StubClient(default=None), _content(), _rows(), run_id="r1:m1")


def test_truncated_batch_splits_without_context_block_and_keeps_partial():
    def handler(attempt, messages):
        targets = _targets(messages)
        if len(targets) == 2:
            return LLMResponse("", {}, {"choices": [{"finish_reason": "length"}]}, True)
        assert "该片其他标签（上下文）" not in messages[1]["content"]
        target = targets[0]
        if target["id"] == "t1":
            raise RuntimeError("分批调用失败")
        return json.dumps({"verdicts": [{"id": target["id"], **_v()}]})

    client = StubClient(handler=handler)
    result = review_movie_tags(client, _content(), _rows(), run_id="r1:m1")
    assert len(client.calls) == 3
    assert result[0]["pass"] is True
    assert result[1]["evaluated"] is False


def test_call_failure_envelope_records_error_without_retry():
    client = StubClient(handler=lambda *_: LLMResponse(
        "", {}, {"error": "maximum context length exceeded"}, False))
    with pytest.raises(ReviewRequestError, match="评审调用失败"):
        review_movie_tags(client, _content(), _rows(), run_id="r1:m1")
    assert len(client.calls) == 1


def test_duplicate_id_never_accepted_even_if_it_appears_three_times():
    client = StubClient(handler=lambda *_: json.dumps({"verdicts": [{"id": "t0", **_v()}] * 3}))
    with pytest.raises(ReviewRequestError):
        review_movie_tags(client, _content(), _rows(1), run_id="r1:m1")


def test_truncated_single_tag_gets_one_single_retry_then_raises():
    client = StubClient(handler=lambda i, m: LLMResponse(
        "", {}, {"choices": [{"finish_reason": "length"}]}, ok=True))
    with pytest.raises(ReviewRequestError):
        review_movie_tags(client, _content(), _rows(1), run_id="r1:m1")
    assert len(client.calls) == 2


def test_format_retry_then_ok():
    state = {"n": 0}

    def handler(i, m):
        state["n"] += 1
        if state["n"] == 1:
            return "不是 JSON"
        return json.dumps({"verdicts": [{"id": "t0", **_v()}]}, ensure_ascii=False)

    result = review_movie_tags(StubClient(handler=handler), _content(),
                               _rows(1), run_id="r1:m1")
    assert result[0]["pass"] is True
    assert state["n"] == 2  # 重试一次


def test_structural_failure_splits_the_batch():
    """JSON 能解析但判决不合协议：重试一次后拆批，而不是整片评审失败。"""

    def handler(_i, messages):
        targets = _targets(messages)
        if len(targets) > 1:
            return json.dumps({"verdicts": [{"id": t["id"], "pass": True} for t in targets]})
        return json.dumps({"verdicts": [{"id": targets[0]["id"], **_v()}]})

    client = StubClient(handler=handler)
    result = review_movie_tags(client, _content(), _rows(), run_id="r1:m1")
    assert len(client.calls) == 4  # 整批两次 + 两个单条批各一次
    assert [row["evaluated"] for row in result] == [True, True]


def test_partial_verdicts_resend_only_the_missing_labels():
    def handler(_i, messages):
        targets = _targets(messages)
        if len(targets) > 1:
            return json.dumps({"verdicts": [{"id": "t0", **_v()}, {"id": "t1", "pass": True}]})
        assert [t["id"] for t in targets] == ["t1"]
        return json.dumps({"verdicts": [{"id": "t1", **_v()}]})

    client = StubClient(handler=handler)
    result = review_movie_tags(client, _content(), _rows(), run_id="r1:m1")
    assert len(client.calls) == 2
    assert [row["evaluated"] for row in result] == [True, True]


def test_transport_failure_never_splits():
    client = StubClient(default=RuntimeError("端点宕机"))
    with pytest.raises(ReviewRequestError, match="评审调用失败"):
        review_movie_tags(client, _content(), _rows(4), run_id="r1:m1")
    assert len(client.calls) == 1


def test_call_budget_caps_runaway_splitting():
    client = StubClient(handler=lambda *_: json.dumps({"verdicts": [{"id": "t0"}]}))
    with pytest.raises(ReviewRequestError):
        review_movie_tags(client, _content(), _rows(4), run_id="r1:m1")
    assert len(client.calls) <= 2 * 4 + 2


def test_every_singleton_gets_its_first_attempt_before_any_singleton_retry():
    attempted = []

    def handler(_index, messages):
        targets = _targets(messages)
        if len(targets) != 1:
            return '{"verdicts":[]}'
        target_id = targets[0]["id"]
        attempted.append(target_id)
        if attempted.count(target_id) == 1:
            return '{"verdicts":[]}'
        return json.dumps({"verdicts": [{"id": target_id, **_v()}]})

    trace = {}
    client = StubClient(handler=handler)
    rows = review_movie_tags(client, _content(), _rows(8), run_id="r1:m1", trace=trace)
    assert len(client.calls) == 18
    assert attempted[:8] == [f"t{i}" for i in range(8)]
    assert sum(row["evaluated"] for row in rows) == 2
    assert trace["errors"]["t7"] == "评审调用预算耗尽，未发出恢复请求"


def test_all_singletons_can_recover_within_budget():
    def handler(_index, messages):
        targets = _targets(messages)
        return json.dumps({"verdicts": [{"id": target["id"], **_v()}
                                        for target in targets if len(targets) == 1]})

    client = StubClient(handler=handler)
    rows = review_movie_tags(client, _content(), _rows(8), run_id="r1:m1")
    assert all(row["evaluated"] for row in rows)
    assert len(client.calls) <= 18


def test_trace_keeps_raw_responses_and_failure_reasons():
    trace: dict = {}
    client = StubClient(handler=lambda *_: "不是 JSON")
    with pytest.raises(ReviewRequestError):
        review_movie_tags(client, _content(), _rows(1), run_id="r1:m1", trace=trace)
    assert [row["raw"] for row in trace["batches"]] == ["不是 JSON", "不是 JSON"]
    assert trace["errors"]["t0"].startswith("评审响应无法解析")
    assert trace["salvaged"] == 0


def test_trace_records_successful_batches_too():
    trace: dict = {}
    review_movie_tags(StubClient(default=_v()), _content(), _rows(1),
                      run_id="r1:m1", trace=trace)
    assert len(trace["batches"]) == 1
    assert trace["batches"][0]["error"] is None
    assert trace["batches"][0]["missing"] == []
    assert json.loads(trace["batches"][0]["raw"])["verdicts"]
    assert trace["errors"] == {} and trace["salvaged"] == 0


# ---- mock 客户端路径（MockLLMClient._quality_output 咬合）----


def test_mock_client_review_path_produces_evaluated_rows():
    rows = _rows()
    result = review_movie_tags(MockLLMClient(), _content(), rows, run_id="r1:m1")
    assert all(row["evaluated"] for row in result)
    assert result[0]["pass"] is True
    assert result[0]["relevance"]["score"] == 4
    assert result[0]["quote"]
    assert result[0]["evidence_scene_ids"] == ["scene_001", "scene_002"]
    assert result[1]["pass"] is True


def test_mock_client_without_scenes_keeps_relevance_null():
    content = ContentJSON.model_validate({
        "schema_version": "1.0",
        "asset": {"asset_id": "m2", "asset_type": "movie", "title": "空场景",
                  "duration_ms": 60000},
        "content": {"summary": "只有摘要没有场景。", "characters": [], "scenes": []},
        "metadata": {"source": "test", "recognition_status": "done"},
    })
    row = {"id": "t0", "name": "摘要标签", "desc": "通用释义", "level": "film",
           "scene_ids": []}
    result = review_movie_tags(MockLLMClient(), content, [row], run_id="r1:m2")[0]
    assert result["evaluated"] is True
    assert result["relevance"]["score"] is None
    assert result["value"]["score"] == 3.0
    assert result["pass"] is None


def test_empty_rows_returns_empty():
    assert review_movie_tags(StubClient(), _content(), [], run_id="r1:m1") == []


# ---- 客户端工厂与评审口径 ----


def test_make_review_client_same_model_uses_low_and_output_cap():
    settings = Settings(use_mock_llm=False, llm_model="main-model")
    client = make_review_client(settings, MainLLM())
    assert client.model == "main-model"
    assert client.extra_body["reasoning_effort"] == "low"
    assert client.max_tokens == 8192


def test_make_review_client_mock_and_zero_network_reuse_llm():
    llm = MockLLMClient()
    assert make_review_client(Settings(use_mock_llm=True,
                                       open_eval_review_model="review-x"), llm) is llm
    assert make_review_client(Settings(open_eval_review_model="review-x"), llm) is llm


def test_make_review_client_builds_independent_client():
    settings = Settings(open_eval_review_model="review-x",
                        open_eval_review_base_url_env="REVIEW_BASE",
                        open_eval_review_api_key_env="REVIEW_KEY",
                        llm_base_url_env="MAIN_BASE", llm_api_key_env="MAIN_KEY",
                        llm_request_interval=0.7)
    client = make_review_client(settings, MainLLM())
    assert isinstance(client, OpenAIClient)
    assert client.model == "review-x"
    assert client.base_url_env == "REVIEW_BASE"
    assert client.api_key_env == "REVIEW_KEY"
    assert client.temperature == 0.0
    assert client.max_tokens == 8192
    assert client.request_interval == 0.7
    assert client.extra_body == {"reasoning_effort": "low"}


def test_make_review_client_reasoning_effort_empty_omits_extra_body():
    settings = Settings(open_eval_review_model="review-x",
                        open_eval_review_reasoning_effort="")
    client = make_review_client(settings, MainLLM())
    assert client.extra_body == {}


def test_make_review_client_env_names_fall_back_to_main():
    settings = Settings(open_eval_review_model="review-x")
    client = make_review_client(settings, MainLLM())
    assert client.base_url_env == settings.llm_base_url_env
    assert client.api_key_env == settings.llm_api_key_env


def test_review_calibre_independent_model_threshold_mode():
    settings = Settings(open_eval_review_model="review-x", llm_model="main-model")
    calibre = review_calibre(settings, MainLLM())
    assert calibre["model"] == "review-x" and calibre["same_model"] is False
    assert calibre["pass_mode"] == "threshold"


def test_review_calibre_same_name_is_same_model():
    settings = Settings(open_eval_review_model="main-model", llm_model="main-model")
    assert review_calibre(settings, MainLLM())["same_model"] is True
