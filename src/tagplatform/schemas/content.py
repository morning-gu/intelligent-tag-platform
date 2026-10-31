"""统一内容 JSON 模型（设计文档 §3.3.1，schema v1.0）。

封闭式标签提取与开放式标签生成共用的数据输入格式。本模块为纯
Pydantic 模型层，不触碰数据库；Task 8（适配器）、Task 9（内容导入）、
Task 11（API）经 `parse_content` 消费本模型树。

契约要点：
- `schema_version` 固定为 "1.0"，格式演进经版本号管理；
- 时间戳（TimeRange/Dialogue）仅约束非负与 end_ms >= start_ms，支持
  标签证据追溯；
- 不加多余校验：场景数可为 0，空字符串交由后续治理判断。
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


class Dialogue(BaseModel):
    """单条台词：speaker/text 加与 TimeRange 相同的时间戳约束。"""

    speaker: str
    text: str
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def _check_order(self) -> "Dialogue":
        if self.end_ms < self.start_ms:
            raise ValueError("end_ms 必须大于等于 start_ms")
        return self


class Entity(BaseModel):
    """场景内识别出的实体（人物/地点/物品等）。"""

    entity_id: str
    name: str
    type: str


class Event(BaseModel):
    """场景内识别出的事件。"""

    event_type: str
    description: str


class Scene(BaseModel):
    """单个场景：时间区间 + 描述 + 多模态识别结果（四类列表默认为空）。"""

    scene_id: str
    time_range: TimeRange
    description: str
    visual_features: list[str] = Field(default_factory=list)
    dialogues: list[Dialogue] = Field(default_factory=list)
    entities: list[Entity] = Field(default_factory=list)
    events: list[Event] = Field(default_factory=list)


class ContentBody(BaseModel):
    """内容正文：整体剧情摘要 + 场景列表（可为 0 个场景）。"""

    summary: str
    scenes: list[Scene]


class AssetInfo(BaseModel):
    """资产元信息：影片、剧集、场景三级结构共用，子级经 parent_id 挂父级。"""

    asset_id: str | None = None
    # 取值 movie|series|scene
    asset_type: Literal["movie", "series", "scene"]
    title: str
    duration_ms: int | None = Field(default=None, ge=0)
    video_uri: str | None = None
    parent_id: str | None = None


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
