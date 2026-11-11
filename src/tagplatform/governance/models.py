"""标签导入解析器共用的数据结构与字段规范化工具。"""

import re
from dataclasses import dataclass, field


REQUIRED_COLUMNS = ("tag_name", "tag_type", "description")
REQUIRED_VALUE_COLUMNS = ("tag_name", "tag_type")
OPTIONAL_COLUMNS = ("tag_id", "parent_tag", "aliases", "applicable_scope", "status")
# 树状 XML 的目录节点带 dir_type 属性（dir_static|dir_dynamic）；扁平
# 模板无此列，TagRow.dir_type 为 None（入库层归一默认 dir_dynamic）
_ALIAS_SEPARATOR = re.compile(r"[,，;；|]")


class BadCell(Exception):
    """单元格值无法可靠转换为文本。"""


@dataclass
class TagRow:
    """导入文件中的一行：目录行或标签行（结构同形，靠 is_dir 区分）。"""

    tag_id: str | None
    tag_name: str
    tag_type: str
    description: str
    parent_tag: str | None
    aliases: list[str]
    applicable_scope: str | None
    status: str | None
    is_manual: bool = False
    # 目录类型（dir_static|dir_dynamic），仅树状 XML 目录节点有值；
    # None 时入库层归一为 dir_dynamic
    dir_type: str | None = None
    # 目录行显式标记（三态）：解析层本来就知道该行是目录（双表模板的
    # 「标签目录」sheet、层级模板的一/二/三级列、树状 XML 的 level1/2/3
    # 节点）时置 True，知道是标签（「标签定义」sheet、标签列、<tag> 节点）
    # 时置 False；None = 解析层未表态，由治理流水线按"名称被批内其他行
    # 引用即目录"推断兜底（旧扁平模板、直接构造 TagRow 的调用方）。
    # **空目录**（其下没有任何标签行）只有靠本标记才保得住"目录"身份——
    # 否则会被推断当成标签行落库。
    is_dir: bool | None = None


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
    """按中英文逗号/分号/竖线拆分别名（模板约定多个别名以 | 分隔）。"""
    if not text:
        return []
    return [part for part in (p.strip() for p in _ALIAS_SEPARATOR.split(text)) if part]


def build_tag_row(texts: dict[str, str | None]) -> TagRow:
    """由已规范化的字段构造标签行。"""
    return TagRow(
        tag_id=texts.get("tag_id"),
        tag_name=texts["tag_name"],
        tag_type=texts["tag_type"],
        description=texts.get("description") or "",
        parent_tag=texts.get("parent_tag"),
        aliases=split_aliases(texts.get("aliases")),
        applicable_scope=texts.get("applicable_scope"),
        status=texts.get("status"),
    )
