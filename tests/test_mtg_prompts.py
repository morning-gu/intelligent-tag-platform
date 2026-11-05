"""开放式生成 prompt 的内容渲染：台词全量进生成 prompt，不进 judge prompt。

2026-11 口径调整：
- 台词按条数截断（3 条）改成全用——资产侧已经把每场景台词控在
  asset_dialogue_limit（8 条），prompt 再筛一次只会丢掉关键证据；
- judge 输入去掉台词（A/B 实测判定 0 变化，prompt 省 24%-36%）；
- 事件描述两侧都不进（与场景描述语义重合 0.84）。
"""
import json
from pathlib import Path

from tagplatform.generation.mtg.prompts import (
    build_generation_messages,
    build_judge_messages,
)
from tagplatform.schemas import parse_content

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "content_movie.json").read_text(
        encoding="utf-8"))


def _content_with_dialogues(count: int):
    data = json.loads(json.dumps(FIXTURE))
    scene = data["content"]["scenes"][0]
    scene["dialogues"] = [
        {"speaker": f"人物{i}", "speaker_name": f"人物{i}",
         "text": f"第{i}句台词", "start_ms": i, "end_ms": i + 1}
        for i in range(1, count + 1)
    ]
    return parse_content(data)


def _prompt_text(content) -> str:
    messages = build_generation_messages(content, {})
    return "".join(part["text"] for part in messages[0]["content"]
                   if part["type"] == "text")


def test_all_dialogues_reach_the_prompt():
    """6 条台词全部进 prompt（旧行为只渲染前 3 条）。"""
    text = _prompt_text(_content_with_dialogues(6))

    for index in range(1, 7):
        assert f"第{index}句台词" in text


def test_event_descriptions_stay_out_of_the_prompt():
    """事件描述不进 prompt——与场景描述语义重叠 0.84，只加长度不加信息。"""
    data = json.loads(json.dumps(FIXTURE))
    data["content"]["scenes"][0]["events"] = [
        {"event_id": "event_001", "event_type": "event",
         "description": "这件事只出现在事件里", "participant_ids": []},
    ]

    text = _prompt_text(parse_content(data))

    assert "这件事只出现在事件里" not in text


def test_judge_prompt_has_no_dialogues():
    """judge 输入去掉台词：A/B 实测判定 0 处变化，prompt 省 24%-36%。"""
    content = _content_with_dialogues(6)
    text = build_judge_messages(content, [{"tag": "示例", "dimension": "题材"}])[0]["content"]

    assert "台词：" not in text
    assert "第1句台词" not in text


def test_judge_prompt_keeps_scene_shape():
    """去台词后场景行、氛围、人物情绪仍在：judge 靠这些判「有没有依据」。"""
    data = json.loads(json.dumps(FIXTURE))
    scene = data["content"]["scenes"][0]
    scene["emotions"] = ["紧张"]
    scene["character_emotions"] = [{"character": "张三", "emotion": "愤怒"}]
    scene["dialogues"] = [{"speaker": "张三", "text": "只在台词里的一句话",
                           "start_ms": 0, "end_ms": 1}]

    text = build_judge_messages(
        parse_content(data), [{"tag": "示例", "dimension": "题材"}])[0]["content"]

    assert "  氛围：紧张" in text
    assert "  人物情绪：张三（愤怒）" in text
    assert "只在台词里的一句话" not in text
    assert scene["scene_id"] in text
