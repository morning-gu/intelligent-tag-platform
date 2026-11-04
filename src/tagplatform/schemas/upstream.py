"""上游（视频理解部门）长视频理解 JSON 模型：宽松解析 + 转平台 ContentJSON。

来源格式见仓库根 `图谱&标签与长视频理解数据对齐.txt`（2026-11 与视频理解
部门对齐的初始契约）。与平台内部 `ContentJSON` 的差异：

| 上游 | 平台内部 | 处理 |
|---|---|---|
| `content.description` | `content.summary` | 直接映射；为空时回退首个非空场景描述 |
| `scenes[].start_ms/end_ms` | `scenes[].time_range` | 拍平 ↔ 嵌套 |
| `content.characters[]` | `content.characters[]` | 保留；缺 `character_id` 时按序生成 |
| `scenes[].emotion[].name` | `scenes[].emotions[]` | 取 name 列表 |
| `scenes[].character_ids[]` | `scenes[].entities[]` | 解析成人物实体（type="character"） |
| `scenes[].audio_events[]` | `scenes[].audio_events[]` | 保留；缺时间戳补 0 |
| `dialogues[].speaker_id/name` | `dialogues[].speaker` | 优先姓名，其次 id |
| — | `metadata` | 由适配器补 `source=video_understanding` |

设计取舍（“不需要每个字段都必填，只要有标签需要的即可”）：
- 必填仅 `asset.asset_id/title`、`content.scenes[].scene_id/start_ms/end_ms`；
- 其余一律可选，缺省即空——上游识别能力逐步补齐时不阻塞导入；
- `extra="ignore"`：上游加字段不会打挂导入（对比平台的严格模型）；
- 时间戳只校验 `end_ms >= start_ms`，不做时长上限假设（长视频可达数小时）。
"""
from pydantic import BaseModel, ConfigDict, Field, model_validator


class UpstreamAsset(BaseModel):
    """上游资产元信息。"""

    model_config = ConfigDict(extra="ignore")

    asset_id: str
    asset_type: str = "movie"
    title: str
    duration_ms: int | None = Field(default=None, ge=0)
    video_uri: str | None = None
    language: str | None = None


class UpstreamCharacter(BaseModel):
    """上游角色实体（剧内知识图谱用）。

    对齐文档要求「角色ID、名称、别名、对应的演员、出现的时间、台词及时间点」；
    示例 JSON 里没有演员字段，这里按加法方式补 `actor`（缺省 None，不破坏解析）。
    """

    model_config = ConfigDict(extra="ignore")

    character_id: str | None = None
    name: str
    aliases: list[str] = Field(default_factory=list)
    description: str | None = None
    actor: str | None = None


class UpstreamEmotion(BaseModel):
    """情绪条目：`{name, code}`；早期数据只有 name（code 缺省 None）。"""

    model_config = ConfigDict(extra="ignore")

    name: str
    code: str | None = None


class UpstreamParticipant(BaseModel):
    """事件参与人：`{character_id, emotion:[{name, code}]}`（new_json.txt 口径）。

    早期资产里 `participant_ids` 是纯字符串数组，两种写法都收。
    """

    model_config = ConfigDict(extra="ignore")

    character_id: str | None = None
    character_name: str | None = None
    emotion: list[UpstreamEmotion] = Field(default_factory=list)


class UpstreamEvent(BaseModel):
    """场景事件：参与人带情绪（新口径）或纯 id 列表（旧口径）。"""

    model_config = ConfigDict(extra="ignore")

    event_id: str | None = None
    description: str
    participant_ids: list[UpstreamParticipant | str] = Field(default_factory=list)

    def participant_cids(self) -> list[str]:
        """参与人 id 列表（对象取 character_id，字符串原样）。"""
        out: list[str] = []
        for item in self.participant_ids:
            cid = item if isinstance(item, str) else item.character_id
            if cid and cid not in out:
                out.append(cid)
        return out


class UpstreamDialogueEmotion(BaseModel):
    """台词情绪：文本情绪 + 语音情绪。"""

    model_config = ConfigDict(extra="ignore")

    text_emotion: str | None = None
    voice_emotion: str | None = None


class UpstreamCharacterEmotion(BaseModel):
    """场景内某个角色的情绪（模板扩展字段，两种写法都收）。

    新写法（对齐文档 + 本平台生成）：`{"character_id","character_name","emotion"}`
    旧写法（早期精简资产）：`{"character","emotion"}`
    """

    model_config = ConfigDict(extra="ignore")

    character: str | None = None
    character_name: str | None = None
    character_id: str | None = None
    emotion: str

    @model_validator(mode="after")
    def _check_name(self) -> "UpstreamCharacterEmotion":
        if not (self.character or self.character_name or self.character_id):
            raise ValueError("character_emotions 缺少人物标识")
        return self

    @property
    def name(self) -> str:
        """统一取人物显示名（姓名优先，其次旧字段，最后 id）。"""
        return (self.character_name or self.character or self.character_id or "")


class UpstreamDialogue(BaseModel):
    """上游台词：说话人以 id 标识，姓名与情绪可选。"""

    model_config = ConfigDict(extra="ignore")

    dialogue_id: str | None = None
    speaker_id: str | None = None
    speaker_name: str | None = None
    text: str
    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, ge=0)
    emotion: UpstreamDialogueEmotion | None = None

    @model_validator(mode="after")
    def _check_order(self) -> "UpstreamDialogue":
        if (self.start_ms is not None and self.end_ms is not None
                and self.end_ms < self.start_ms):
            raise ValueError("dialogues.end_ms 必须大于等于 start_ms")
        return self


class UpstreamAudioEvent(BaseModel):
    """上游音频事件：背景音乐 / 音效。"""

    model_config = ConfigDict(extra="ignore")

    event_type: str
    description: str
    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _check_order(self) -> "UpstreamAudioEvent":
        if (self.start_ms is not None and self.end_ms is not None
                and self.end_ms < self.start_ms):
            raise ValueError("audio_events.end_ms 必须大于等于 start_ms")
        return self


class UpstreamScene(BaseModel):
    """上游场景：时间戳拍平在场景上（非 time_range 嵌套）。"""

    model_config = ConfigDict(extra="ignore")

    scene_id: str
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    description: str | None = None
    virtual_time: str | None = None
    location: str | None = None
    character_ids: list[str] = Field(default_factory=list)
    events: list[UpstreamEvent] = Field(default_factory=list)
    emotion: list[UpstreamEmotion] = Field(default_factory=list)
    dialogues: list[UpstreamDialogue] = Field(default_factory=list)
    audio_events: list[UpstreamAudioEvent] = Field(default_factory=list)
    # ---- 2026-11 精简资产（无台词、无音频版） ----
    atmosphere: list[str] = Field(default_factory=list)
    character_emotions: list[UpstreamCharacterEmotion] = Field(default_factory=list)
    background_music: str | None = None

    @model_validator(mode="after")
    def _check_order(self) -> "UpstreamScene":
        if self.end_ms < self.start_ms:
            raise ValueError("scenes.end_ms 必须大于等于 start_ms")
        return self


class UpstreamContent(BaseModel):
    """上游内容正文：整体剧情描述 + 角色表 + 场景列表。"""

    model_config = ConfigDict(extra="ignore")

    description: str | None = None
    characters: list[UpstreamCharacter] = Field(default_factory=list)
    scenes: list[UpstreamScene] = Field(default_factory=list)


class UpstreamPayload(BaseModel):
    """上游长视频理解 JSON 顶层结构。"""

    model_config = ConfigDict(extra="ignore")

    schema_version: str = "1.0"
    asset: UpstreamAsset
    content: UpstreamContent


def parse_upstream(data: dict) -> UpstreamPayload:
    """校验并解析上游 JSON，失败抛 pydantic.ValidationError。"""
    return UpstreamPayload.model_validate(data)
