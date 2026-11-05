"""开放式生成 prompt 的内容渲染：台词全量进 prompt，事件描述不进。

2026-11 口径调整：台词按条数截断（3 条）改成全用——资产侧已经把每场景
台词控在 asset_dialogue_limit（8 条），prompt 再筛一次只会丢掉关键证据。
"""
import json
from pathlib import Path

from tagplatform.generation.mtg.prompts import build_generation_messages
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
