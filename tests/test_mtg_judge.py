"""judge 评分协议：显式 null / 漏字段 / 非法值三分，重试与末轮兜底。

实测背景（run-20261124-034822-6bb3 / run-20261124-060432-84ee）：judge 对
整片 11-12 条候选统一给 `value=null`，解析器把它与"漏字段"混成同一个
null，过滤层按未过线淘汰 → 整片产出归零，且原始响应没落盘、无从复盘。

契约：
- relevance/semantics 允许显式 null（要核对材料，判不了是合法结论）；
- value 只看名称本身，必须给 0-4 整数；null / 漏字段 / 非法值都是协议错误；
- 协议错误抛 MTGParseError（带同批合格判决），由 judge_candidates 重试；
- 末轮仍部分违协议 → 保住合格判决，违协议候选按未判淘汰；全坏 → JudgeError；
- 每次原始响应进 trace，供审计落盘。
"""

import json

import pytest

from tagplatform.capabilities.llm.client import LLMResponse
from tagplatform.services.generation.open.mtg.judge import JudgeError, judge_candidates
from tagplatform.services.generation.open.mtg.parse import MTGParseError, parse_judge_items


def _verdict(tag="冷峻压抑", **overrides):
    row = {"tag": tag, "relevance": 3, "semantics": 3, "value": 2,
           "confidence": 0.8, "scenes": ["scene_001"], "quote": "原文摘录",
           "reason": "桩判定"}
    row.update(overrides)
    return row


def _text(*rows):
    return json.dumps(list(rows), ensure_ascii=False)


class _ScriptedJudge:
    """按调用序返回预置响应的 judge 桩。"""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = 0

    def complete(self, messages, run_id):
        index = min(self.calls, len(self.responses) - 1)
        self.calls += 1
        return LLMResponse(content=self.responses[index], usage={}, raw={}, ok=True)


# ---- 解析层：三种"没分"必须分开 ----


def test_explicit_null_on_material_axes_stays_null():
    items = parse_judge_items(_text(_verdict(relevance=None, semantics=None)))
    assert items[0]["relevance"] is None
    assert items[0]["semantics"] is None
    assert items[0]["value"] == 2


@pytest.mark.parametrize("row", [
    _verdict(value=None),
    {key: value for key, value in _verdict().items() if key != "value"},
    _verdict(value=True),
    _verdict(value=5),
    _verdict(value="3"),
])
def test_value_protocol_violations_raise(row):
    with pytest.raises(MTGParseError):
        parse_judge_items(_text(row))


def test_missing_material_axis_is_protocol_error_not_null():
    row = {key: value for key, value in _verdict().items() if key != "relevance"}
    with pytest.raises(MTGParseError, match="漏字段"):
        parse_judge_items(_text(row))


def test_protocol_error_carries_the_valid_verdicts():
    text = _text(_verdict(tag="合格标签"), _verdict(tag="坏标签", value=None))
    with pytest.raises(MTGParseError) as excinfo:
        parse_judge_items(text)
    assert [item["tag"] for item in excinfo.value.items] == ["合格标签"]


def test_all_bad_leaves_no_salvage():
    with pytest.raises(MTGParseError) as excinfo:
        parse_judge_items(_text(_verdict(value=None)))
    assert excinfo.value.items == []


# ---- 调用层：重试、兜底、原始响应 ----


def test_batch_null_value_retries_instead_of_zeroing_the_film(content_fixture):
    bad = _text(_verdict(tag="冷峻压抑", value=None), _verdict(tag="粗粝写实", value=None))
    good = _text(_verdict(tag="冷峻压抑"), _verdict(tag="粗粝写实"))
    client = _ScriptedJudge(bad, good)
    trace: dict = {}
    candidates = [{"tag": "冷峻压抑"}, {"tag": "粗粝写实"}]
    verdicts = judge_candidates(client, content_fixture, candidates,
                                run_id="r", trace=trace)
    assert client.calls == 2
    assert {tag: row["value"] for tag, row in verdicts.items()} == {
        "冷峻压抑": 2, "粗粝写实": 2}
    assert [row["content"] for row in trace["raw"]] == [bad, good]
    assert trace["errors"][0]["attempt"] == 0


def test_last_attempt_salvages_valid_verdicts(content_fixture):
    mixed = _text(_verdict(tag="合格标签"), _verdict(tag="坏标签", value=None))
    client = _ScriptedJudge(mixed, mixed)
    trace: dict = {}
    verdicts = judge_candidates(client, content_fixture,
                                [{"tag": "合格标签"}, {"tag": "坏标签"}],
                                run_id="r", trace=trace)
    assert list(verdicts) == ["合格标签"]
    assert trace["salvaged"] == 1
    assert len(trace["raw"]) == 2


def test_two_bad_responses_still_raise(content_fixture):
    client = _ScriptedJudge("不是 JSON", "还不是 JSON")
    with pytest.raises(JudgeError):
        judge_candidates(client, content_fixture, [{"tag": "冷峻压抑"}], run_id="r")
