"""judge 评分协议：显式 null / 漏字段 / 非法值三分，重试与末轮兜底。

实测背景（run-20261124-034822-6bb3 / run-20261124-060432-84ee）：judge 对
整片 11-12 条候选统一给 `value=null`，解析器把它与"漏字段"混成同一个
null，过滤层按未过线淘汰 → 整片产出归零，且原始响应没落盘、无从复盘。

契约：
- 只有 relevance 允许显式 null（缺少影片材料）；
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
        self.messages = []

    def complete(self, messages, run_id):
        index = min(self.calls, len(self.responses) - 1)
        self.calls += 1
        self.messages.append(messages)
        return LLMResponse(content=self.responses[index], usage={}, raw={}, ok=True)


# ---- 解析层：三种"没分"必须分开 ----


def test_explicit_null_is_allowed_only_for_relevance():
    items = parse_judge_items(_text(_verdict(relevance=None)))
    assert items[0]["relevance"] is None
    with pytest.raises(MTGParseError, match="semantics 不接受 null"):
        parse_judge_items(_text(_verdict(semantics=None)))
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


def test_omitted_verdict_retries_only_missing_candidate(content_fixture):
    client = _ScriptedJudge(_text(_verdict()), _text(_verdict("粗粝写实")))
    trace = {}
    verdicts = judge_candidates(client, content_fixture,
                                [{"tag": "冷峻压抑"}, {"tag": "粗粝写实"}],
                                run_id="r", trace=trace)
    assert set(verdicts) == {"冷峻压抑", "粗粝写实"}
    assert "粗粝写实" in trace["errors"][0]["error"]
    retry = client.messages[1][0]["content"]
    assert "- 粗粝写实｜" in retry
    assert "- 冷峻压抑｜" not in retry


def test_unmatched_responses_cannot_complete_the_task(content_fixture):
    client = _ScriptedJudge(_text(_verdict("无关名")))
    with pytest.raises(JudgeError, match="缺少有效判决：冷峻压抑"):
        judge_candidates(client, content_fixture, [{"tag": "冷峻压抑"}], run_id="r")
    assert client.calls == 2


def test_bad_scene_field_retries_without_discarding_the_other_verdict(content_fixture):
    client = _ScriptedJudge(
        _text(_verdict(), _verdict("粗粝写实", scenes=1)),
        _text(_verdict(value=None), _verdict("粗粝写实")),
    )
    verdicts = judge_candidates(client, content_fixture,
                                [{"tag": "冷峻压抑"}, {"tag": "粗粝写实"}], run_id="r")
    assert set(verdicts) == {"冷峻压抑", "粗粝写实"}


# ---- 目录契合：生成侧只是提示词，judge 侧才是硬闸 ----


def test_dir_fit_only_applies_to_marked_candidates_and_recovers_missing_ids(content_fixture):
    dirs = [{"dir_id": identifier, "path": identifier} for identifier in ("dir_a", "dir_b")]
    client = _ScriptedJudge(
        _text(_verdict(dir_fits={"dir_a": False, "dir_b": "true", "unasked": True}),
              _verdict("库内词")),
        _text(_verdict(dir_fits={"dir_a": True, "dir_b": True})),
    )
    verdicts = judge_candidates(client, content_fixture,
                                [{"tag": "冷峻压抑", "dir_ids": ["dir_a", "dir_b"]},
                                 {"tag": "库内词"}],
                                dirs=dirs, run_id="r")
    assert verdicts["冷峻压抑"]["dir_fits"] == {"dir_a": False, "dir_b": True}
    assert "dir_fits" not in verdicts["库内词"]
    assert "〔目录 dir_b〕" in client.messages[1][0]["content"]
    assert "- 库内词｜" not in client.messages[1][0]["content"]


def test_judge_prompt_marks_each_candidate_with_its_target_directory(content_fixture):
    from tagplatform.services.generation.open.mtg.prompts import build_judge_messages

    dirs = [{"dir_id": "dir_mood", "path": "内容类标签/情感氛围",
             "description": "描述内容传递给观众的整体情感基调和氛围。"}]
    text = build_judge_messages(
        content_fixture,
        [{"tag": "屋顶枪战追逐", "scope": "scene", "scenes": ["scene_001"],
          "dir_ids": ["dir_mood"]}],
        dirs=dirs)[0]["content"]

    assert "## 目标目录与目录契合" in text
    assert "- dir_mood｜内容类标签/情感氛围｜描述内容传递给观众的整体情感基调和氛围。" in text
    assert "〔scene｜依据场景 scene_001｜目录 dir_mood〕" in text
    assert '"dir_fits"' in text


def test_judge_without_directories_keeps_the_old_contract(content_fixture):
    from tagplatform.services.generation.open.mtg.prompts import build_judge_messages

    text = build_judge_messages(content_fixture, [{"tag": "示例"}])[0]["content"]
    assert "## 目标目录与目录契合" not in text
    assert "dir_fits" not in text
