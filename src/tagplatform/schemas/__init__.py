"""统一内容 JSON 模型层（纯 Pydantic）。

分层红线：本包不得 import fastapi（设计文档 §9.4）。
模型树与 `parse_content` 定义在 `content` 子模块；此处重导出公共
名字，供适配器、内容导入与 API 层统一从 `tagplatform.schemas` 引用。
"""

from tagplatform.schemas.content import (
    AssetInfo,
    ContentBody,
    ContentJSON,
    Dialogue,
    Entity,
    Event,
    MetaInfo,
    Scene,
    TimeRange,
    parse_content,
)

__all__ = [
    "AssetInfo",
    "ContentBody",
    "ContentJSON",
    "Dialogue",
    "Entity",
    "Event",
    "MetaInfo",
    "Scene",
    "TimeRange",
    "parse_content",
]
