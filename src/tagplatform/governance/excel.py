"""Excel 标签模板解析：xlsx 字节流 → TagRow 行对象 + 行级错误报告。

纯解析层：只做"单元格 → 行对象"的结构化与行级问题收集，不做判重/
语义等内容校验（那是 Task 7 治理流水线的职责）。`rows` 与 `errors`
不相交：带行级错误的行不会进入 rows——导入 API（Task 10）把 rows
原样交给治理流水线，若错误行混入会被直接入库。

解析规则：
- 首行为表头，按列名映射；必需列 tag_name/tag_type/description，缺失
  即 header_error（此时 rows/errors 均为空）；其余 5 列可选，缺可选
  列头不报错，多余列头忽略；
- 整行单元格均为空白/None 的空行跳过；
- 数字单元格转字符串（int/float）；bool/日期等无法可靠文本化的值报
  bad_type；
- aliases 单元格按中英文逗号/分号（`,，;；`）分隔、去首尾空白、去空段，
  单元格缺失时为空列表；
- 同一行多个问题时只报首个（缺必需字段优先于类型异常）；
- 行号从 1 计：表头为第 1 行，数据首行为第 2 行；
- status 缺省语义由入库层（治理流水线）决定，本层原样保留 None；
- 损坏/非 xlsx 输入由 openpyxl 抛异常，交由调用方（API 层）映射响应。
"""

import io
import re
from dataclasses import dataclass, field

import openpyxl

# 模板 8 列：3 必需 + 5 可选（缺可选列头不报错）
REQUIRED_COLUMNS = ("tag_name", "tag_type", "description")
OPTIONAL_COLUMNS = ("tag_id", "parent_tag", "aliases", "applicable_scope", "status")

# aliases 单元格分隔符：中英文逗号/分号
_ALIAS_SEPARATOR = re.compile(r"[,，;；]")


class _BadCell(Exception):
    """单元格值无法可靠转换为文本（bool/日期等非文本非数字类型）。"""


@dataclass
class TagRow:
    """Excel 模板中的一行标签（字段均已规范化为文本，缺失为 None）。"""

    tag_id: str | None
    tag_name: str
    tag_type: str
    description: str
    parent_tag: str | None
    aliases: list[str]
    applicable_scope: str | None
    status: str | None


@dataclass
class RowError:
    """单行解析失败记录；该行缺 tag_name 时 tag_name 为 None。"""

    row_number: int
    tag_name: str | None
    code: str  # missing_field / bad_type / bad_alias
    detail: str


@dataclass
class ExcelParseResult:
    """解析结果：rows 与 errors 不相交；header_error 非空时两者均为空。"""

    rows: list[TagRow] = field(default_factory=list)
    errors: list[RowError] = field(default_factory=list)
    header_error: str | None = None


def _cell_text(value: object) -> str | None:
    """单元格值规范化为文本：None/空白 → None，数字 → 字符串。

    返回 None 表示该单元格缺失/为空；无法可靠文本化的类型（bool、日期
    等）抛 _BadCell，由调用方归入 bad_type。注意 bool 是 int 子类，须
    先于数字判断。
    """
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, bool):
        raise _BadCell(value)
    if isinstance(value, (int, float)):
        return str(value)
    raise _BadCell(value)


def _split_aliases(text: str | None) -> list[str]:
    """按中英文逗号/分号分隔 aliases 单元格，去首尾空白与空段。"""
    if not text:
        return []
    return [part for part in (p.strip() for p in _ALIAS_SEPARATOR.split(text)) if part]


def _build_header_map(first_row: tuple) -> dict[str, int]:
    """表头行 → {列名: 列下标}；非字符串/空白表头格忽略，重名取首个。"""
    header_map: dict[str, int] = {}
    for idx, value in enumerate(first_row):
        if not isinstance(value, str):
            continue
        name = value.strip()
        if name and name not in header_map:
            header_map[name] = idx
    return header_map


def parse_tag_excel(data: bytes) -> ExcelParseResult:
    """解析 xlsx 字节流为标签行列表 + 行级错误（规则见模块 docstring）。"""
    workbook = openpyxl.load_workbook(io.BytesIO(data))
    try:
        worksheet = workbook.active
        header_row = next(worksheet.iter_rows(min_row=1, max_row=1, values_only=True), None)
        header_map = _build_header_map(header_row or ())

        missing = [name for name in REQUIRED_COLUMNS if name not in header_map]
        if missing:
            # 缺必需列头：表头级失败，rows/errors 均为空
            return ExcelParseResult(
                header_error=f"表头缺少必需列：{'、'.join(missing)}")

        result = ExcelParseResult()
        for row_number, cells in enumerate(
                worksheet.iter_rows(min_row=2, values_only=True), start=2):
            texts: dict[str, str | None] = {}
            bad_columns: list[str] = []
            for name, idx in header_map.items():
                try:
                    texts[name] = _cell_text(cells[idx])
                except _BadCell:
                    texts[name] = None
                    bad_columns.append(name)

            # 空行跳过：整行（含模板外列）均为 None/空白
            if all(
                value is None or (isinstance(value, str) and not value.strip())
                for value in cells
            ):
                continue

            tag_name = texts.get("tag_name")
            missing_required = [
                name for name in REQUIRED_COLUMNS if texts.get(name) is None
            ]
            if missing_required:
                # 缺必需字段优先于类型异常（与治理流水线"只报首个"一致）
                result.errors.append(RowError(
                    row_number=row_number,
                    tag_name=tag_name,
                    code="missing_field",
                    detail=f"缺少必需字段：{'、'.join(missing_required)}",
                ))
                continue
            if bad_columns:
                result.errors.append(RowError(
                    row_number=row_number,
                    tag_name=tag_name,
                    code="bad_type",
                    detail=f"列 {'、'.join(bad_columns)} 的值无法转换为文本",
                ))
                continue

            result.rows.append(TagRow(
                tag_id=texts.get("tag_id"),
                tag_name=tag_name,
                tag_type=texts["tag_type"],
                description=texts["description"],
                parent_tag=texts.get("parent_tag"),
                aliases=_split_aliases(texts.get("aliases")),
                applicable_scope=texts.get("applicable_scope"),
                status=texts.get("status"),
            ))
        return result
    finally:
        workbook.close()
