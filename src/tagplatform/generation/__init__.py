"""生成引擎包出口：契约 dataclass、TagProducer 协议、注册表与 closed 生成器。

分层红线：本包不得 import fastapi（设计文档 §9.4）。协议与注册表定义在
`base` 子模块，此处重导出公共名字；closed 生成器（Task 5）在本包导入时
经模块级 register_producer 自动注册（open 生成器在任务 6 加入），故导入
本包后 list_producers() == ["closed"] 是预期初始态。
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
from tagplatform.generation.closed import ClosedTagProducer

__all__ = [
    "ClosedTagProducer",
    "Evidence",
    "GenerationPolicy",
    "GenerationSpec",
    "ProducedTag",
    "TagProducer",
    "get_producer",
    "list_producers",
    "register_producer",
]
