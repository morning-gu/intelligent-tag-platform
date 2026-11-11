"""生成结果过滤责任链包出口：环节协议、链注册表与默认两环节。

默认链顺序（"任务完成生成标签后，不直接展示"）：
1. `quality`（质量评估）——当前占位，原样透传；
2. `fence`（信息围栏）——当前占位，原样透传。

两环节在本包导入时经模块级 `register_filter` 自动注册，故导入本包后
`list_filters() == ["quality", "fence"]` 是预期初始态（顺序即执行序）。

分层红线：本包不得 import fastapi（设计文档 §9.4）。
"""

from tagplatform.filtering.base import (
    FilterContext,
    FilterOutcome,
    StageReport,
    TagFilter,
    list_filters,
    register_filter,
    run_filter_chain,
)
from tagplatform.filtering.fence import InfoFenceFilter
from tagplatform.filtering.quality import QualityFilter

# 默认责任链：质量评估 → 信息围栏（顺序即语义，勿调换）
register_filter(QualityFilter())
register_filter(InfoFenceFilter())

__all__ = [
    "FilterContext",
    "FilterOutcome",
    "InfoFenceFilter",
    "QualityFilter",
    "StageReport",
    "TagFilter",
    "list_filters",
    "register_filter",
    "run_filter_chain",
]