"""开放式生成 prompt 的内容渲染：台词全量进生成 prompt，不进 judge prompt。

2026-11 口径调整：
- 台词按条数截断（3 条）改成全用——资产侧已经把每场景台词控在
  asset_dialogue_limit（8 条），prompt 再筛一次只会丢掉关键证据；
- judge 输入去掉台词（A/B 实测判定 0 变化，prompt 省 24%-36%）；
- 事件描述两侧都不进（与场景描述语义重合 0.84）。
"""
import json
from pathlib import Path

import pytest

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


# ---------------------------------------------------------------- 模态门控
#
# `modalities` 是消融实验**唯一**允许变的旋钮（`eval/ablation.py`）。每条
# 断言都要能回答「关掉之后模型到底少看了什么」，否则消融结论无从解释。

def _text_with(content, modalities) -> str:
    messages = build_generation_messages(content, {}, modalities=modalities)
    return "".join(part["text"] for part in messages[0]["content"]
                   if part["type"] == "text")


def test_default_modalities_render_everything():
    """`modalities=None` 必须与旧行为逐字节一致（否则默认路径被悄悄改了口径）。"""
    content = _content_with_dialogues(3)

    assert _text_with(content, None) == _prompt_text(content)


def test_script_off_drops_all_content_text():
    """关掉场景文本 → 文本区整个为空；台词离了场景上下文无处归属，一并去掉。"""
    content = _content_with_dialogues(2)
    messages = build_generation_messages(
        content, {}, modalities=frozenset({"video"}))
    body = "".join(part["text"] for part in messages[0]["content"]
                    if part["type"] == "text")

    assert "第1句台词" not in body
    assert "台词：" not in body
    # 指令区仍在（模型仍要知道"输出什么"），只是没有了内容可读
    assert "## 要求" in body


def test_dialogue_off_keeps_scene_text_but_drops_lines():
    """关掉台词 → 场景描述、氛围都留着，只少 `台词：` 行。"""
    data = json.loads(json.dumps(FIXTURE))
    scene = data["content"]["scenes"][0]
    scene["emotions"] = ["紧张"]
    scene["dialogues"] = [{"speaker": "张三", "text": "不该出现的台词",
                           "start_ms": 0, "end_ms": 1}]
    content = parse_content(data)

    body = _text_with(content, frozenset({"video", "script"}))

    assert "  氛围：紧张" in body
    assert scene["scene_id"] in body
    assert "不该出现的台词" not in body
    assert "台词：" not in body


def test_video_off_drops_image_parts(tmp_path):
    """关掉关键帧 → 帧表被清空，prompt 里不再有任何 `image_url` 块。"""
    frame = tmp_path / "f_001.jpg"
    frame.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")
    content = _content_with_dialogues(1)
    frames = {content.content.scenes[0].scene_id: [frame]}

    with_frames = build_generation_messages(content, frames)
    without = build_generation_messages(
        content, frames, modalities=frozenset({"script", "dialogue"}))

    def _image_count(messages) -> int:
        return sum(1 for part in messages[0]["content"]
                   if part["type"] == "image_url")

    # 调用方即使照样传了帧表，关掉 video 也必须清空——不能指望调用方记得
    assert _image_count(with_frames) == 1
    assert _image_count(without) == 0


def test_unknown_modality_raises_instead_of_being_ignored():
    """拼错的模态名必须当场报错：静默当成 full 会让消融结论全错。"""
    content = _content_with_dialogues(1)

    with pytest.raises(ValueError, match="未知模态"):
        build_generation_messages(content, {}, modalities=frozenset({"viedo"}))
    with pytest.raises(ValueError, match="至少要开启一种模态"):
        build_generation_messages(content, {}, modalities=frozenset())
