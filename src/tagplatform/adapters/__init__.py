"""适配器包出口：ContentAdapter 协议、注册表与各来源格式适配器。

分层红线：本包不得 import fastapi（设计文档 §9.4）。协议与注册表定义
在 `base` 子模块；`platform` 是平台标准格式（identity），`vision` 是
上游视频理解部门的长视频理解格式（拍平时间戳 + 角色表 + 场景情绪/音频
事件）。此处重导出公共名字，并在包导入时自动注册两个适配器
（kind="platform" / kind="vision"）。
"""

from tagplatform.adapters.base import (
    ContentAdapter,
    get_adapter,
    list_adapters,
    register_adapter,
)
from tagplatform.adapters.platform import PlatformAdapter
from tagplatform.adapters.vision import VisionAdapter, to_content_json

# 包导入时自动注册（kind 重复会抛 ValueError，故各自只注册一次）
register_adapter(PlatformAdapter())
register_adapter(VisionAdapter())

__all__ = [
    "ContentAdapter",
    "PlatformAdapter",
    "VisionAdapter",
    "get_adapter",
    "list_adapters",
    "register_adapter",
    "to_content_json",
]
