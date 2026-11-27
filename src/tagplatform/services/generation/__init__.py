"""生成引擎包出口：契约 dataclass、TagProducer 协议、注册表与 closed/open 生成器。

分层红线：本包不得 import fastapi（设计文档 §9.4）。协议与注册表定义在
`base` 子模块，此处重导出公共名字；closed 与 open 生成器在各自模块导入时
经模块级 register_producer 自动注册，故导入本包后
list_producers() == ["closed", "open"] 是预期初始态。

子包结构（四层重构 Task 4）：
- `base`：TagProducer 协议 + 契约 dataclass + 注册表；
- `closed/`：模式 A——producer（封闭提取）/ recall（向量召回）/
  parse（LLM 输出契约解析）；
- `open/`：模式 B——producer（注册入口）+ mtg/（MovieTagGen 移植管线）；
- `review/`：人工审核服务（pre_asset_tag 四动作行操作）。
"""

from tagplatform.services.generation.base import (
    Evidence,
    GenerationPolicy,
    GenerationSpec,
    ProducedTag,
    TagProducer,
    get_producer,
    list_producers,
    register_producer,
)
from tagplatform.services.generation.closed.producer import ClosedTagProducer
from tagplatform.services.generation.open.producer import OpenTagProducer

__all__ = [
    "ClosedTagProducer",
    "OpenTagProducer",
    "Evidence",
    "GenerationPolicy",
    "GenerationSpec",
    "ProducedTag",
    "TagProducer",
    "get_producer",
    "list_producers",
    "register_producer",
]
