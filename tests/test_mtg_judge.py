"""judge 评分协议：统一三维标准、三方向加权、重试兜底（评审归一 Task 6 接引擎）。

契约：
- 判决解析/证据核验/批编排全部由 `mtg.verdicts` 引擎承载（parse_verdicts /
  finalize_verdict / run_verdicts），本模块只测 judge 侧的投影与编排参数；
- 返回值按候选键（candidate_id 或归一化标签名）键控，值为 finalize_verdict
  终判（relevance/semantics {"score","reason"}、value 聚合块、quote、scenes、
  confidence、reason、verify_fail）；
- judge 策略：20 条一批、漏判带共享重试提示补试一轮后落 errors（不整批盲
  重试——与 legacy 的有意分歧）、名称兜底开；
- 零判决抛 JudgeError（任务应置 failed），部分漏判不抛错（上层过滤段淘汰）；
- trace 由引擎写统一键族（batches/errors/salvaged）。
"""

import json

import pytest

from tagplatform.capabilities.llm.client import LLMResponse
from tagplatform.capabilities.prompts.mtg import JUDGE_RETRY_HINT
from tagplatform.services.generation.open.mtg.judge import JudgeError, judge_candidates


def _dims(scores) -> dict:
    """value 三方向 {direction: {"score", "reason"}}（融合协议形状）。"""
    return {key: {"score": score, "reason": "方向理由"}
            for key, score in scores.items()}


def _candidate(tag, key, **overrides):
    row = {
        "tag": tag,
        "candidate_id": key,
        "desc": "通用释义",
        "dimension": "情绪",
        "scope": "excerpt",
        "tag_kind": None,
        "reason": "生成依据",
        "scenes": ["scene_001"],
        "dir_ids": [],
    }
    row.update(overrides)
    return row


def _verdict(key, **overrides):
    """对候选 key 的标准合格判决（融合形状，quote/scenes 落在材料内）。"""
    entry = {
        "id": key,
        "tag": "",
        "relevance": {"score": 4, "reason": "内容强相关"},
        "semantics": {"score": 3, "reason": "语义清楚"},
        "value": _dims({"presentation": 3, "search": 4, "recommendation": 2}),
        "quote": "在仓库中发生激烈争执",
        "scenes": ["scene_001"],
        "confidence": 0.85,
    }
    entry.update(overrides)
    return entry


def _text(entries):
    return json.dumps({"verdicts": entries}, ensure_ascii=False)


class _ScriptedJudge:
    """按调用序返回预置响应的 judge 桩。

    脚本项为响应文本（包装成成功 LLMResponse）或 LLMResponse 实例（信封
    原样透传，截断/失败信封用例用）。
    """

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = 0
        self.messages = []

    def complete(self, messages, run_id):
        step = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        self.messages.append(messages)
        if isinstance(step, LLMResponse):
            return step
        return LLMResponse(content=step, usage={}, raw={}, ok=True)


# ---- 调用层：终判形状、批大小、漏判、错误 ----


def test_judge_candidates_returns_finalized_nested_verdicts(content_fixture):
    client = _ScriptedJudge(_text([_verdict("c1")]))
    verdicts = judge_candidates(client, content_fixture,
                                [_candidate("冷峻压抑", "c1")], run_id="r")
    verdict = verdicts["c1"]
    assert verdict["relevance"] == {"score": 4, "reason": "内容强相关"}
    assert verdict["semantics"] == {"score": 3, "reason": "语义清楚"}
    # content 权重 (0.30, 0.40, 0.30)：0.3*3 + 0.4*4 + 0.3*2 = 3.10
    assert verdict["value"]["score"] == 3.1
    assert verdict["value"]["tag_kind"] == "content"
    assert set(verdict["value"]["dimensions"]) == {"presentation", "search",
                                                   "recommendation"}
    assert verdict["quote"] == "在仓库中发生激烈争执"
    assert verdict["scenes"] == ["scene_001"]
    assert verdict["confidence"] == 0.85
    assert verdict["verify_fail"] is None
    assert "相关性(4)" in verdict["reason"] and "语义(3)" in verdict["reason"]


def test_batch_size_20_splits_into_two_calls(content_fixture):
    keys = [f"c{i:02d}" for i in range(21)]
    candidates = [_candidate(f"标签{i}", key) for i, key in enumerate(keys)]
    client = _ScriptedJudge(_text([_verdict(key) for key in keys[:20]]),
                            _text([_verdict(keys[20])]))
    trace: dict = {}
    verdicts = judge_candidates(client, content_fixture, candidates,
                                run_id="r", trace=trace)
    assert client.calls == 2
    assert set(verdicts) == set(keys)
    assert [len(record["targets"]) for record in trace["batches"]] == [20, 1]


def test_missing_verdicts_omitted_without_raising(content_fixture):
    """漏判候选带提示补试一轮仍缺 → 不在返回值、不抛错（上层按未返回淘汰）。"""
    client = _ScriptedJudge(_text([_verdict("c1")]), _text([_verdict("c1")]))
    verdicts = judge_candidates(
        client, content_fixture,
        [_candidate("冷峻压抑", "c1"), _candidate("粗粝写实", "c2")], run_id="r")
    assert set(verdicts) == {"c1"}
    assert client.calls == 2


def test_all_failed_raises_judge_error(content_fixture):
    client = _ScriptedJudge("不是 JSON", "还不是 JSON")
    with pytest.raises(JudgeError):
        judge_candidates(client, content_fixture,
                         [_candidate("冷峻压抑", "c1")], run_id="r")


def test_truncated_response_drops_missing_without_whole_retry(content_fixture):
    """裁定钉死（评审归一 Task 4）：judge 策略 + 截断响应 → 不整批重试，
    漏判直落 errors（与 legacy judge 的盲重试是有意分歧）。"""
    truncated = LLMResponse(
        content='{"verdicts": [{"id": "c1", "relevance": {"score": 4, "reason": "内容强相关"',
        usage={}, raw={"choices": [{"finish_reason": "length"}]}, ok=True)
    client = _ScriptedJudge(truncated)
    with pytest.raises(JudgeError, match="超出长度限制"):
        judge_candidates(client, content_fixture,
                         [_candidate("冷峻压抑", "c1"),
                          _candidate("粗粝写实", "c2")], run_id="r")
    assert client.calls == 1


def test_value_aggregation_uses_tag_kind_weights(content_fixture):
    dirs = [
        {"dir_id": "d_content", "path": "题材", "scope": "content"},
        {"dir_id": "d_scene", "path": "名场面", "scope": "scene"},
        {"dir_id": "d_char", "path": "人物", "scope": "character"},
    ]
    scores = {"presentation": 4, "search": 2, "recommendation": 0}
    candidates = [
        {"tag": "硬科幻", "candidate_id": "c_content", "dir_ids": ["d_content"],
         "scope": "excerpt", "scenes": ["scene_001"], "desc": "通用释义"},
        {"tag": "雨夜追击", "candidate_id": "c_scene", "dir_ids": ["d_scene"],
         "scope": "excerpt", "scenes": ["scene_001"], "desc": "通用释义"},
        {"tag": "孤胆英雄", "candidate_id": "c_char", "dir_ids": ["d_char"],
         "scope": "excerpt", "scenes": ["scene_001"], "desc": "通用释义"},
    ]
    client = _ScriptedJudge(_text([
        _verdict("c_content", value=_dims(scores)),
        _verdict("c_scene", value=_dims(scores)),
        _verdict("c_char", value=_dims({"presentation": 4, "search": 1,
                                        "recommendation": 0})),
    ]))
    verdicts = judge_candidates(client, content_fixture, candidates,
                                dirs=dirs, run_id="r")
    # content: 0.30*4 + 0.40*2 + 0.30*0 = 2.0
    assert verdicts["c_content"]["value"]["tag_kind"] == "content"
    assert verdicts["c_content"]["value"]["score"] == 2.0
    # scene: 0.50*4 + 0.40*2 + 0.10*0 = 2.8
    assert verdicts["c_scene"]["value"]["tag_kind"] == "scene"
    assert verdicts["c_scene"]["value"]["score"] == 2.8
    # character: 0.55*4 + 0.30*1 + 0.15*0 = 2.5
    assert verdicts["c_char"]["value"]["tag_kind"] == "character"
    assert verdicts["c_char"]["value"]["score"] == 2.5


def test_batch_null_value_retries_instead_of_zeroing_the_film(content_fixture):
    bad = _text([_verdict("c1", value=_dims({"presentation": None, "search": 2,
                                             "recommendation": 2})),
                 _verdict("c2", value=_dims({"presentation": None, "search": 2,
                                             "recommendation": 2}))])
    good = _text([_verdict("c1"), _verdict("c2")])
    client = _ScriptedJudge(bad, good)
    trace: dict = {}
    verdicts = judge_candidates(
        client, content_fixture,
        [_candidate("冷峻压抑", "c1"), _candidate("粗粝写实", "c2")],
        run_id="r", trace=trace)
    assert client.calls == 2
    assert {key: row["value"]["score"] for key, row in verdicts.items()} == {
        "c1": 3.1, "c2": 3.1}
    assert [record["raw"] for record in trace["batches"]] == [bad, good]
    assert trace["batches"][0]["attempt"] == 0
    assert trace["errors"] == {}


def test_last_attempt_salvages_valid_verdicts(content_fixture):
    broken_value = _dims({"presentation": None, "search": 2, "recommendation": 2})
    mixed = _text([_verdict("c1"), _verdict("c2", value=broken_value)])
    client = _ScriptedJudge(mixed, mixed)
    trace: dict = {}
    verdicts = judge_candidates(
        client, content_fixture,
        [_candidate("合格标签", "c1"), _candidate("坏标签", "c2")],
        run_id="r", trace=trace)
    assert set(verdicts) == {"c1"}
    # 引擎口径：每个未完成批的已配条目即时计数。首轮混合响应保住 1 条；
    # 漏判子集重试批里只有 c2（c1 条目带别批 id，不进该批回配）→ 计数仍为 1。
    assert trace["salvaged"] == 1
    assert len(trace["batches"]) == 2


def test_omitted_verdict_retries_only_missing_candidate(content_fixture):
    client = _ScriptedJudge(_text([_verdict("c1")]), _text([_verdict("c2")]))
    trace = {}
    verdicts = judge_candidates(
        client, content_fixture,
        [_candidate("冷峻压抑", "c1"), _candidate("粗粝写实", "c2")],
        run_id="r", trace=trace)
    assert set(verdicts) == {"c1", "c2"}
    assert trace["batches"][0]["missing"] == ["c2"]
    retry = client.messages[1][0]["content"]
    assert "- 粗粝写实｜" in retry
    assert "- 冷峻压抑｜" not in retry
    assert JUDGE_RETRY_HINT in retry


def test_unmatched_responses_cannot_complete_the_task(content_fixture):
    client = _ScriptedJudge(_text([_verdict("陌生人")]), _text([_verdict("陌生人")]))
    with pytest.raises(JudgeError, match="评审响应未包含有效判决"):
        judge_candidates(client, content_fixture,
                         [_candidate("冷峻压抑", "c1")], run_id="r")
    assert client.calls == 2


def test_bad_scene_field_retries_without_discarding_the_other_verdict(content_fixture):
    broken_value = _dims({"presentation": None, "search": 2, "recommendation": 2})
    client = _ScriptedJudge(
        _text([_verdict("c1"), _verdict("c2", scenes=1)]),
        _text([_verdict("c1", value=broken_value), _verdict("c2")]),
    )
    verdicts = judge_candidates(
        client, content_fixture,
        [_candidate("冷峻压抑", "c1"), _candidate("粗粝写实", "c2")], run_id="r")
    assert set(verdicts) == {"c1", "c2"}


def test_candidate_ids_keep_same_name_verdicts_separate_across_batches(
        content_fixture, monkeypatch):
    from tagplatform.services.generation.open.mtg import judge

    monkeypatch.setattr(judge, "_MAX_BATCH_SIZE", 2)
    same_name = "宿命循环"
    candidates = [_candidate(same_name, f"c{i}", dir_ids=[f"d{i}"])
                  for i in range(3)]
    dirs = [{"dir_id": f"d{i}", "path": f"d{i}", "scope": "content"}
            for i in range(3)]
    client = _ScriptedJudge(
        _text([_verdict("c1", value=_dims({"presentation": 4, "search": 4,
                                           "recommendation": 4})),
               _verdict(None, tag=same_name,
                        value=_dims({"presentation": 2, "search": 2,
                                     "recommendation": 2}))]),
        _text([_verdict("c0", value=_dims({"presentation": 3, "search": 3,
                                           "recommendation": 3}))]),
        _text([_verdict("c2", value=_dims({"presentation": 2, "search": 2,
                                           "recommendation": 2}))]),
    )
    trace = {}
    verdicts = judge_candidates(client, content_fixture, candidates,
                                dirs=dirs, run_id="r", trace=trace)
    assert {key: row["value"]["score"] for key, row in verdicts.items()} == {
        "c0": 3.0, "c1": 4.0, "c2": 2.0,
    }
    assert [record["attempt"] for record in trace["batches"]] == [0, 1, 0]
    retry = client.messages[1][0]["content"]
    assert "候选 ID c0" in retry and "候选 ID c1" not in retry


# ---- 提示词：目录融入 semantics、带入释义、无 dir_fits ----


def test_judge_prompt_carries_desc_target_dir_and_unified_criteria_without_dir_fits(content_fixture):
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
    assert "- dir_mood｜内容类标签/情感氛围｜适用种类：内容级｜描述内容传递给观众的整体情感基调和氛围。" in text
    assert "〔scene｜依据场景 scene_001｜标签种类 内容级｜目录 dir_mood｜候选 ID scene-000001〕" in text
    assert "释义：角色在屋顶展开紧张追逐交火的动作桥段；依据：屋顶追逐交火" in text
    assert "dir_fits" not in text
    from tagplatform.capabilities.prompts._evaluation import VERDICT_RETRY_HINT
    assert JUDGE_RETRY_HINT == VERDICT_RETRY_HINT
    assert "verdicts" in JUDGE_RETRY_HINT and "quote" in JUDGE_RETRY_HINT
    assert "dir_fits" not in JUDGE_RETRY_HINT


def test_judge_without_directories_omits_dir_section(content_fixture):
    from tagplatform.services.generation.open.mtg.prompts import build_judge_messages

    text = build_judge_messages(content_fixture, [{"tag": "示例"}])[0]["content"]
    assert "## 目标目录" not in text
    assert "dir_fits" not in text
    assert "candidate_id" not in text
