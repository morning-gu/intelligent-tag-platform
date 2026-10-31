"""适配器包出口：ContentAdapter 协议、注册表与平台格式适配器（Task 8）。

分层红线：本包不得 import fastapi（设计文档 §9.4）。协议与注册表定义
在 `base` 子模块，平台格式适配器定义在 `platform` 子模块；此处重导出
公共名字，并在包导入时自动注册平台格式适配器（kind="platform"）。
"""

from tagplatform.adapters.base import (
    ContentAdapter,
    get_adapter,
    list_adapters,
    register_adapter,
)
from tagplatform.adapters.platform import PlatformAdapter

# 包导入时自动注册平台格式适配器（kind="platform"）
register_adapter(PlatformAdapter())

__all__ = [
    "ContentAdapter",
    "PlatformAdapter",
    "get_adapter",
    "list_adapters",
    "register_adapter",
]
