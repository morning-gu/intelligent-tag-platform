"""嵌入器包出口：`make_embedder` 是唯一构造入口（`build_embedder` 为
settings 便捷封装），契约类型 `Embedder` 供注解。

能力层定位（四层重构 Task 2）：本包是嵌入技术能力（确定性 mock / 本地
模型 / 远程通道三实现），检索与合并等业务算法不在此层；防滑坡规则：
流程编排、算法、领域规则禁止入层，业务知识上限 = schema 常量。
"""

from tagplatform.capabilities.embed.embedding import (
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
