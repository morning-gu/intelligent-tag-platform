"""统一内容 JSON 模型（设计文档 §3.3.1，schema v1.0 + 上游扩展字段）。

封闭式标签提取与开放式标签生成共用的数据输入格式。本模块为纯
Pydantic 模型层，不触碰数据库；Task 8（适配器）、Task 9（内容导入）、
Task 11（API）经 `parse_content` 消费本模型树。

契约要点：
- `schema_version` 固定为 "1.0"，格式演进经版本号管理；
- 时间戳（TimeRange/Dialogue/AudioEvent）仅约束非负与 end_ms >= start_ms，
  支持标签证据追溯；
- 不加多余校验：场景数可为 0，空字符串交由后续治理判断。

2026-11 上游对接扩展（全部可选、向后兼容）：
- `asset.language`：内容语种（如 zh-CN），仅透传展示；
- `content.characters`：角色表（视频理解部门「剧内知识图谱」要求的角色
  实体），标签生成时作为人物线索注入 prompt；
- `scene.emotions`：场景整体氛围（如 紧张/压迫），mood 类标签的核心输入；
- `scene.location` / `scene.virtual_time`：剧情地点与剧情时间（上游质量
  不保证，仅透传，不进 prompt 的硬约束）；
- `scene.audio_events`：背景音乐/音效描述，氛围类标签的补充证据；
- `dialogue.speaker_name` / `dialogue.emotion`：说话人姓名与情绪
  （text_emotion/voice_emotion），台词证据的可读性来源。

老数据（不含上述字段）解析结果与扩展前逐字一致：新字段一律有默认值，
Scene.description 由必填放宽为默认空串（上游允许只给时间与人物）。
"""
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class TimeRange(BaseModel):
    """毫秒时间区间：标签证据追溯的最小时间单位。"""

    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def _check_order(self) -> "TimeRange":
        if self.end_ms < self.start_ms:
            raise ValueError("end_ms 必须大于等于 start_ms")
        return self


class DialogueEmotion(BaseModel):
    """单条台词的情绪：文本情绪与语音情绪（上游可选，缺省 None）。"""

    text_emotion: str | None = None
    voice_emotion: str | None = None


class Dialogue(BaseModel):
    """单条台词：speaker/text 加与 TimeRange 相同的时间戳约束。"""

    speaker: str
    text: str
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    # 上游扩展：说话人姓名（speaker 可能是 character_id）与台词情绪
    speaker_name: str | None = None
    emotion: DialogueEmotion | None = None
    # 上游模板字段（对齐文档 2026-11）：稳定 id 与角色 id
    dialogue_id: str | None = None
    speaker_id: str | None = None

    @model_validator(mode="after")
    def _check_order(self) -> "Dialogue":
        if self.end_ms < self.start_ms:
            raise ValueError("end_ms 必须大于等于 start_ms")
        return self


class AudioEvent(BaseModel):
    """音频事件：背景音乐 / 音效（上游扩展，标签的氛围证据）。"""

    event_type: str
    description: str
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def _check_order(self) -> "AudioEvent":
        if self.end_ms < self.start_ms:
            raise ValueError("end_ms 必须大于等于 start_ms")
        return self


class Character(BaseModel):
    """角色实体（上游扩展）：影视内容中的出场人物。"""

    character_id: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    description: str | None = None
    # 对齐文档「剧内知识图谱」要求 1 的"对应的演员"（示例 JSON 未列，属加法字段）
    actor: str | None = None


class Entity(BaseModel):
    """场景内识别出的实体（人物/地点/物品等）。"""

    entity_id: str
    name: str
    type: str


class Event(BaseModel):
    """场景内识别出的事件。"""

    event_type: str
    description: str
    # 上游模板字段（对齐文档 2026-11）：事件 id 与参与角色
    event_id: str | None = None
    participant_ids: list[str] = Field(default_factory=list)


class CharacterEmotion(BaseModel):
    """场景内某个角色的情绪（上游 VLM 抽取，人物名对齐角色表）。"""

    character: str
    emotion: str
    # 上游模板扩展：可回指 content.characters 的 character_id
    character_id: str | None = None


class Scene(BaseModel):
    """单个场景：时间区间 + 描述 + 多模态识别结果（四类列表默认为空）。"""

    scene_id: str
    time_range: TimeRange
    # 上游允许只给时间与人物，剧情描述可缺省（标签生成会退回整体描述）
    description: str = ""
    visual_features: list[str] = Field(default_factory=list)
    dialogues: list[Dialogue] = Field(default_factory=list)
    entities: list[Entity] = Field(default_factory=list)
    events: list[Event] = Field(default_factory=list)
    # ---- 上游扩展（2026-11 对接视频理解部门） ----
    emotions: list[str] = Field(default_factory=list)
    location: str | None = None
    virtual_time: str | None = None
    audio_events: list[AudioEvent] = Field(default_factory=list)
    # 上游模板字段：本场景出场角色的 character_id（与 content.characters 对齐）
    character_ids: list[str] = Field(default_factory=list)
    # ---- 2026-11 精简资产（无音频、无台词版本） ----
    character_emotions: list[CharacterEmotion] = Field(default_factory=list)
    background_music: str | None = None


class ContentBody(BaseModel):
    """内容正文：整体剧情摘要 + 角色表 + 场景列表（可为 0 个场景）。"""

    summary: str
    scenes: list[Scene]
    characters: list[Character] = Field(default_factory=list)


class AssetInfo(BaseModel):
    """资产元信息：影片、剧集、场景三级结构共用，子级经 parent_id 挂父级。"""

    asset_id: str | None = None
    # 取值 movie|series|scene
    asset_type: Literal["movie", "series", "scene"]
    title: str
    duration_ms: int | None = Field(default=None, ge=0)
    video_uri: str | None = None
    parent_id: str | None = None
    # 上游扩展：内容语种（如 zh-CN）
    language: str | None = None


class MetaInfo(BaseModel):
    """识别元信息：数据来源与识别状态。"""

    source: str
    model_version: str | None = None
    recognition_status: str


class ContentJSON(BaseModel):
    """统一影视内容 JSON 顶层结构（schema v1.0）。"""

    schema_version: Literal["1.0"]
    asset: AssetInfo
    content: ContentBody
    metadata: MetaInfo


def parse_content(data: dict) -> ContentJSON:
    """校验并解析统一内容 JSON，失败抛 pydantic.ValidationError。"""
    return ContentJSON.model_validate(data)
