"""嵌入器包出口：`make_embedder` 是唯一构造入口，契约类型 `Embedder` 供注解。"""

from tagplatform.embed.embedding import (
    Embedder,
    LocalEmbedder,
    MockEmbedder,
    make_embedder,
)

__all__ = ["Embedder", "LocalEmbedder", "MockEmbedder", "make_embedder"]
