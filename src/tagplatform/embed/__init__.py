"""嵌入器包出口：`make_embedder` 是唯一构造入口（`build_embedder` 为
settings 便捷封装），契约类型 `Embedder` 供注解。"""

from tagplatform.embed.embedding import (
    Embedder,
    LocalEmbedder,
    MockEmbedder,
    RemoteEmbedder,
    build_embedder,
    make_embedder,
)

__all__ = [
    "Embedder",
    "LocalEmbedder",
    "MockEmbedder",
    "RemoteEmbedder",
    "build_embedder",
    "make_embedder",
]
