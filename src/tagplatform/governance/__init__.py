"""治理包出口：Excel 解析（Task 6）与治理流水线（Task 7）。

分层红线：本包不得 import fastapi（设计文档 §9.4）。`TagRow` /
`parse_tag_excel` 定义在 `excel` 子模块，供治理流水线与 API 层统一
从 `tagplatform.governance` 引用。
"""

from tagplatform.governance.excel import (
    ExcelParseResult,
    RowError,
    TagRow,
    parse_tag_excel,
)

__all__ = [
    "ExcelParseResult",
    "RowError",
    "TagRow",
    "parse_tag_excel",
]
