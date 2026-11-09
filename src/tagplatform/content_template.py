"""资产内容 JSON 的模板形态（唯一存储/对外口径 = `new_json.txt`）。

平台内部仍用 `ContentJSON`（summary / time_range / emotions 字符串）做标签
生成与证据计算；但**资产 JSON 本身**（`media_asset.content_json` 与
`GET /assets/{id}/content` 的返回值）一律是上游模板形态：

```
schema_version / asset{asset_id,asset_type,title,duration_ms,video_uri,language}
content{description, characters[], scenes[]}
scenes[]{scene_id,start_ms,end_ms,description,virtual_time,location,
         character_ids[],events[],emotion[],dialogues[],audio_events[]}
```

字段完整性规则（与需求一致）：
- 模板里的每个字段都必须出现；
- 有数据就填值，没有数据写 `null`（对象/标量）；
- 列表字段没有数据写 `[]`（保持数组类型，下游遍历安全）。

两条来源路径：
- `to_template(upstream)`：上游 payload → 模板（信息最全，`emotion.code`、
  事件参与人情绪都在）。导入与读取上游形态时走它；
- `to_template_from_content(content)`：内部 `ContentJSON` → 模板（兜底：
  平台标准格式导入、2026-11 之前的历史内部形态）。情绪 code 按词表反查，
  查不到写 `null`；事件参与人情绪按角色回填（内部模型不保留“哪个事件”）。
"""

from __future__ import annotations

import json
from typing import Any

from tagplatform.assets.emotions import to_emotion
from tagplatform.content_io import is_upstream_shape
from tagplatform.schemas.content import ContentJSON, parse_content
from tagplatform.schemas.upstream import UpstreamPayload, parse_upstream

TEMPLATE_SCHEMA_VERSION = "1.0"


def _text(value: str | None) -> str | None:
    """空字符串/纯空白视为“没数据”→ null。"""
    if value is None:
        return None
    return value if value.strip() else None


def _code(name: str | None) -> str | None:
    """情绪中文名 → 词表 code；词表外返回 None（名称保留，code 为 null）。"""
    if not name:
        return None
    item = to_emotion(name)
    return item["code"] if item else None


def _emotion_item(name: str | None, code: str | None = None) -> dict[str, Any]:
    return {"name": name, "code": code if code is not None else _code(name)}


def _emotion_list(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """丢弃 name/code 都为空的占位项（“没数据”就不该出现空对象）。"""
    return [item for item in items if item["name"] or item["code"]]


def _asset_block(
    asset_id: str | None,
    asset_type: str,
    title: str,
    duration_ms: int | None,
    video_uri: str | None,
    language: str | None,
) -> dict[str, Any]:
    return {
        "asset_id": asset_id,
        "asset_type": asset_type,
        "title": title,
        "duration_ms": duration_ms,
        "video_uri": video_uri,
        "language": language,
    }


def _characters_from_upstream(up: UpstreamPayload) -> list[dict[str, Any]]:
    return [
        {
            "character_id": ch.character_id,
            "name": ch.name,
            "aliases": list(ch.aliases),
            "description": _text(ch.description),
        }
        for ch in up.content.characters
    ]


def _participant_from_upstream(item: Any) -> dict[str, Any]:
    """上游参与人：对象（新口径）或纯字符串 id（旧口径）都归一成对象。"""
    if isinstance(item, str):
        return {"character_id": item, "emotion": []}
    return {
        "character_id": item.character_id,
        "emotion": _emotion_list([_emotion_item(e.name, e.code)
                                  for e in item.emotion]),
    }


def _scenes_from_upstream(up: UpstreamPayload) -> list[dict[str, Any]]:
    scenes: list[dict[str, Any]] = []
    for scene in up.content.scenes:
        scenes.append({
            "scene_id": scene.scene_id,
            "start_ms": scene.start_ms,
            "end_ms": scene.end_ms,
            "description": _text(scene.description),
            "virtual_time": _text(scene.virtual_time),
            "location": _text(scene.location),
            "character_ids": list(scene.character_ids),
            "events": [
                {
                    "event_id": event.event_id,
                    "description": event.description,
                    "participant_ids": [
                        _participant_from_upstream(p) for p in event.participant_ids
                    ],
                }
                for event in scene.events
            ],
            "emotion": _emotion_list([_emotion_item(e.name, e.code)
                                      for e in scene.emotion]),
            "dialogues": [
                {
                    "dialogue_id": d.dialogue_id,
                    "speaker_id": d.speaker_id,
                    "speaker_name": _text(d.speaker_name),
                    "text": d.text,
                    "start_ms": d.start_ms,
                    "end_ms": d.end_ms,
                }
                for d in scene.dialogues
            ],
            "audio_events": [
                {
                    "event_type": a.event_type,
                    "description": a.description,
                    "start_ms": a.start_ms,
                    "end_ms": a.end_ms,
                }
                for a in scene.audio_events
            ],
        })
    return scenes


def to_template(up: UpstreamPayload) -> dict[str, Any]:
    """上游 payload → 完整模板 JSON（缺失值 null / []）。"""
    return {
        "schema_version": TEMPLATE_SCHEMA_VERSION,
        "asset": _asset_block(
            up.asset.asset_id,
            up.asset.asset_type,
            up.asset.title,
            up.asset.duration_ms,
            up.asset.video_uri,
            up.asset.language,
        ),
        "content": {
            "description": _text(up.content.description),
            "characters": _characters_from_upstream(up),
            "scenes": _scenes_from_upstream(up),
        },
    }


def _emotion_lookup(content: ContentJSON) -> dict[str, list[dict[str, Any]]]:
    """角色 → 情绪列表（内部模型只在场景级保留人物情绪）。"""
    mapping: dict[str, list[dict[str, Any]]] = {}
    for scene in content.content.scenes:
        for row in scene.character_emotions:
            key = row.character_id or row.character
            if not key:
                continue
            item = _emotion_item(row.emotion)
            if not item["name"] and not item["code"]:
                continue
            bucket = mapping.setdefault(key, [])
            if item not in bucket:
                bucket.append(item)
    return mapping


def _scenes_from_content(content: ContentJSON) -> list[dict[str, Any]]:
    emotions_by_character = _emotion_lookup(content)
    scenes: list[dict[str, Any]] = []
    for scene in content.content.scenes:
        scenes.append({
            "scene_id": scene.scene_id,
            "start_ms": scene.time_range.start_ms,
            "end_ms": scene.time_range.end_ms,
            "description": _text(scene.description),
            "virtual_time": _text(scene.virtual_time),
            "location": _text(scene.location),
            "character_ids": list(scene.character_ids),
            "events": [
                {
                    "event_id": event.event_id,
                    "description": event.description,
                    "participant_ids": [
                        {
                            "character_id": cid,
                            "emotion": list(emotions_by_character.get(cid, [])),
                        }
                        for cid in event.participant_ids
                    ],
                }
                for event in scene.events
            ],
            "emotion": _emotion_list([_emotion_item(name)
                                      for name in scene.emotions]),
            "dialogues": [
                {
                    "dialogue_id": d.dialogue_id,
                    "speaker_id": d.speaker_id,
                    "speaker_name": _text(d.speaker_name or d.speaker),
                    "text": d.text,
                    "start_ms": d.start_ms,
                    "end_ms": d.end_ms,
                }
                for d in scene.dialogues
            ],
            "audio_events": [
                {
                    "event_type": a.event_type,
                    "description": a.description,
                    "start_ms": a.start_ms,
                    "end_ms": a.end_ms,
                }
                for a in scene.audio_events
            ],
        })
    return scenes


def to_template_from_content(content: ContentJSON) -> dict[str, Any]:
    """内部 ContentJSON → 完整模板 JSON（兜底路径，情绪 code 词表反查）。"""
    characters = [
        {
            "character_id": ch.character_id,
            "name": ch.name,
            "aliases": list(ch.aliases),
            "description": _text(ch.description),
        }
        for ch in content.content.characters
    ]
    return {
        "schema_version": TEMPLATE_SCHEMA_VERSION,
        "asset": _asset_block(
            content.asset.asset_id,
            content.asset.asset_type,
            content.asset.title,
            content.asset.duration_ms,
            content.asset.video_uri,
            content.asset.language,
        ),
        "content": {
            "description": _text(content.content.summary),
            "characters": characters,
            "scenes": _scenes_from_content(content),
        },
    }


def template_from_stored(raw_json: str) -> dict[str, Any]:
    """库里存的 JSON 文本 → 完整模板 JSON（两种历史形态都能读）。"""
    data = json.loads(raw_json)
    if is_upstream_shape(data):
        return to_template(parse_upstream(data))
    return to_template_from_content(parse_content(data))
