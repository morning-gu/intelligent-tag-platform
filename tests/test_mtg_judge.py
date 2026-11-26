"""judge 评分协议：统一三维标准（relevance/semantics/value_scores）、三方向加权与重试兜底。

契约：
- relevance / semantics 允许显式 null（分别对应缺少影片材料、缺少释义）；
- value_scores 必须含 presentation / search / recommendation 三个 0-4 整数，
  不接受 null、漏字段或非法值；
- 协议错误抛 MTGParseError（带同批合格判决），由 judge_candidates 重试；
- judge_candidates 按候选所属标签种类（content/scene/character）与权重表确定性
  聚合保留两位小数的 value 总分；
- 目录匹配度已融合进 semantics，不再使用独立 dir_fits 协议；
- 每次原始响应进 trace，供审计落盘。
"""

import json

import pytest

from tagplatform.capabilities.llm.client import LLMResponse
from tagplatform.services.generation.open.mtg.judge import JudgeError, judge_candidates
from tagplatform.services.generation.open.mtg.parse import MTGParseError, parse_judge_items


def _verdict(tag="冷峻压抑", **overrides):
    row = {
        "tag": tag,
        "relevance": 3,
        "semantics": 3,
        "value_scores": {"presentation": 2, "search": 2, "recommendation": 2},
        "confidence": 0.8,
        "scenes": ["scene_001"],
        "quote": "原文摘录",
        "reason": "桩判定",
    }
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


# ---- 解析层：显式 null / 漏字段 / 非法值 ----


def test_explicit_null_is_allowed_for_relevance_and_semantics():
    items = parse_judge_items(_text(_verdict(relevance=None, semantics=None)))
    assert items[0]["relevance"] is None
    assert items[0]["semantics"] is None
    assert items[0]["value_scores"] == {"presentation": 2, "search": 2, "recommendation": 2}
    assert "dir_fits" not in items[0]


def test_value_scores_accepts_chinese_keys_and_value_object_fallback():
    items = parse_judge_items(_text({
        "tag": "硬科幻",
        "relevance": 4,
        "semantics": 4,
        "value": {"内容呈现": 3, "内容搜索": 4, "内容推荐": 1},
        "confidence": 0.9,
        "scenes": ["scene_001"],
        "quote": "原文摘录",
        "reason": "中文键兼容",
    }))
    assert items[0]["value_scores"] == {"presentation": 3, "search": 4, "recommendation": 1}


@pytest.mark.parametrize("row", [
    _verdict(value_scores=None),
    {key: value for key, value in _verdict().items() if key != "value_scores"},
    _verdict(value_scores={"presentation": None, "search": 2, "recommendation": 2}),
    _verdict(value_scores={"presentation": 2, "search": 2}),
    _verdict(value_scores={"presentation": True, "search": 2, "recommendation": 2}),
    _verdict(value_scores={"presentation": 5, "search": 2, "recommendation": 2}),
    _verdict(value_scores={"presentation": "3", "search": 2, "recommendation": 2}),
])
def test_value_scores_protocol_violations_raise(row):
    with pytest.raises(MTGParseError):
        parse_judge_items(_text(row))


def test_missing_material_axis_is_protocol_error_not_null():
    row = {key: value for key, value in _verdict().items() if key != "relevance"}
    with pytest.raises(MTGParseError, match="漏字段"):
        parse_judge_items(_text(row))


def test_protocol_error_carries_the_valid_verdicts():
    text = _text(_verdict(tag="合格标签"), _verdict(tag="坏标签", value_scores=None))
    with pytest.raises(MTGParseError) as excinfo:
        parse_judge_items(text)
    assert [item["tag"] for item in excinfo.value.items] == ["合格标签"]


def test_all_bad_leaves_no_salvage():
    with pytest.raises(MTGParseError) as excinfo:
        parse_judge_items(_text(_verdict(value_scores=None)))
    assert excinfo.value.items == []


# ---- 调用层：权重聚合、重试、兜底、原始响应 ----


def test_value_aggregation_uses_tag_kind_weights(content_fixture):
    scores = {"presentation": 4, "search": 2, "recommendation": 0}
    dirs = [
        {"dir_id": "d_content", "path": "题材", "scope": "content"},
        {"dir_id": "d_scene", "path": "名场面", "scope": "scene"},
        {"dir_id": "d_char", "path": "人物", "scope": "character"},
    ]
    candidates = [
        {"tag": "硬科幻", "candidate_id": "c_content", "dir_ids": ["d_content"]},
        {"tag": "雨夜追击", "candidate_id": "c_scene", "dir_ids": ["d_scene"]},
        {"tag": "孤胆英雄", "candidate_id": "c_char", "dir_ids": ["d_char"]},
    ]
    client = _ScriptedJudge(_text(
        _verdict("硬科幻", candidate_id="c_content", value_scores=scores),
        _verdict("雨夜追击", candidate_id="c_scene", value_scores=scores),
        _verdict("孤胆英雄", candidate_id="c_char",
                 value_scores={"presentation": 4, "search": 1, "recommendation": 0}),
    ))
    verdicts = judge_candidates(client, content_fixture, candidates, dirs=dirs, run_id="r")
    # content: 0.30*4 + 0.40*2 + 0.30*0 = 2.0
    assert verdicts["c_content"]["tag_kind"] == "content"
    assert verdicts["c_content"]["value"] == 2.0
    # scene: 0.50*4 + 0.40*2 + 0.10*0 = 2.8
    assert verdicts["c_scene"]["tag_kind"] == "scene"
    assert verdicts["c_scene"]["value"] == 2.8
    # character: 0.55*4 + 0.30*1 + 0.15*0 = 2.5
    assert verdicts["c_char"]["tag_kind"] == "character"
    assert verdicts["c_char"]["value"] == 2.5


def test_batch_null_value_retries_instead_of_zeroing_the_film(content_fixture):
    bad = _text(_verdict(tag="冷峻压抑", value_scores=None),
                _verdict(tag="粗粝写实", value_scores=None))
    good = _text(_verdict(tag="冷峻压抑"), _verdict(tag="粗粝写实"))
    client = _ScriptedJudge(bad, good)
    trace: dict = {}
    candidates = [{"tag": "冷峻压抑"}, {"tag": "粗粝写实"}]
    verdicts = judge_candidates(client, content_fixture, candidates,
                                run_id="r", trace=trace)
    assert client.calls == 2
    assert {tag: row["value"] for tag, row in verdicts.items()} == {
        "冷峻压抑": 2.0, "粗粝写实": 2.0}
    assert [row["content"] for row in trace["raw"]] == [bad, good]
    assert trace["errors"][0]["attempt"] == 0


def test_last_attempt_salvages_valid_verdicts(content_fixture):
    mixed = _text(_verdict(tag="合格标签"), _verdict(tag="坏标签", value_scores=None))
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
        _text(_verdict(value_scores=None), _verdict("粗粝写实")),
    )
    verdicts = judge_candidates(client, content_fixture,
                                [{"tag": "冷峻压抑"}, {"tag": "粗粝写实"}], run_id="r")
    assert set(verdicts) == {"冷峻压抑", "粗粝写实"}


# ---- 提示词：目录融入 semantics、带入释义、无 dir_fits ----


def test_judge_prompt_carries_desc_target_dir_and_unified_criteria_without_dir_fits(content_fixture):
    from tagplatform.capabilities.prompts.mtg import JUDGE_RETRY_HINT
    from tagplatform.services.generation.open.mtg.prompts import build_judge_messages

    dirs = [{"dir_id": "dir_mood", "path": "内容类标签/情感氛围",
             "scope": "content",
             "description": "描述内容传递给观众的整体情感基调和氛围。"}]
    text = build_judge_messages(
        content_fixture,
        [{"tag": "屋顶枪战追逐", "desc": "角色在屋顶展开紧张追逐交火的动作桥段",
          "scope": "scene", "scenes": ["scene_001"],
          "dir_ids": ["dir_mood"], "candidate_id": "scene-000001",
          "reason": "屋顶追逐交火"}],
        dirs=dirs)[0]["content"]

    assert "## 目标目录\n" in text
    assert "## 目标目录与目录契合" not in text
    assert "- dir_mood｜内容类标签/情感氛围｜适用种类：内容级｜描述内容传递给观众的整体情感基调和氛围。" in text
    assert "〔scene｜依据场景 scene_001｜标签种类 内容级｜目录 dir_mood｜候选 ID scene-000001〕" in text
    assert "释义：角色在屋顶展开紧张追逐交火的动作桥段；依据：屋顶追逐交火" in text
    assert "内容呈现（presentation）" in text
    assert "内容搜索（search）" in text
    assert "内容推荐（recommendation）" in text
    assert "目标目录匹配度" in text
    assert "dir_fits" not in text
    assert '"confidence":0.85' in text
    assert "完整上下文片段" in text
    example = text.rsplit("只输出一个 JSON 对象", 1)[1]
    assert '"candidate_id":"候选 ID"' in example
    assert '"value_scores":{"presentation":3,"search":4,"recommendation":2}' in example
    assert "dir_fits" not in example
    assert "candidate_id" in JUDGE_RETRY_HINT and "value_scores" in JUDGE_RETRY_HINT
    assert "dir_fits" not in JUDGE_RETRY_HINT


def test_judge_without_directories_omits_dir_section(content_fixture):
    from tagplatform.services.generation.open.mtg.prompts import build_judge_messages

    text = build_judge_messages(content_fixture, [{"tag": "示例"}])[0]["content"]
    assert "## 目标目录" not in text
    assert "dir_fits" not in text
    assert "candidate_id" not in text


def test_candidate_ids_keep_same_name_verdicts_separate_across_batches(
        content_fixture, monkeypatch):
    from tagplatform.services.generation.open.mtg import judge

    monkeypatch.setattr(judge, "_MAX_BATCH_SIZE", 2)
    same_name = "宿命循环"
    candidates = [
        {"tag": same_name, "candidate_id": f"c{i}", "dir_ids": [f"d{i}"]}
        for i in range(3)
    ]
    dirs = [{"dir_id": f"d{i}", "path": f"d{i}", "scope": "content"} for i in range(3)]
    client = _ScriptedJudge(
        _text(_verdict(same_name, candidate_id="c1",
                       value_scores={"presentation": 4, "search": 4, "recommendation": 4}),
              _verdict(same_name,
                       value_scores={"presentation": 2, "search": 2, "recommendation": 2})),
        _text(_verdict(same_name, candidate_id="c0",
                       value_scores={"presentation": 3, "search": 3, "recommendation": 3})),
        _text(_verdict(same_name, candidate_id="c2",
                       value_scores={"presentation": 2, "search": 2, "recommendation": 2})),
    )
    trace = {}
    verdicts = judge_candidates(client, content_fixture, candidates,
                                dirs=dirs, run_id="r", trace=trace)
    assert {key: row["value"] for key, row in verdicts.items()} == {
        "c0": 3.0, "c1": 4.0, "c2": 2.0,
    }
    assert [row["attempt"] for row in trace["raw"]] == [0, 1, 2]
    retry = client.messages[1][0]["content"]
    assert "候选 ID c0" in retry and "候选 ID c1" not in retry
