"""prompt 内容渲染测试（上游扩展字段 → content 文本）。

契约：
- 老数据（无角色/情绪/音频/台词）渲染结果与扩展前逐字一致；
- 新字段以“非 '-' 开头的缩进行”注入，不破坏场景行契约
  （llm/mock.py _SCENE_LINE_RE 依赖 `- {scene_id} [start-end] desc`）；
- 角色行、氛围、地点、声音、台词按上限截断。
"""

import json
from pathlib import Path

from tagplatform.llm.mock import _parse_scenes
from tagplatform.schemas import parse_content
from tagplatform.tasks.runner import _render_content_text
from tagplatform.tasks.runner import _MAX_CAST_LINES

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "content_movie.json").read_text(
        encoding="utf-8"))


def test_legacy_content_renders_unchanged():
    """无角色/情绪/音频/台词的老数据：渲染与扩展前逐字一致。"""
    content = parse_content({
        "schema_version": "1.0",
        "asset": {"asset_id": "m1", "asset_type": "movie", "title": "旧片"},
        "content": {"summary": "整体剧情", "scenes": [
            {"scene_id": "s1", "time_range": {"start_ms": 0, "end_ms": 1000},
             "description": "第一场"},
            {"scene_id": "s2", "time_range": {"start_ms": 1000, "end_ms": 2000},
             "description": "第二场"},
        ]},
        "metadata": {"source": "legacy", "recognition_status": "completed"},
    })
    assert _render_content_text(content) == (
        "《旧片》\n摘要：整体剧情\n- s1 [0-1000] 第一场\n- s2 [1000-2000] 第二场")


def test_rich_content_injects_cast_and_scene_details():
    data = json.loads(json.dumps(FIXTURE))
    data["content"]["characters"] = [
        {"character_id": "c1", "name": "张三", "description": "主角"},
        {"character_id": "c2", "name": "李四"},
        {"character_id": "c3", "name": "王五"},
    ]
    scene = data["content"]["scenes"][0]
    scene["emotions"] = ["紧张", "压迫"]
    scene["character_emotions"] = [
        {"character": "张三", "emotion": "愤怒"},
        {"character": "李四", "emotion": "恐惧"},
    ]
    scene["background_music"] = "低沉弦乐铺底"
    scene["location"] = "仓库"
    scene["audio_events"] = [
        {"event_type": "background_music", "description": "低沉弦乐",
         "start_ms": 0, "end_ms": 1000},
        {"event_type": "sound_effect", "description": "玻璃碎裂",
         "start_ms": 0, "end_ms": 1000},
        {"event_type": "sound_effect", "description": "应被截断",
         "start_ms": 0, "end_ms": 1000},
    ]
    scene["dialogues"] = [{
        "speaker": "张三", "speaker_name": "张三", "text": "你到底想怎样？",
        "start_ms": 0, "end_ms": 1,
        "emotion": {"text_emotion": "愤怒", "voice_emotion": "激动"},
    }]

    text = _render_content_text(parse_content(data))
    assert "人物：张三（主角）、李四、王五" in text
    assert "  氛围：紧张、压迫" in text
    assert "  人物情绪：张三（愤怒）、李四（恐惧）" in text
    assert "  音乐：低沉弦乐铺底" in text
    assert "  地点：仓库" in text
    assert "  声音：低沉弦乐" in text
    assert "  声音：玻璃碎裂" in text
    assert "应被截断" not in text  # 上限 2
    assert "  台词：张三（愤怒）：你到底想怎样？" in text


def test_character_emotions_fall_back_to_dialogue_tone():
    """严格贴模板的内容没有场景级人物情绪：从台词情绪回推（沉默角色除外）。"""
    data = json.loads(json.dumps(FIXTURE))
    scene = data["content"]["scenes"][0]
    scene.pop("character_emotions", None)
    scene["dialogues"] = [
        {"speaker": "张三", "speaker_name": "张三", "text": "你到底想怎样？",
         "start_ms": 0, "end_ms": 1,
         "emotion": {"text_emotion": "愤怒", "voice_emotion": None}},
        {"speaker": "李四", "speaker_name": "李四", "text": "我……我不知道。",
         "start_ms": 2, "end_ms": 3},
    ]

    text = _render_content_text(parse_content(data))
    assert "  人物情绪：张三（愤怒）" in text
    assert "李四" not in text.split("人物情绪：")[1].splitlines()[0]


def test_new_contract_content_renders_character_emotions():
    """new_json.txt 口径的资产（情绪挂在事件参与人上）也能渲染出人物情绪。"""
    from tagplatform.content_io import load_content

    payload = {
        "schema_version": "1.0",
        "asset": {"asset_id": "movie_x", "asset_type": "movie", "title": "片名",
                  "duration_ms": 1000, "video_uri": None, "language": None},
        "content": {
            "description": "整片剧情",
            "characters": [{"character_id": "character_001", "name": "张三",
                            "aliases": [], "description": "由 某演员 饰演"}],
            "scenes": [{
                "scene_id": "scene_001", "start_ms": 0, "end_ms": 1000,
                "description": "场景描述", "virtual_time": None, "location": "仓库",
                "character_ids": ["character_001"],
                "events": [{"event_id": "event_001", "description": "争执",
                            "participant_ids": [{
                                "character_id": "character_001",
                                "emotion": [{"name": "愤怒", "code": "ANGER"}]}]}],
                "emotion": [{"name": "紧张压迫", "code": "TENSE_OPPRESSIVE"}],
                "dialogues": [],
                "audio_events": [],
            }],
        },
    }
    text = _render_content_text(load_content(json.dumps(payload, ensure_ascii=False)))
    assert "  氛围：紧张压迫" in text
    assert "  人物情绪：张三（愤怒）" in text
    assert "  地点：仓库" in text


def test_cast_line_truncates():
    data = json.loads(json.dumps(FIXTURE))
    data["content"]["characters"] = [
        {"character_id": f"c{i}", "name": f"角色{i}"} for i in range(_MAX_CAST_LINES + 3)
    ]
    text = _render_content_text(parse_content(data))
    assert f"角色{_MAX_CAST_LINES - 1}" in text
    assert f"角色{_MAX_CAST_LINES}" not in text


def test_detail_lines_do_not_break_scene_contract():
    """明细行不得被 MockLLM 的场景行正则误识别（证据链依赖场景行唯一性）。"""
    data = json.loads(json.dumps(FIXTURE))
    data["content"]["scenes"][0]["emotions"] = ["紧张"]
    data["content"]["scenes"][0]["dialogues"] = [{
        "speaker": "张三", "text": "台词", "start_ms": 0, "end_ms": 1}]
    text = _render_content_text(parse_content(data))
    scenes = _parse_scenes(text)
    assert len(scenes) == len(data["content"]["scenes"])
    assert scenes[0]["scene_id"] == data["content"]["scenes"][0]["scene_id"]
