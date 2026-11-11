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
  manual，否则标记为非手动添加；TagRow.tag_type 固定为 val_fixed；
  拆出的目录行标 `is_dir=True`、「标签」列拆出的标签行标
  `is_dir=False`（层级行恒被本行标签引用，标记只为语义一致）；
- 双表模板（docs/movietags-template.xlsx）按 sheet 名探测（任一存在即走
  该分支——首个 sheet 是填写说明页，不能按 active sheet 判格式）：「标签
  目录」「标签定义」两 sheet，列头格形如「目录ID\n(dir_id)」取换行前段
  匹配，列头行扫描定位（前面可有提示行）。目录行 → 目录 TagRow
  （tag_id=目录ID、parent_tag=上级目录名称——根目录留空、dir_type=目录
  类型、applicable_scope=适用范围，缺省交入库层归一；目录层级列仅用于
  「二级及以上缺上级」的行级检查，建树以父链为准），并显式标
  `is_dir=True`——**空目录**（其下没有任何标签行的行）靠这个标记保住
  目录身份，不会被治理流水线当成标签行落库；标签行 →
  标签 TagRow（tag_id=标签ID、parent_tag=所属目录名称——必填、tag_type=标签
  类型，缺省 val_fixed，标 `is_dir=False`）。行号取各自 sheet 的实际行号，
  行级错误 detail 带 sheet 名；缺任一 sheet 或列头不全 → header_error；
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
    split_aliases,
)

NEW_EXCEL_COLUMNS = (
    "一级标签名称", "二级标签名称", "三级标签名称", "标签", "标签描述", "标签生成方式")
MANUAL_TAG_TYPE = "manual"

# 双表模板：sheet 名与列头（列头单元格形如 "目录ID\n(dir_id)"，取换行
# 前段匹配）；与 scripts/convert_tag_template.py 的常量保持一致
DUAL_DIR_SHEET = "标签目录"
DUAL_TAG_SHEET = "标签定义"
DUAL_DIR_COLUMNS = ("目录ID", "目录层级", "上级目录名称", "目录名称",
                    "目录类型", "适用范围", "目录描述", "别名")
DUAL_TAG_COLUMNS = ("标签ID", "标签名称", "所属目录名称", "标签类型",
                    "标签描述", "别名")

__all__ = [
    "ExcelParseResult",
    "ImportParseResult",
    "OPTIONAL_COLUMNS",
    "REQUIRED_COLUMNS",
    "RowError",
    "TagRow",
    "NEW_EXCEL_COLUMNS",
    "MANUAL_TAG_TYPE",
    "DUAL_DIR_SHEET",
    "DUAL_TAG_SHEET",
    "DUAL_DIR_COLUMNS",
    "DUAL_TAG_COLUMNS",
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


def _dual_header_index(cells: tuple, columns: tuple[str, ...]) -> dict[str, int]:
    """双表模板表头行 → {列名: 列下标}。

    列头单元格形如「目录ID\n(dir_id)」，取换行前段的中文名匹配；
    非字符串格忽略，重名取首个。
    """
    index: dict[str, int] = {}
    for idx, value in enumerate(cells):
        if not isinstance(value, str):
            continue
        key = value.split("\n")[0].strip()
        if key in columns and key not in index:
            index[key] = idx
    return index


def _find_dual_header(
        worksheet: openpyxl.worksheet.worksheet.Worksheet,
        columns: tuple[str, ...],
) -> tuple[int, dict[str, int]]:
    """扫描 sheet 定位完整列头行 → (列头行号, {列名: 下标})。

    列头行之前可有提示行（如「可选，留空由系统生成」）；未找到完整
    列头时行号为 0，映射返回匹配最多的部分结果（供缺列报错）。
    """
    best: dict[str, int] = {}
    for row_number, cells in enumerate(
            worksheet.iter_rows(values_only=True), start=1):
        header_map = _dual_header_index(cells, columns)
        if len(header_map) == len(columns):
            return row_number, header_map
        if len(header_map) > len(best):
            best = header_map
    return 0, best


def _dual_row_texts(
        cells: tuple,
        header_map: dict[str, int],
        columns: tuple[str, ...],
) -> tuple[dict[str, str | None], list[str]] | None:
    """双表模板数据行 → ({列名: 文本}, 无法文本化的列)；空行返回 None。"""
    if all(
        value is None or (isinstance(value, str) and not value.strip())
        for value in cells
    ):
        return None
    texts: dict[str, str | None] = {}
    bad_columns: list[str] = []
    for column in columns:
        try:
            texts[column] = cell_text(cells[header_map[column]])
        except _BadCell:
            texts[column] = None
            bad_columns.append(column)
    return texts, bad_columns


def _dir_level(raw: str | None) -> int | None:
    """目录层级文本 → 层级值；无法解析时返回 None（跳过层级行级检查）。"""
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        try:
            return int(float(raw))
        except ValueError:
            return None


def _parse_dual_dir_sheet(
        worksheet: openpyxl.worksheet.worksheet.Worksheet,
        header_map: dict[str, int],
        header_row_number: int,
        result: ExcelParseResult,
) -> None:
    """解析「标签目录」sheet → 目录 TagRow + 行级错误。"""
    for row_number, cells in enumerate(
            worksheet.iter_rows(
                min_row=header_row_number + 1, values_only=True),
            start=header_row_number + 1):
        parsed = _dual_row_texts(cells, header_map, DUAL_DIR_COLUMNS)
        if parsed is None:
            continue
        texts, bad_columns = parsed
        name = texts["目录名称"]
        if name is None:
            result.errors.append(RowError(
                row_number=row_number,
                tag_name=None,
                code="missing_field",
                detail=f"「{DUAL_DIR_SHEET}」缺少必需字段：目录名称",
            ))
            continue
        if bad_columns:
            result.errors.append(RowError(
                row_number=row_number,
                tag_name=name,
                code="bad_type",
                detail=(f"「{DUAL_DIR_SHEET}」列 "
                        f"{'、'.join(bad_columns)} 的值无法转换为文本"),
            ))
            continue
        parent = texts["上级目录名称"]
        level = _dir_level(texts["目录层级"])
        if parent is None and level is not None and level >= 2:
            # 层级表明非根目录却缺上级名称：报行级错误，防静默变根目录
            result.errors.append(RowError(
                row_number=row_number,
                tag_name=name,
                code="missing_field",
                detail=(f"「{DUAL_DIR_SHEET}」目录「{name}」为 {level} 级"
                        "却缺少上级目录名称"),
            ))
            continue
        result.rows.append(TagRow(
            tag_id=texts["目录ID"],
            tag_name=name,
            tag_type="val_fixed",  # 目录行不持久化此字段，占位即可
            description=texts["目录描述"] or "",
            parent_tag=parent,
            aliases=split_aliases(texts["别名"]),
            applicable_scope=texts["适用范围"],
            status=None,
            is_manual=False,
            dir_type=texts["目录类型"],
            # 「标签目录」sheet 的每一行都是目录：显式标记，空目录（其下
            # 无任何标签行）才不会被治理流水线当成标签行落库
            is_dir=True,
        ))


def _parse_dual_tag_sheet(
        worksheet: openpyxl.worksheet.worksheet.Worksheet,
        header_map: dict[str, int],
        header_row_number: int,
        result: ExcelParseResult,
) -> None:
    """解析「标签定义」sheet → 标签 TagRow + 行级错误。"""
    for row_number, cells in enumerate(
            worksheet.iter_rows(
                min_row=header_row_number + 1, values_only=True),
            start=header_row_number + 1):
        parsed = _dual_row_texts(cells, header_map, DUAL_TAG_COLUMNS)
        if parsed is None:
            continue
        texts, bad_columns = parsed
        name = texts["标签名称"]
        if name is None:
            result.errors.append(RowError(
                row_number=row_number,
                tag_name=None,
                code="missing_field",
                detail=f"「{DUAL_TAG_SHEET}」缺少必需字段：标签名称",
            ))
            continue
        if bad_columns:
            result.errors.append(RowError(
                row_number=row_number,
                tag_name=name,
                code="bad_type",
                detail=(f"「{DUAL_TAG_SHEET}」列 "
                        f"{'、'.join(bad_columns)} 的值无法转换为文本"),
            ))
            continue
        parent = texts["所属目录名称"]
        if parent is None:
            result.errors.append(RowError(
                row_number=row_number,
                tag_name=name,
                code="missing_field",
                detail=f"「{DUAL_TAG_SHEET}」标签「{name}」缺少所属目录名称",
            ))
            continue
        result.rows.append(TagRow(
            tag_id=texts["标签ID"],
            tag_name=name,
            tag_type=texts["标签类型"] or "val_fixed",
            description=texts["标签描述"] or "",
            parent_tag=parent,
            aliases=split_aliases(texts["别名"]),
            applicable_scope=None,
            status=None,
            is_manual=False,
            # 「标签定义」sheet 的每一行都是标签：显式表态，同名目录不再
            # 把本行拖进目录命名空间
            is_dir=False,
        ))


def _parse_dual_sheet_excel(workbook) -> ExcelParseResult:
    """解析双表模板（「标签目录」+「标签定义」两 sheet）。"""
    specs: list[tuple[str, int, dict[str, int]]] = []
    for sheet_name, columns in ((DUAL_DIR_SHEET, DUAL_DIR_COLUMNS),
                                (DUAL_TAG_SHEET, DUAL_TAG_COLUMNS)):
        if sheet_name not in workbook.sheetnames:
            # 表头级失败：rows/errors 均为空
            return ExcelParseResult(header_error=f"缺少 sheet「{sheet_name}」")
        header_row_number, header_map = _find_dual_header(
            workbook[sheet_name], columns)
        if not header_row_number:
            missing = [c for c in columns if c not in header_map]
            return ExcelParseResult(
                header_error=(f"sheet「{sheet_name}」表头缺少必需列："
                              f"{'、'.join(missing)}"))
        specs.append((sheet_name, header_row_number, header_map))

    result = ExcelParseResult()
    _dir_sheet, _dir_header_row, _dir_header_map = specs[0]
    _parse_dual_dir_sheet(
        workbook[_dir_sheet], _dir_header_map, _dir_header_row, result)
    _tag_sheet, _tag_header_row, _tag_header_map = specs[1]
    _parse_dual_tag_sheet(
        workbook[_tag_sheet], _tag_header_map, _tag_header_row, result)
    print_parse_result(result)
    return result


def _parse_hierarchy_excel(
        worksheet: openpyxl.worksheet.worksheet.Worksheet,
        header_map: dict[str, int],
) -> ExcelParseResult:
    """解析六列层级标签模板，并转换为通用 TagRow。"""
    result = ExcelParseResult()
    required = ("一级标签名称", "二级标签名称", "标签")
    hierarchy_names: set[tuple[str | None, str]] = set()
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
            hierarchy_key = (parent_name, hierarchy_name)
            if hierarchy_name is None or hierarchy_key in hierarchy_names:
                continue
            hierarchy_names.add(hierarchy_key)
            result.rows.append(TagRow(
                tag_id=None,
                tag_name=hierarchy_name,
                tag_type="val_fixed",
                description="",
                parent_tag=parent_name,
                aliases=[],
                applicable_scope=None,
                status=None,
                is_manual=True,
                # 层级列名即目录节点：显式标记（末级空目录不会退化为标签）
                is_dir=True,
            ))

        result.rows.append(TagRow(
            tag_id=None,
            tag_name=tag_name,
            tag_type="val_fixed",
            description=texts["标签描述"] or "",
            parent_tag=texts["三级标签名称"] or texts["二级标签名称"],
            aliases=[],
            applicable_scope=None,
            status=None,
            is_manual=texts["标签生成方式"] is None,
            # 「标签」列即标签行：显式表态（与层级列的目录标记互不干扰）
            is_dir=False,
        ))
    return result


def parse_tag_excel(data: bytes) -> ExcelParseResult:
    """解析 xlsx 字节流为标签行列表 + 行级错误（规则见模块 docstring）。"""
    workbook = openpyxl.load_workbook(io.BytesIO(data))
    try:
        # 双表模板按 sheet 名探测：首个 sheet 是填写说明页，不能用
        # active sheet 判格式；任一数据 sheet 存在即走双表分支
        if (DUAL_DIR_SHEET in workbook.sheetnames
                or DUAL_TAG_SHEET in workbook.sheetnames):
            return _parse_dual_sheet_excel(workbook)

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
