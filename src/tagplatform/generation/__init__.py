"""生成引擎包出口：契约 dataclass、TagProducer 协议与注册表（Task 3）。

分层红线：本包不得 import fastapi（设计文档 §9.4）。协议与注册表
定义在 `base` 子模块，此处重导出公共名字。本任务不注册任何实现——
closed/open 生成器在任务 5/6 的各自模块导入时调用 register_producer
自动注册，故导入本包后 list_producers() 为空是预期初始态。
"""

from tagplatform.generation.base import (
    Evidence,
    GenerationPolicy,
    GenerationSpec,
    ProducedTag,
    TagProducer,
    get_producer,
    list_producers,
    register_producer,
)

__all__ = [
    "Evidence",
    "GenerationPolicy",
    "GenerationSpec",
    "ProducedTag",
    "TagProducer",
    "get_producer",
    "list_producers",
    "register_producer",
]
