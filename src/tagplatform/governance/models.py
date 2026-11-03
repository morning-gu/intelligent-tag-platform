"""标签导入解析器共用的数据结构与字段规范化工具。"""

import re
from dataclasses import dataclass, field


REQUIRED_COLUMNS = ("tag_name", "tag_type", "description")
OPTIONAL_COLUMNS = ("tag_id", "parent_tag", "aliases", "applicable_scope", "status")
_ALIAS_SEPARATOR = re.compile(r"[,，;；]")


class BadCell(Exception):
    """单元格值无法可靠转换为文本。"""


@dataclass
class TagRow:
    """导入文件中的一行标签。"""

    tag_id: str | None
    tag_name: str
    tag_type: str
    description: str
    parent_tag: str | None
    aliases: list[str]
    applicable_scope: str | None
    status: str | None
    is_manual: bool = False


@dataclass
class RowError:
    """单行解析失败记录。"""

    row_number: int
    tag_name: str | None
    code: str
    detail: str


@dataclass
class ImportParseResult:
    """所有标签导入解析器共用的结果结构。"""

    rows: list[TagRow] = field(default_factory=list)
    errors: list[RowError] = field(default_factory=list)
    header_error: str | None = None


# 兼容已有 Excel API 名称；新格式可统一使用 ImportParseResult。
ExcelParseResult = ImportParseResult


def cell_text(value: object) -> str | None:
    """将输入值规范化为文本；空值和空白文本返回 None。"""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, bool):
        raise BadCell(value)
    if isinstance(value, (int, float)):
        return str(value)
    raise BadCell(value)


def split_aliases(text: str | None) -> list[str]:
    """按中英文逗号/分号拆分别名。"""
    if not text:
        return []
    return [part for part in (p.strip() for p in _ALIAS_SEPARATOR.split(text)) if part]


def build_tag_row(texts: dict[str, str | None]) -> TagRow:
    """由已规范化的字段构造标签行。"""
    return TagRow(
        tag_id=texts.get("tag_id"),
        tag_name=texts["tag_name"],
        tag_type=texts["tag_type"],
        description=texts["description"],
        parent_tag=texts.get("parent_tag"),
        aliases=split_aliases(texts.get("aliases")),
        applicable_scope=texts.get("applicable_scope"),
        status=texts.get("status"),
    )
