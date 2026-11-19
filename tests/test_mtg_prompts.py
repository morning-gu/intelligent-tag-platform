"""开放式生成与 Judge 的材料渲染、模态门控和输出契约。"""
import json
from pathlib import Path

import pytest

from tagplatform.generation.mtg.prompts import (
    build_generation_messages,
    build_judge_messages,
    build_scene_generation_messages,
)
from tagplatform.schemas import parse_content
from tagplatform.schemas.content import Character

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


def test_generation_prompt_asks_for_grounded_reasons():
    text = _prompt_text(_content_with_dialogues(2))

    assert "至少引用两个不同场景" in text
    assert "证据不足可以少给或返回空 tags" in text
    assert "可跨影片复用的词典式释义" in text
    assert "并说明这些细节如何支持标签" in text
    assert "20 字以内" not in text


def test_scene_prompt_uses_catalog_and_search_goal():
    content = _content_with_dialogues(2)
    scene = content.content.scenes[0]
    messages = build_scene_generation_messages(
        scene, (), content=content, max_tags=1,
        dir_hint=[{"dir_id": "dir_event", "path": "情节事件", "scope": "scene"}],
    )
    body, directory, instructions = [part["text"] for part in messages[0]["content"]]
    assert "第1句台词" in body
    assert "dir_event｜情节事件｜单场景" in directory
    assert "为场景搜索提炼标签" in instructions
    assert "五个维度" not in instructions
    assert '"dimension"' not in instructions


def test_generation_adds_relevant_cast_only_to_the_current_scene():
    content = _content_with_dialogues(1)
    content.content.characters.extend(
        Character(character_id=f"added_{index}", name=f"额外人物{index}")
        for index in range(10)
    )
    content.content.characters[-1].aliases = ["人物1"]
    scene = content.content.scenes[0]
    scene.character_ids = ["added_8"]
    other_scene = scene.model_copy(
        update={"scene_id": "other_scene", "description": "另一场特有描述", "dialogues": []})
    content.content.scenes.append(other_scene)

    local = build_scene_generation_messages(
        scene, (), content=content, max_tags=1, dir_hint=[])
    local_body = local[0]["content"][0]["text"]
    background, _, scene_body = local_body.partition("### 当前场景")
    assert content.asset.title in background
    assert "额外人物8" in background and "额外人物9" in background
    assert "额外人物0" not in background
    assert "第1句台词" not in background
    assert "第1句台词" in scene_body
    assert "另一场特有描述" not in local_body
    assert all(name in _prompt_text(content) for name in ("额外人物0", "额外人物9", "另一场特有描述"))

    visual_only = build_scene_generation_messages(
        scene, (), content=content, max_tags=1, dir_hint=[], modalities={"video"})
    assert content.asset.title not in visual_only[0]["content"][0]["text"]


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


def test_judge_uses_dialogues_only_when_enabled():
    content = _content_with_dialogues(6)
    candidates = [{"tag": "示例", "scope": "scene", "scenes": [content.content.scenes[0].scene_id]}]
    with_dialogue = build_judge_messages(content, candidates)[0]["content"]
    without_dialogue = build_judge_messages(
        content, candidates, include_dialogue=False)[0]["content"]

    assert "scene 按本场检索价值" in with_dialogue
    assert "第1句台词" in with_dialogue
    assert "第1句台词" not in without_dialogue


def test_judge_prompt_keeps_complete_candidate_reason_on_one_line():
    content = _content_with_dialogues(1)
    sentences = (
        "分别和重逢两场都回到旧码头，两人用相同的称呼确认对方依然记得当初的约定。",
        "地点与称呼的呼应表明这段关系跨越了多年的分别。",
    )
    text = build_judge_messages(content, [{
        "tag": "命运羁绊", "dimension": "主题", "reason": "\n".join(sentences),
    }])[0]["content"]

    assert f"- 命运羁绊｜主题｜{' '.join(sentences)}" in text.splitlines()


def test_judge_prompt_keeps_scene_shape():
    """去台词后场景行、氛围、人物情绪仍在：judge 靠这些判「有没有依据」。"""
    data = json.loads(json.dumps(FIXTURE))
    scene = data["content"]["scenes"][0]
    scene["emotions"] = ["紧张"]
    scene["character_emotions"] = [{"character": "张三", "emotion": "愤怒"}]
    scene["dialogues"] = [{"speaker": "张三", "text": "只在台词里的一句话",
                           "start_ms": 0, "end_ms": 1}]

    text = build_judge_messages(
        parse_content(data), [{"tag": "示例", "dimension": "题材"}],
        include_dialogue=False)[0]["content"]

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
