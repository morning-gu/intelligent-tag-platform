"""上游（视频理解部门）格式适配器测试。

覆盖契约：
- 约定示例（见仓库根 `图谱&标签与长视频理解数据对齐.txt`）可完整转换：
  拍平时间戳 → time_range、emotion[].name → emotions、audio_events 保留、
  character_ids → entities、dialogue 姓名优先、metadata 由适配器补齐；
- 最小样本（只有 asset + 一个无描述的 scene）可转换，summary 回退；
- 宽松解析：上游新增未知字段不报错；缺 character_id 时自动编号；
- 硬约束：end_ms < start_ms、非法 asset_type 抛 ValidationError；
- 注册表：包导入后 list_adapters 含 "vision"，重复注册抛 ValueError。
"""

import json

import pytest
from pydantic import ValidationError

from tagplatform.adapters import VisionAdapter, get_adapter, list_adapters, register_adapter

# 与《图谱&标签与长视频理解数据对齐.txt》示例逐字对齐（含注释时请先去除）
SAMPLE = {
    "schema_version": "1.0",
    "asset": {
        "asset_id": "movie_001",
        "asset_type": "movie",
        "title": "示例电影",
        "duration_ms": 7200000,
        "video_uri": "file://bucket/movie_001.mp4",
        "language": "zh-CN",
    },
    "content": {
        "description": "影片整体剧情描述，包括故事背景、核心人物、主要矛盾和剧情发展。",
        "characters": [
            {"character_id": "character_001", "name": "张三", "aliases": [],
             "description": "影片中的主要人物"},
            {"character_id": "character_002", "name": "李四", "aliases": ["小李"],
             "description": "影片中的另一主要人物"},
        ],
        "scenes": [{
            "scene_id": "scene_001",
            "start_ms": 192000,
            "end_ms": 220000,
            "description": "场景中的剧情描述",
            "virtual_time": "场景中的剧情时间点",
            "location": "场景中的剧情地点",
            "character_ids": ["character_001", "character_002"],
            "events": [{"event_id": "event_001", "description": "场景中的事件描述",
                        "participant_ids": [
                            {"character_id": "character_001",
                             "emotion": [{"name": "愤怒", "code": "ANGER"}]},
                            {"character_id": "character_002",
                             "emotion": [{"name": "恐惧", "code": "FEAR"},
                                         {"name": "挫败", "code": "FRUSTRATION"}]},
                        ]}],
            "emotion": [{"name": "紧张压迫", "code": "TENSE_OPPRESSIVE"},
                        {"name": "压抑沉重", "code": "OPPRESSIVE_HEAVY"}],
            "dialogues": [{
                "dialogue_id": "dialogue_001",
                "speaker_id": "character_001",
                "speaker_name": "张三",
                "text": "你到底想怎样？",
                "start_ms": 200000,
                "end_ms": 202000,
            }],
            "audio_events": [
                {"event_type": "background_music", "description": "紧张低沉的背景音乐",
                 "start_ms": 192000, "end_ms": 220000},
                {"event_type": "sound_effect", "description": "桌面撞击声",
                 "start_ms": 200000, "end_ms": 201000},
            ],
        }],
    },
}


def test_vision_adapter_full_sample_mapping():
    c = get_adapter("vision").adapt(SAMPLE)
    assert c.asset.asset_id == "movie_001" and c.asset.language == "zh-CN"
    assert c.content.summary.startswith("影片整体剧情描述")
    assert [ch.name for ch in c.content.characters] == ["张三", "李四"]
    assert c.content.characters[1].aliases == ["小李"]

    scene = c.content.scenes[0]
    assert (scene.time_range.start_ms, scene.time_range.end_ms) == (192000, 220000)
    assert scene.emotions == ["紧张压迫", "压抑沉重"]
    assert scene.location == "场景中的剧情地点"
    assert scene.virtual_time == "场景中的剧情时间点"
    assert [(e.event_type, e.description) for e in scene.events] == [
        ("event", "场景中的事件描述")]
    assert {(e.entity_id, e.name, e.type) for e in scene.entities} == {
        ("character_001", "张三", "character"),
        ("character_002", "李四", "character"),
    }
    assert [(a.event_type, a.description) for a in scene.audio_events] == [
        ("background_music", "紧张低沉的背景音乐"),
        ("sound_effect", "桌面撞击声"),
    ]

    d = scene.dialogues[0]
    assert d.speaker == "张三" and d.speaker_name == "张三"
    assert d.emotion is None            # 新口径台词行没有情绪字段
    assert (d.start_ms, d.end_ms) == (200000, 202000)

    # 人物情绪从事件参与人回推到场景级，供标签渲染使用
    assert [(e.character_id, e.emotion) for e in scene.character_emotions] == [
        ("character_001", "愤怒"), ("character_002", "恐惧"),
        ("character_002", "挫败")]
    assert scene.events[0].participant_ids == ["character_001", "character_002"]

    # 上游未定义 metadata：适配器补 source/recognition_status
    assert c.metadata.source == "video_understanding"
    assert c.metadata.recognition_status == "completed"


def test_vision_adapter_minimal_payload():
    """最小样本：只有标题与一个无描述、无对话的场景。"""
    raw = {
        "asset": {"asset_id": "m2", "title": "最小样本"},
        "content": {"scenes": [{"scene_id": "s1", "start_ms": 0, "end_ms": 1000}]},
    }
    c = get_adapter("vision").adapt(raw)
    assert c.asset.asset_type == "movie"  # 缺省值
    assert c.content.summary == "" and c.content.scenes[0].description == ""
    assert c.content.scenes[0].time_range.end_ms == 1000


def test_vision_adapter_summary_falls_back_to_first_scene():
    raw = {
        "asset": {"asset_id": "m3", "title": "回退"},
        "content": {"scenes": [
            {"scene_id": "s1", "start_ms": 0, "end_ms": 10, "description": "第一段"},
            {"scene_id": "s2", "start_ms": 10, "end_ms": 20, "description": "第二段"},
        ]},
    }
    assert get_adapter("vision").adapt(raw).content.summary == "第一段"


def test_vision_adapter_generates_missing_character_ids():
    raw = {
        "asset": {"asset_id": "m4", "title": "无 id 角色"},
        "content": {
            "characters": [{"name": "甲"}],
            "scenes": [{"scene_id": "s1", "start_ms": 0, "end_ms": 1}],
        },
    }
    c = get_adapter("vision").adapt(raw)
    assert c.content.characters[0].character_id == "character_001"


def test_vision_adapter_duplicate_scene_ids_get_suffix():
    raw = {
        "asset": {"asset_id": "m5", "title": "重复场景"},
        "content": {"scenes": [
            {"scene_id": "s1", "start_ms": 0, "end_ms": 1},
            {"scene_id": "s1", "start_ms": 1, "end_ms": 2},
        ]},
    }
    c = get_adapter("vision").adapt(raw)
    assert [s.scene_id for s in c.content.scenes] == ["s1", "s1_2"]


def test_vision_adapter_ignores_unknown_fields():
    raw = json.loads(json.dumps(SAMPLE))
    raw["future_section"] = {"x": 1}
    raw["content"]["scenes"][0]["shot_ids"] = [1, 2, 3]
    assert get_adapter("vision").adapt(raw).asset.asset_id == "movie_001"


@pytest.mark.parametrize("mutate", [
    {"path": ("content", "scenes", 0, "end_ms"), "value": 1000},   # end < start
    {"path": ("asset", "asset_type"), "value": "clip"},            # 非法类型
    {"path": ("asset", "asset_id"), "value": None},                # 必填缺失
])
def test_vision_adapter_invalid_rejected(mutate):
    raw = json.loads(json.dumps(SAMPLE))
    node = raw
    for key in mutate["path"][:-1]:
        node = node[key]
    node[mutate["path"][-1]] = mutate["value"]
    with pytest.raises(ValidationError):
        get_adapter("vision").adapt(raw)


def test_vision_registered_and_duplicate_rejected():
    assert "vision" in list_adapters()
    with pytest.raises(ValueError):
        register_adapter(VisionAdapter())


def test_vision_adapter_slim_asset_fields():
    """精简资产：atmosphere / character_emotions / background_music 落位。"""
    raw = {
        "asset": {"asset_id": "m9", "title": "精简", "duration_ms": 60_000},
        "content": {
            "description": "整片中文剧情",
            "scenes": [{
                "scene_id": "s1", "start_ms": 0, "end_ms": 30_000,
                "description": "场景中文剧情",
                "atmosphere": ["紧张", "压抑"],
                "character_emotions": [{"character": "陈默", "emotion": "愤怒"}],
                "background_music": "",
            }],
        },
    }
    c = get_adapter("vision").adapt(raw)
    scene = c.content.scenes[0]
    assert scene.emotions == ["紧张", "压抑"]
    assert [(e.character, e.emotion) for e in scene.character_emotions] == [
        ("陈默", "愤怒")]
    assert scene.background_music == ""
    # 台词/角色表在精简资产里可缺省
    assert scene.dialogues == [] and c.content.characters == []


def test_vision_adapter_merges_emotion_and_atmosphere():
    """老格式 emotion[].name 与精简格式 atmosphere[] 去重合并。"""
    raw = {
        "asset": {"asset_id": "m10", "title": "合并"},
        "content": {"scenes": [{
            "scene_id": "s1", "start_ms": 0, "end_ms": 1,
            "emotion": [{"name": "紧张"}], "atmosphere": ["紧张", "温情"],
        }]},
    }
    assert get_adapter("vision").adapt(raw).content.scenes[0].emotions == [
        "紧张", "温情"]
