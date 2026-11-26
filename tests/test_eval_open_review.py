"""开放评测整片盲评、拆批、证据和客户端契约。"""
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


def _v(r=4, s=3, v=3, evidence=("scene_001", "scene_002"), reason="台词有据", decision=True, overall="提供独立检索价值，建议保留"):
    if isinstance(v, dict):
        value_block = v
    else:
        value_block = {
            "presentation": {"score": v, "reason": "呈现理由"},
            "search": {"score": v, "reason": "搜索理由"},
            "recommendation": {"score": v, "reason": "推荐理由"},
        }
    return {"pass": decision, "reason": overall,
            "relevance": {"score": r, "reason": reason},
            "semantics": {"score": s, "reason": "名称清晰"},
            "value": value_block,
            "evidence_scene_ids": list(evidence)}


class StubClient:
    """按目标标签名返回整批固定判决。

    by_name 映射名→判决 dict（或 Exception 抛出 / None 漏判）；default
    兜底。handler 给出时优先（信封/截断用例）。记录每次调用消息。
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
        return json.dumps({"reviews": rows}, ensure_ascii=False)


def _targets(messages):
    text = messages[1]["content"].rsplit("## 待评估标签\n", 1)[1]
    return json.JSONDecoder().raw_decode(text)[0]


class MainLLM:
    model = "main-model"


# ---- 逐条消息形状 ----


def test_batch_message_has_full_labels_without_judge_reference():
    client = StubClient(default=_v())
    review_movie_tags(client, _content(), _rows(), run_id="r1:m1")
    assert len(client.calls) == 1
    assert [target["id"] for target in _targets(client.calls[0])] == ["t0", "t1"]
    assert _targets(client.calls[0])[1]["scene_ids"] == ["scene_001"]
    assert "judge_reference" not in client.calls[0][1]["content"]


def test_normal_pass_and_fail():
    client = StubClient(by_name={"逃离安排": _v(), "街道晨跑": _v(r=1, decision=False)})
    rows = _rows()
    result = review_movie_tags(client, _content(), rows, run_id="r1:m1")
    assert result is rows
    assert result[0]["pass"] is True
    assert result[0]["relevance"] == {"score": 4, "reason": "台词有据"}
    assert result[1]["pass"] is False
    assert result[1]["relevance"]["score"] == 1


@pytest.mark.parametrize(("verdict", "expected"), [
    (_v(4, 4, 4, decision=False, overall="同组标签已完整表达该信息"), False),
    (_v(2, 2, 1, decision=True), True),
    (_v(4, 4, 4, decision=None, overall="无法确认用途"), None),
])
def test_model_decision_and_overall_reason_are_independent_of_scores(verdict, expected):
    result = review_movie_tags(StubClient(default=verdict), _content(),
                               _rows(1), run_id="r1:m1")[0]
    assert result["pass"] is expected
    assert result["reason"] == verdict["reason"]
    assert result["relevance"] == verdict["relevance"]


@pytest.mark.parametrize("field", ["pass", "reason"])
def test_missing_decision_or_overall_reason_is_not_inferred(field):
    incomplete = _v()
    incomplete.pop(field)
    client = StubClient(by_name={"街道晨跑": incomplete}, default=_v())
    rows = review_movie_tags(client, _content(), _rows(), run_id="r1:m1")
    assert rows[1]["evaluated"] is False and rows[1]["pass"] is None


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


def test_review_rejects_legacy_scalar_value_and_retries():
    calls = {"n": 0}

    def handler(_i, messages):
        calls["n"] += 1
        targets = _targets(messages)
        if calls["n"] == 1:
            bad = {**_v(), "value": {"score": 3, "reason": "旧标量格式"}}
            return json.dumps({"reviews": [{"id": targets[0]["id"], **bad}]})
        return json.dumps({"reviews": [{"id": targets[0]["id"], **_v()}]})

    out = review_movie_tags(StubClient(handler=handler), _content(), _rows(1), run_id="r1:m1")
    assert calls["n"] == 2
    assert out[0]["evaluated"] is True
    assert out[0]["value"]["score"] == 3.0


def test_missing_desc_keeps_low_semantics_defect_but_nulls_high_score():
    rows = [
        {"id": "t0", "name": "不合目录词", "desc": None, "level": "film", "scene_ids": []},
        {"id": "t1", "name": "清晰词", "desc": "", "level": "film", "scene_ids": []},
    ]
    client = StubClient(by_name={"不合目录词": _v(s=1), "清晰词": _v(s=3)})
    out = review_movie_tags(client, _content(), rows, run_id="r1:m1")
    assert out[0]["semantics"]["score"] == 1
    assert out[1]["semantics"] == {"score": None, "reason": "缺少释义"}


def test_film_high_relevance_requires_two_valid_distinct_scenes():
    client = StubClient(default=_v(evidence=["scene_001", "scene_001", "invented"]))
    result = review_movie_tags(client, _content(), _rows(1), run_id="r1:m1")[0]
    assert result["evaluated"] is True and result["pass"] is True
    assert result["relevance"]["score"] is None
    assert result["evidence_scene_ids"] == ["scene_001"]


def test_scene_evidence_must_be_in_target_scene_ids():
    client = StubClient(default=_v(evidence=["scene_002"]))
    result = review_movie_tags(client, _content(), [_rows()[1]], run_id="r1:m1")[0]
    assert result["pass"] is True
    assert result["relevance"]["score"] is None
    assert result["evidence_scene_ids"] == []


def test_rationale_supplies_full_valid_scene_ids_when_structured_field_is_incomplete():
    client = StubClient(default=_v(
        evidence=["scene_001"], reason="在scene_001确认起因", overall="scene_002体现结果，建议保留"))
    result = review_movie_tags(client, _content(), _rows(1), run_id="r1:m1")[0]
    assert result["evidence_scene_ids"] == ["scene_001", "scene_002"]
    assert result["relevance"]["score"] == 4


def test_rationale_cannot_supply_wrong_scene_or_a_partial_id():
    client = StubClient(default=_v(
        evidence=[], reason="scene_002 和 scene_001_extra", overall="引用了另一场"))
    result = review_movie_tags(client, _content(), [_rows()[1]], run_id="r1:m1")[0]
    assert result["evidence_scene_ids"] == []
    assert result["relevance"]["score"] is None
    assert "scene_001_extra" in result["relevance"]["reason"]
    assert result["pass"] is True


def test_honors_modalities():
    client = StubClient(default=_v())
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


def test_explicit_null_is_not_missed():
    result = review_movie_tags(StubClient(default=_v(s=None)), _content(),
                               _rows(1), run_id="r1:m1")
    assert result[0]["pass"] is True and result[0]["evaluated"] is True
    assert result[0]["semantics"]["score"] is None


def test_all_missed_raises():
    with pytest.raises(ReviewRequestError):
        review_movie_tags(StubClient(default=None), _content(), _rows(), run_id="r1:m1")


def test_truncated_batch_splits_with_full_context_and_keeps_partial_results():
    def handler(attempt, messages):
        targets = _targets(messages)
        if len(targets) == 2:
            return LLMResponse("", {}, {"choices": [{"finish_reason": "length"}]}, True)
        assert "该片其他标签（上下文）" in messages[1]["content"]
        assert all(label["name"] in messages[1]["content"] for label in _rows())
        target = targets[0]
        if target["id"] == "t1":
            raise RuntimeError("分批调用失败")
        return json.dumps({"reviews": [{"id": target["id"], **_v()}]})

    client = StubClient(handler=handler)
    result = review_movie_tags(client, _content(), _rows(), run_id="r1:m1")
    assert len(client.calls) == 3
    assert result[0]["pass"] is True
    assert result[1]["evaluated"] is False


def test_input_limit_does_not_repeat_the_same_oversized_material():
    client = StubClient(handler=lambda *_: LLMResponse(
        "", {}, {"error": "maximum context length exceeded"}, False))
    with pytest.raises(ReviewRequestError, match="影片评审材料超出模型上下文"):
        review_movie_tags(client, _content(), _rows(), run_id="r1:m1")
    assert len(client.calls) == 1


def test_duplicate_id_never_accepted_even_if_it_appears_three_times():
    client = StubClient(handler=lambda *_: json.dumps({"reviews": [{"id": "t0", **_v()}] * 3}))
    with pytest.raises(ReviewRequestError):
        review_movie_tags(client, _content(), _rows(1), run_id="r1:m1")


def test_all_transport_failure_raises():
    with pytest.raises(ReviewRequestError, match="评审调用失败"):
        review_movie_tags(StubClient(default=RuntimeError("端点宕机")),
                          _content(), _rows(), run_id="r1:m1")


def test_truncated_single_tag_missed():
    client = StubClient(handler=lambda i, m: LLMResponse(
        "", {}, {"choices": [{"finish_reason": "length"}]}, ok=True))
    with pytest.raises(ReviewRequestError):
        review_movie_tags(client, _content(), _rows(1), run_id="r1:m1")


def test_format_retry_then_ok():
    state = {"n": 0}

    def handler(i, m):
        state["n"] += 1
        if state["n"] == 1:
            return "不是 JSON"
        return json.dumps({"reviews": [{"id": "t0", **_v()}]}, ensure_ascii=False)

    result = review_movie_tags(StubClient(handler=handler), _content(),
                               _rows(1), run_id="r1:m1")
    assert result[0]["pass"] is True
    assert state["n"] == 2  # 重试一次


def test_structural_failure_splits_the_batch():
    """JSON 能解析但判决不合协议：重试一次后拆批，而不是整片评审失败。"""

    def handler(_i, messages):
        targets = _targets(messages)
        if len(targets) > 1:
            return json.dumps({"reviews": [{"id": t["id"], "pass": True} for t in targets]})
        return json.dumps({"reviews": [{"id": targets[0]["id"], **_v()}]})

    client = StubClient(handler=handler)
    result = review_movie_tags(client, _content(), _rows(), run_id="r1:m1")
    assert len(client.calls) == 4  # 整批两次 + 两个单条批各一次
    assert [row["evaluated"] for row in result] == [True, True]


def test_partial_verdicts_resend_only_the_missing_labels():
    def handler(_i, messages):
        targets = _targets(messages)
        if len(targets) > 1:
            return json.dumps({"reviews": [{"id": "t0", **_v()}, {"id": "t1", "pass": True}]})
        assert [t["id"] for t in targets] == ["t1"]
        return json.dumps({"reviews": [{"id": "t1", **_v()}]})

    client = StubClient(handler=handler)
    result = review_movie_tags(client, _content(), _rows(), run_id="r1:m1")
    assert len(client.calls) == 2
    assert [row["evaluated"] for row in result] == [True, True]


def test_transport_failure_never_splits():
    client = StubClient(default=RuntimeError("端点宕机"))
    with pytest.raises(ReviewRequestError):
        review_movie_tags(client, _content(), _rows(4), run_id="r1:m1")
    assert len(client.calls) == 1


def test_call_budget_caps_runaway_splitting():
    client = StubClient(handler=lambda *_: json.dumps({"reviews": [{"id": "t0"}]}))
    with pytest.raises(ReviewRequestError):
        review_movie_tags(client, _content(), _rows(4), run_id="r1:m1")
    assert len(client.calls) <= 2 * 4 + 2


def test_every_singleton_gets_its_first_attempt_before_any_singleton_retry():
    attempted = []

    def handler(_index, messages):
        targets = _targets(messages)
        if len(targets) != 1:
            return '{"reviews":[]}'
        target_id = targets[0]["id"]
        attempted.append(target_id)
        if attempted.count(target_id) == 1:
            return '{"reviews":[]}'
        return json.dumps({"reviews": [{"id": target_id, **_v()}]})

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
        return json.dumps({"reviews": [{"id": target["id"], **_v()}
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


def test_trace_records_successful_batches_too():
    trace: dict = {}
    review_movie_tags(StubClient(default=_v()), _content(), _rows(1),
                      run_id="r1:m1", trace=trace)
    assert len(trace["batches"]) == 1
    assert json.loads(trace["batches"][0]["raw"])["reviews"]
    assert "errors" not in trace


# ---- 实时回调 ----


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


def test_review_calibre_independent_model():
    settings = Settings(open_eval_review_model="review-x", llm_model="main-model")
    calibre = review_calibre(settings, MainLLM())
    assert calibre["model"] == "review-x" and calibre["same_model"] is False


def test_review_calibre_same_name_is_same_model():
    settings = Settings(open_eval_review_model="main-model", llm_model="main-model")
    assert review_calibre(settings, MainLLM())["same_model"] is True
