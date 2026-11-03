"""治理包出口：标签导入解析与治理流水线。

分层红线：本包不得 import fastapi（设计文档 §9.4）。`TagRow` /
`parse_tag_excel` 定义在 `excel` 子模块，供治理流水线与 API 层统一
从 `tagplatform.governance` 引用。
"""

from tagplatform.governance.excel import (
    ExcelParseResult,
    ImportParseResult,
    MANUAL_TAG_TYPE,
    NEW_EXCEL_COLUMNS,
    RowError,
    TagRow,
    parse_tag_excel,
    print_parse_result,
)
from tagplatform.governance.xml import parse_tag_xml
from tagplatform.governance.pipeline import (
    GovernanceResult,
    Issue,
    run_governance,
)

__all__ = [
    "ExcelParseResult",
    "ImportParseResult",
    "MANUAL_TAG_TYPE",
    "NEW_EXCEL_COLUMNS",
    "GovernanceResult",
    "Issue",
    "RowError",
    "TagRow",
    "parse_tag_excel",
    "parse_tag_xml",
    "print_parse_result",
    "run_governance",
]
