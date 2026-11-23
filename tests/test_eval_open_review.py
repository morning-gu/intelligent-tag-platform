"""eval/open_review 单测：逐条并发三维评审的解析纪律与摘录必核。

被测层（open-review-v3）逐条并发评审：每条请求携带「该片全部标签」兄弟
清单作上下文（保留跨标签视野），「待评估标签」只放目标一条。测试用按
标签名回 canned 判决的替身钉死：

1. 逐条消息形状：兄弟清单含全部名、待评估标签恰一条、judge_reference 附上；
2. pass 由三维分按阈值推导；摘录必核（高相关分核不中降档）；
3. 漏判语义：单条漏判 pass=None（排除出分母），全部漏判/全部调用失败才
   ReviewRequestError（评审失败 ≠ 0 分，产出不作废）；单条失败不拖累其余；
4. on_result 每条完成回调（实时报告用）；
5. 客户端工厂 / 评审口径 / settings 默认值。
"""
from __future__ import annotations

import json

import pytest

from tagplatform.capabilities.llm import LLMResponse, MockLLMClient, OpenAIClient
from tagplatform.capabilities.prompts.open_review import REVIEW_PROMPT
from tagplatform.schemas import ContentJSON
from tagplatform.services.eval.open_review import (
    REVIEW_PASS_FLOORS,
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
            }],
        },
        "metadata": {"source": "test", "recognition_status": "done"},
    })


def _rows(count: int = 2) -> list[dict]:
    head = [
        {"id": "t0", "name": "逃离安排", "desc": "主角察觉生活被全程安排并决意逃离",
         "level": "film", "scenes": []},
        {"id": "t1", "name": "街道晨跑", "desc": "清晨在小镇街道跑步的情节",
         "level": "scene", "scenes": ["scene_001"]},
    ]
    tail = [{"id": f"t{i}", "name": f"标签{i}", "desc": f"第 {i} 条测试标签",
             "level": "film", "scenes": []} for i in range(2, count)]
    return (head + tail)[:count]


def _v(r=4, s=3, v=3, quote=QUOTE, reason="台词有据"):
    return {"relevance": {"score": r, "reason": reason},
            "semantics": {"score": s, "reason": "名称清晰"},
            "value": {"score": v, "reason": "有区分度"},
            "quote": quote}


class StubClient:
    """逐条评审替身：按目标标签名回 canned 判决。

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
        target = json.loads(messages[1]["content"].split("## 待评估标签\n", 1)[1])[0]
        verdict = self.by_name.get(target["name"], self.default)
        if isinstance(verdict, Exception):
            raise verdict
        if verdict is None:
            return json.dumps({"reviews": []}, ensure_ascii=False)
        return json.dumps({"reviews": [{"id": "t0", **verdict}]}, ensure_ascii=False)


def _target(messages):
    return json.loads(messages[1]["content"].split("## 待评估标签\n", 1)[1])[0]


def _siblings(messages):
    seg = messages[1]["content"].split("## 该片全部标签（上下文）\n", 1)[1]
    return json.loads(seg.split("\n\n", 1)[0])


class MainLLM:
    model = "main-model"


# ---- 逐条消息形状 ----


def test_per_tag_message_shape():
    client = StubClient(default=_v())
    rows = _rows()
    rows[0]["judge_reference"] = {"relevance": 4, "semantics": 3, "value": 3,
                                  "reason": "judge 依据"}
    review_movie_tags(client, _content(), rows, run_id="r1:m1")
    assert len(client.calls) == 2  # 逐条：两条各一次
    for messages in client.calls:
        assert messages[0]["content"] == REVIEW_PROMPT
        assert _siblings(messages) == ["逃离安排", "街道晨跑"]  # 兄弟清单含全部
        assert "仅供参考" in messages[0]["content"]
    targets = [_target(m) for m in client.calls]
    assert all(isinstance(t, dict) for t in targets)
    assert {t["name"] for t in targets} == {"逃离安排", "街道晨跑"}
    assert all(t["id"] == "t0" for t in targets)  # 目标重编 t0
    by_name = {t["name"]: t for t in targets}
    assert by_name["逃离安排"]["judge_reference"]["relevance"] == 4
    assert "judge_reference" not in by_name["街道晨跑"]


def test_normal_pass_and_fail():
    client = StubClient(by_name={"逃离安排": _v(), "街道晨跑": _v(r=1, quote=None)})
    rows = _rows()
    result = review_movie_tags(client, _content(), rows, run_id="r1:m1")
    assert result is rows
    assert result[0]["pass"] is True
    assert result[0]["relevance"] == {"score": 4, "reason": "台词有据"}
    assert result[1]["pass"] is False
    assert result[1]["relevance"]["score"] == 1


def test_pass_derived_from_floors():
    assert REVIEW_PASS_FLOORS == {"relevance": 3, "semantics": 3, "value": 2}
    cases = [(_v(3, 3, 2), True), (_v(r=2), False), (_v(s=2), False), (_v(v=1), False)]
    for verdict, expected in cases:
        result = review_movie_tags(StubClient(default=verdict), _content(),
                                   _rows(1), run_id="r1:m1")
        assert result[0]["pass"] is expected


def test_redundancy_instruction_in_prompt():
    assert "语义重复" in REVIEW_PROMPT
    assert "该片全部标签" in REVIEW_PROMPT


# ---- 摘录必核 ----


def test_high_relevance_quote_must_hit():
    client = StubClient(default=_v(quote="这句原文根本不在影片材料里"))
    result = review_movie_tags(client, _content(), _rows(1), run_id="r1:m1")
    assert result[0]["pass"] is False
    assert result[0]["relevance"] == {"score": 1, "reason": "摘录未命中原文"}


def test_quote_spanning_punctuation_stays_high():
    client = StubClient(default=_v(quote="小镇街道晨跑，电台广播"))
    result = review_movie_tags(client, _content(), _rows(1), run_id="r1:m1")
    assert result[0]["pass"] is True
    assert result[0]["relevance"]["score"] == 4


def test_honors_modalities():
    client = StubClient(default=_v())
    result = review_movie_tags(client, _content(), _rows(1), run_id="r1:m1",
                               modalities=("script",))
    assert result[0]["pass"] is False  # 台词摘录核不中 → 降档
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
    assert result[0]["pass"] is False
    assert result[0]["semantics"]["score"] is None


def test_all_missed_raises():
    with pytest.raises(ReviewRequestError):
        review_movie_tags(StubClient(default=None), _content(), _rows(), run_id="r1:m1")


def test_single_tag_transport_failure_isolated():
    client = StubClient(by_name={"逃离安排": RuntimeError("端点超时"), "街道晨跑": _v()})
    result = review_movie_tags(client, _content(), _rows(), run_id="r1:m1")
    assert result[0]["pass"] is None  # 单条失败不拖累其余
    assert result[1]["pass"] is True


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


# ---- 实时回调 ----


def test_on_result_called_per_tag():
    seen = []
    client = StubClient(default=_v())
    review_movie_tags(client, _content(), _rows(), run_id="r1:m1",
                      on_result=lambda i, row: seen.append((i, row["pass"])))
    assert len(seen) == 2
    assert all(p is True for _, p in seen)


def test_on_result_exception_does_not_break():
    client = StubClient(default=_v())
    result = review_movie_tags(client, _content(), _rows(1), run_id="r1:m1",
                               on_result=lambda i, row: (_ for _ in ()).throw(RuntimeError("x")))
    assert result[0]["pass"] is True


def test_empty_rows_returns_empty():
    assert review_movie_tags(StubClient(), _content(), [], run_id="r1:m1") == []


# ---- 客户端工厂与评审口径 ----


def test_make_review_client_same_model_fallback_identity():
    llm = MainLLM()
    assert make_review_client(Settings(), llm) is llm


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


def test_make_review_client_env_names_fall_back_to_main():
    settings = Settings(open_eval_review_model="review-x")
    client = make_review_client(settings, MainLLM())
    assert client.base_url_env == settings.llm_base_url_env
    assert client.api_key_env == settings.llm_api_key_env


def test_review_calibre_independent_model():
    settings = Settings(open_eval_review_model="review-x", llm_model="main-model")
    assert review_calibre(settings, MainLLM()) == {
        "model": "review-x", "same_model": False}


def test_review_calibre_same_name_is_same_model():
    settings = Settings(open_eval_review_model="main-model", llm_model="main-model")
    assert review_calibre(settings, MainLLM())["same_model"] is True
