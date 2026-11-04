"""上游（视频理解部门）格式适配器：kind="vision"。

把 `schemas/upstream.py` 的上游长视频理解 JSON 归一为平台统一
`ContentJSON`。字段映射与取舍见 `schemas/upstream.py` 的模块 docstring。

映射要点：
- `content.description` → `content.summary`（为空时回退首个非空场景描述）；
- 上游拍平的 `scenes[].start_ms/end_ms` → 平台 `scenes[].time_range`；
- `scenes[].emotion[].name` → `scenes[].emotions`；
- `scenes[].character_ids` → `scenes[].entities`（type="character"，名字取自
  角色表；角色表里查不到时以 id 自身作名字，保证证据可读）；
- `dialogues[].speaker_name/id` → `speaker`（姓名优先）；
- 缺时间戳的台词 / 音频事件补 0（它们不参与证据时间轴）；
- `metadata` 由本适配器补：source="video_understanding"、
  recognition_status="completed"（上游未定义该节，平台内部模型需要）。

不做的校验：场景描述/角色/台词全部可缺（“只要有标签需要的字段”）；重复
scene_id 自动加后缀（`_2`、`_3`…），避免 media_asset 主键冲突——上游同一
片段被切两次是数据问题，但不应让整批导入失败。

分层红线：本模块不得 import fastapi（设计文档 §9.4）。
"""

from tagplatform.schemas.content import (
    AssetInfo,
    AudioEvent,
    Character,
    CharacterEmotion,
    ContentBody,
    ContentJSON,
    Dialogue,
    DialogueEmotion,
    Entity,
    Event,
    MetaInfo,
    Scene,
    TimeRange,
)
from tagplatform.schemas.upstream import UpstreamPayload, parse_upstream

# 上游未定义 metadata：按“已完成识别”的语义补齐，前端据此显示上下文来源
_SOURCE = "video_understanding"
_RECOGNITION_STATUS = "completed"


def _merge_unique(items: list[str]) -> list[str]:
    """去重保序（氛围可能同时来自 emotion[] 与 atmosphere[]）。"""
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


class VisionAdapter:
    """上游长视频理解格式适配器（kind="vision"）。"""

    kind: str

    def __init__(self, kind: str = "vision") -> None:
        self.kind = kind

    def adapt(self, raw: dict) -> ContentJSON:
        """校验上游 raw dict 并转换为统一 ContentJSON（失败抛 ValidationError）。"""
        return to_content_json(parse_upstream(raw))


def _unique_scene_ids(up: UpstreamPayload) -> list[str]:
    """scene_id 去重：首次出现原样保留，重复的追加 _2/_3… 后缀。"""
    seen: dict[str, int] = {}
    out: list[str] = []
    for scene in up.content.scenes:
        sid = scene.scene_id
        if sid in seen:
            seen[sid] += 1
            sid = f"{scene.scene_id}_{seen[sid]}"
        else:
            seen[sid] = 1
            sid = scene.scene_id
        out.append(sid)
    return out


def _characters(up: UpstreamPayload) -> list[Character]:
    """角色表：缺 character_id 时按序生成（character_001 起）。"""
    rows: list[Character] = []
    for index, ch in enumerate(up.content.characters, start=1):
        rows.append(Character(
            character_id=ch.character_id or f"character_{index:03d}",
            name=ch.name,
            aliases=list(ch.aliases),
            description=ch.description,
            actor=ch.actor,
        ))
    return rows


def to_content_json(up: UpstreamPayload) -> ContentJSON:
    """上游 payload → 平台 ContentJSON（纯函数，便于测试直接调用）。"""
    characters = _characters(up)
    by_id = {c.character_id: c for c in characters}
    scene_ids = _unique_scene_ids(up)

    scenes: list[Scene] = []
    for sid, scene in zip(scene_ids, up.content.scenes):
        entities = [
            Entity(
                entity_id=cid,
                name=by_id[cid].name if cid in by_id else cid,
                type="character",
            )
            for cid in scene.character_ids
        ]
        dialogues = [
            Dialogue(
                speaker=(d.speaker_name or d.speaker_id or "未知说话人"),
                text=d.text,
                start_ms=d.start_ms or 0,
                end_ms=d.end_ms if d.end_ms is not None else (d.start_ms or 0),
                speaker_name=d.speaker_name,
                emotion=(DialogueEmotion(
                    text_emotion=d.emotion.text_emotion,
                    voice_emotion=d.emotion.voice_emotion,
                ) if d.emotion else None),
                dialogue_id=d.dialogue_id,
                speaker_id=d.speaker_id,
            )
            for d in scene.dialogues
        ]
        audio_events = [
            AudioEvent(
                event_type=a.event_type,
                description=a.description,
                start_ms=a.start_ms or 0,
                end_ms=a.end_ms if a.end_ms is not None else (a.start_ms or 0),
            )
            for a in scene.audio_events
        ]
        # 人物情绪：新口径挂在事件参与人上（events[].participant_ids[].emotion），
        # 旧口径用场景级 character_emotions；两路合并，按 (角色, 情绪) 去重。
        participant_emotions: list[CharacterEmotion] = []
        for event in scene.events:
            for item in event.participant_ids:
                if isinstance(item, str):
                    continue
                for emotion in item.emotion:
                    # 人物名：优先给的名字，其次用角色表按 id 反查（新契约只给 id）
                    cid = item.character_id
                    name = (item.character_name
                            or (by_id[cid].name if cid in by_id else cid) or "")
                    row = CharacterEmotion(
                        character=name,
                        emotion=emotion.name,
                        character_id=cid,
                    )
                    if row not in participant_emotions:
                        participant_emotions.append(row)
        scenes.append(Scene(
            scene_id=sid,
            time_range=TimeRange(start_ms=scene.start_ms, end_ms=scene.end_ms),
            description=scene.description or "",
            entities=entities,
            events=[Event(event_type="event", description=e.description,
                          event_id=e.event_id,
                          participant_ids=e.participant_cids())
                    for e in scene.events],
            character_ids=list(scene.character_ids),
            # 氛围：老格式 emotion[].name + 精简资产 atmosphere[]，去重保序合并
            emotions=_merge_unique([e.name for e in scene.emotion]
                                   + list(scene.atmosphere)),
            location=scene.location,
            virtual_time=scene.virtual_time,
            audio_events=audio_events,
            dialogues=dialogues,
            character_emotions=participant_emotions + [
                CharacterEmotion(character=ce.name, emotion=ce.emotion,
                                 character_id=ce.character_id)
                for ce in scene.character_emotions
                if CharacterEmotion(character=ce.name, emotion=ce.emotion,
                                    character_id=ce.character_id)
                not in participant_emotions],
            background_music=scene.background_music,
        ))

    summary = (up.content.description or "").strip()
    if not summary:
        # 上游只给场景、不给整片描述：用首个非空场景描述兜底（召回/生成都要它）
        summary = next((s.description.strip() for s in scenes
                        if (s.description or "").strip()), "")

    return ContentJSON(
        schema_version="1.0",
        asset=AssetInfo(
            asset_id=up.asset.asset_id,
            asset_type=up.asset.asset_type,  # 非法取值由 ContentJSON 的 Literal 拦截
            title=up.asset.title,
            duration_ms=up.asset.duration_ms,
            video_uri=up.asset.video_uri,
            language=up.asset.language,
        ),
        content=ContentBody(summary=summary, characters=characters, scenes=scenes),
        metadata=MetaInfo(
            source=_SOURCE,
            model_version=None,
            recognition_status=_RECOGNITION_STATUS,
        ),
    )
