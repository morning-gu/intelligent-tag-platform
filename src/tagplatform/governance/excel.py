"""Excel 标签模板解析：xlsx 字节流 → 通用导入结果。

纯解析层：只做"单元格 → 行对象"的结构化与行级问题收集，不做判重/
语义等内容校验（那是 Task 7 治理流水线的职责）。`rows` 与 `errors`
不相交：带行级错误的行不会进入 rows——导入 API（Task 10）把 rows
原样交给治理流水线，若错误行混入会被直接入库。

解析规则：
- 新版模板首行为六列表头：一级标签名称、二级标签名称、三级标签名称、标签、标签描述、
  标签生成方式。
  一级标签名称、二级标签名称、标签、标签描述必填；三级标签名称可空，
  父标签优先取三级标签名称，否则取二级标签名称；tag_type 为空时固定为
  manual，否则标记为非手动添加；TagRow.tag_type 固定为 genre；
- 旧版英文表头模板仍兼容：必需列 tag_name/tag_type/description，缺失即
  header_error；其余列可选，缺可选列头不报错，多余列头忽略；
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
from pprint import pprint

import openpyxl

from tagplatform.governance.models import (
    BadCell as _BadCell,
    ExcelParseResult,
    ImportParseResult,
    OPTIONAL_COLUMNS,
    REQUIRED_COLUMNS,
    RowError,
    TagRow,
    build_tag_row,
    cell_text,
)

NEW_EXCEL_COLUMNS = (
    "一级标签名称", "二级标签名称", "三级标签名称", "标签", "标签描述", "标签生成方式")
MANUAL_TAG_TYPE = "manual"

__all__ = [
    "ExcelParseResult",
    "ImportParseResult",
    "OPTIONAL_COLUMNS",
    "REQUIRED_COLUMNS",
    "RowError",
    "TagRow",
    "NEW_EXCEL_COLUMNS",
    "MANUAL_TAG_TYPE",
    "parse_tag_excel",
    "print_parse_result",
]


def print_parse_result(result: ExcelParseResult) -> None:
    """以易读的结构打印 Excel 解析结果。"""
    pprint({
        "rows": [row.__dict__ for row in result.rows],
        "errors": [error.__dict__ for error in result.errors],
        "header_error": result.header_error,
    }, sort_dicts=False)


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


def _parse_hierarchy_excel(
        worksheet: openpyxl.worksheet.worksheet.Worksheet,
        header_map: dict[str, int],
) -> ExcelParseResult:
    """解析六列层级标签模板，并转换为通用 TagRow。"""
    result = ExcelParseResult()
    required = ("一级标签名称", "二级标签名称", "标签")
    hierarchy_names: set[str] = set()
    for row_number, cells in enumerate(
            worksheet.iter_rows(min_row=2, values_only=True), start=2):
        if all(
            value is None or (isinstance(value, str) and not value.strip())
            for value in cells
        ):
            continue

        texts: dict[str, str | None] = {}
        bad_columns: list[str] = []
        for name in NEW_EXCEL_COLUMNS:
            try:
                texts[name] = cell_text(cells[header_map[name]])
            except _BadCell:
                texts[name] = None
                bad_columns.append(name)

        missing = [name for name in required if texts[name] is None]
        tag_name = texts["标签"]
        if missing:
            result.errors.append(RowError(
                row_number=row_number,
                tag_name=tag_name,
                code="missing_field",
                detail=f"缺少必需字段：{'、'.join(missing)}",
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

        hierarchy = (
            (texts["一级标签名称"], None),
            (texts["二级标签名称"], texts["一级标签名称"]),
            (texts["三级标签名称"], texts["二级标签名称"]),
        )
        for hierarchy_name, parent_name in hierarchy:
            if hierarchy_name is None or hierarchy_name in hierarchy_names:
                continue
            hierarchy_names.add(hierarchy_name)
            result.rows.append(TagRow(
                tag_id=None,
                tag_name=hierarchy_name,
                tag_type="genre",
                description="",
                parent_tag=parent_name,
                aliases=[],
                applicable_scope=None,
                status=None,
                is_manual=True,
            ))

        result.rows.append(TagRow(
            tag_id=None,
            tag_name=tag_name,
            tag_type="genre",
            description=texts["标签描述"] or "",
            parent_tag=texts["三级标签名称"] or texts["二级标签名称"],
            aliases=[],
            applicable_scope=None,
            status=None,
            is_manual=texts["标签生成方式"] is None,
        ))
    return result


def parse_tag_excel(data: bytes) -> ExcelParseResult:
    """解析 xlsx 字节流为标签行列表 + 行级错误（规则见模块 docstring）。"""
    workbook = openpyxl.load_workbook(io.BytesIO(data))
    try:
        worksheet = workbook.active
        header_row = next(worksheet.iter_rows(min_row=1, max_row=1, values_only=True), None)
        header_map = _build_header_map(header_row or ())

        new_columns = [name for name in NEW_EXCEL_COLUMNS if name in header_map]
        if new_columns:
            missing = [name for name in NEW_EXCEL_COLUMNS if name not in header_map]
            if missing:
                return ExcelParseResult(
                    header_error=f"表头缺少必需列：{'、'.join(missing)}")
            result = _parse_hierarchy_excel(worksheet, header_map)
            print_parse_result(result)
            return result

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
                    texts[name] = cell_text(cells[idx])
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
                name for name in ("tag_name", "tag_type") if texts.get(name) is None
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

            result.rows.append(build_tag_row(texts))
        print_parse_result(result)
        return result
    finally:
        workbook.close()
