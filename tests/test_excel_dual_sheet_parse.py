"""双表模板（docs/movietags-template.xlsx）Excel 解析测试。

覆盖契约（与 convert 脚本、治理流水线的口径对齐）：
- 探测：任一数据 sheet（「标签目录」/「标签定义」）存在即走双表分支，
  与 active sheet 无关（首个 sheet 是填写说明页）；
- 列头行扫描定位（前面可有提示行）、列头格换行前段匹配
  （形如「目录ID\n(dir_id)」）；
- 目录行 → TagRow：tag_id/tag_name/dir_type/applicable_scope/
  description/aliases 流转，parent_tag 留空即根目录；tag_type 占位
  val_fixed（目录行不持久化此字段）；is_manual 固定 False；
- 目录层级列仅做行级检查：层级 ≥2 却缺上级目录名称 → missing_field，
  层级不可解析或无层级列内容时跳过检查（建树以父链为准）；
- 标签行 → TagRow：tag_id/tag_name/tag_type（缺省 val_fixed）/
  description/aliases 流转，缺所属目录名称 → missing_field（不静默落
  「未分类」）；
- 行号取各自 sheet 实际行号，错误 detail 带 sheet 名；
- 空行跳过；数字单元格转字符串；bool 单元格 bad_type；
- 缺任一数据 sheet 或列头不全 → header_error（rows/errors 均空）；
- 两数据 sheet 全缺时落回既有分支（英文扁平表头兼容）。

xlsx 字节流由 build_dual_xlsx 用 openpyxl 现场构造，不依赖外部文件。
"""

import io

import openpyxl

from tagplatform.governance.excel import (
    DUAL_DIR_COLUMNS,
    DUAL_DIR_SHEET,
    DUAL_TAG_COLUMNS,
    DUAL_TAG_SHEET,
    parse_tag_excel,
)

DIR_HEADER = [f"{name}\n(en)" for name in DUAL_DIR_COLUMNS]
TAG_HEADER = [f"{name}\n(en)" for name in DUAL_TAG_COLUMNS]
DIR_HINT = ["可选，留空由系统生成"] + [None] * (len(DUAL_DIR_COLUMNS) - 1)
TAG_HINT = ["可选，留空由系统生成"] + [None] * (len(DUAL_TAG_COLUMNS) - 1)

def build_dual_xlsx(
        dir_rows: list[list] | None = None,
        tag_rows: list[list] | None = None,
        include_dir_sheet: bool = True,
        include_tag_sheet: bool = True,
        dir_header: list | None = None,
        tag_header: list | None = None,
) -> bytes:
    """构造双表模板 xlsx：填写说明页首位 + 两数据 sheet（提示行 + 列头行）。"""
    workbook = openpyxl.Workbook()
    workbook.active.title = "填写说明"
    workbook.active.append(["模板说明：请按下方两 sheet 填写"])

    if include_dir_sheet:
        worksheet = workbook.create_sheet(DUAL_DIR_SHEET)
        worksheet.append(DIR_HINT)
        worksheet.append(dir_header if dir_header is not None else DIR_HEADER)
        for row in dir_rows or []:
            worksheet.append(row)
    if include_tag_sheet:
        worksheet = workbook.create_sheet(DUAL_TAG_SHEET)
        worksheet.append(TAG_HINT)
        worksheet.append(tag_header if tag_header is not None else TAG_HEADER)
        for row in tag_rows or []:
            worksheet.append(row)

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _row_dict(rows, name):
    by_name = {r.tag_name: r for r in rows}
    return by_name[name]


def test_dual_template_field_flow():
    r = parse_tag_excel(build_dual_xlsx(
        dir_rows=[
            [None, 1, None, "内容特征标签", "dir_static", "content", "目录描述A", "别名一|别名二"],
            [None, 2, "内容特征标签", "主类型", "dir_dynamic", None, None, None],
        ],
        tag_rows=[
            ["tag_001", "剧情", "主类型", "val_generalized", "以剧情为核心", "情节"],
            [None, "喜剧", "主类型", None, None, None],
        ],
    ))
    assert not r.errors and not r.header_error
    assert len(r.rows) == 4

    root = _row_dict(r.rows, "内容特征标签")
    assert root.parent_tag is None and root.tag_id is None
    assert root.dir_type == "dir_static"
    assert root.applicable_scope == "content"
    assert root.description == "目录描述A"
    assert root.aliases == ["别名一", "别名二"]
    assert root.tag_type == "val_fixed" and not root.is_manual

    sub = _row_dict(r.rows, "主类型")
    assert sub.parent_tag == "内容特征标签" and sub.dir_type == "dir_dynamic"
    assert sub.applicable_scope is None and sub.description == ""

    tag1 = _row_dict(r.rows, "剧情")
    assert tag1.tag_id == "tag_001" and tag1.tag_type == "val_generalized"
    assert tag1.parent_tag == "主类型" and tag1.description == "以剧情为核心"
    assert tag1.aliases == ["情节"] and tag1.dir_type is None

    tag2 = _row_dict(r.rows, "喜剧")
    # tag_type 缺省 val_fixed；applicable_scope/描述/别名为空
    assert tag2.tag_type == "val_fixed" and tag2.tag_id is None
    assert tag2.applicable_scope is None and tag2.description == ""
    assert tag2.aliases == []


def test_dual_template_row_numbers_are_sheet_rows():
    r = parse_tag_excel(build_dual_xlsx(
        dir_rows=[[None, 1, None, "根目录", None, None, None, None]],
        tag_rows=[
            [None, "标签一", "根目录", None, None, None],
            [None, None, "根目录", None, None, None],  # 第 4 行：缺标签名称
        ],
    ))
    assert len(r.errors) == 1
    err = r.errors[0]
    assert err.row_number == 4 and err.tag_name is None
    assert DUAL_TAG_SHEET in err.detail and err.code == "missing_field"

    # 目录行行号取「标签目录」sheet 实际行号（提示行1 + 列头行2 + 数据行3）
    assert [row.tag_name for row in r.rows] == ["根目录", "标签一"]


def test_dual_template_dir_level_without_parent_is_error():
    r = parse_tag_excel(build_dual_xlsx(
        dir_rows=[
            [None, 2, None, "孤儿目录", None, None, None, None],
            [None, "三级", "主类型", "层级乱填字符串也跳过检查", None, None, None, None],
            [None, None, "主类型", "无层级不检查", None, None, None, None],
        ],
    ))
    assert len(r.errors) == 1
    err = r.errors[0]
    assert err.code == "missing_field" and err.tag_name == "孤儿目录"
    assert err.row_number == 3 and DUAL_DIR_SHEET in err.detail
    assert len(r.rows) == 2  # 后两行合法：建树以父链为准


def test_dual_template_tag_without_dir_is_error():
    r = parse_tag_excel(build_dual_xlsx(
        dir_rows=[[None, 1, None, "根目录", None, None, None, None]],
        tag_rows=[
            [None, "有目录", "根目录", None, None, None],
            [None, "无目录", None, None, None, None],
            [None, None, None, None, None, None],  # 空行跳过
        ],
    ))
    assert len(r.rows) == 2 and len(r.errors) == 1
    err = r.errors[0]
    assert err.code == "missing_field" and err.tag_name == "无目录"
    # 标签 sheet：提示行1 + 列头行2 + 数据行3-5（空行行号不占错误）
    assert err.row_number == 4 and DUAL_TAG_SHEET in err.detail


def test_dual_template_bad_cell_and_numeric_cells():
    r = parse_tag_excel(build_dual_xlsx(
        dir_rows=[
            [None, 1, None, "根目录", None, None, None, None],
            [42, 2, "根目录", 3.14, None, None, None, None],  # 数字 ID/名称转字符串
        ],
        tag_rows=[
            [None, "坏行", "根目录", True, None, None],  # bool 单元格 bad_type
        ],
    ))
    # 目录 sheet 第 3 行「根目录」合法、第 4 行数字目录名转字符串合法；
    # 标签 sheet 第 3 行 bool 单元格 bad_type（缺必需字段优先于类型异常）
    assert len(r.rows) == 2 and len(r.errors) == 1
    assert r.rows[1].tag_id == "42" and r.rows[1].tag_name == "3.14"
    assert r.errors[0].code == "bad_type" and r.errors[0].tag_name == "坏行"
    assert "标签类型" in r.errors[0].detail and r.errors[0].row_number == 3


def test_dual_template_missing_sheet_is_header_error():
    r = parse_tag_excel(build_dual_xlsx(include_tag_sheet=False))
    assert r.header_error and DUAL_TAG_SHEET in r.header_error
    assert not r.rows and not r.errors

    r = parse_tag_excel(build_dual_xlsx(include_dir_sheet=False))
    assert r.header_error and DUAL_DIR_SHEET in r.header_error
    assert not r.rows and not r.errors


def test_dual_template_missing_column_is_header_error():
    bad_dir_header = DIR_HEADER[:-1]  # 缺「别名」列
    r = parse_tag_excel(build_dual_xlsx(dir_header=bad_dir_header))
    assert r.header_error and DUAL_DIR_SHEET in r.header_error
    assert "别名" in r.header_error and not r.rows

    bad_tag_header = TAG_HEADER[:4]  # 缺「标签描述」「别名」列
    r = parse_tag_excel(build_dual_xlsx(tag_header=bad_tag_header))
    assert r.header_error and DUAL_TAG_SHEET in r.header_error
    assert "标签描述" in r.header_error and not r.rows


def test_dual_template_falls_back_when_no_dual_sheets():
    # 两数据 sheet 全缺 → 落回既有分支：仅剩「填写说明」页时按
    # active sheet 找不到可识别表头 → header_error（不是崩溃）
    r = parse_tag_excel(build_dual_xlsx(
        include_dir_sheet=False, include_tag_sheet=False,
    ))
    assert r.header_error

    # 英文扁平表头在 active sheet 且无双表 sheet → 仍按原样解析（兼容）
    r = parse_tag_excel(build_xlsx_english([
        ["tag_name", "tag_type", "description"],
        ["悬疑", "val_fixed", "以悬念为核心"],
    ]))
    assert not r.header_error and len(r.rows) == 1
    assert r.rows[0].tag_name == "悬疑"


def build_xlsx_english(rows: list[list]) -> bytes:
    """构造仅 active sheet 的英文扁平表头 xlsx（无双表 sheet）。"""
    workbook = openpyxl.Workbook()
    for row in rows:
        workbook.active.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
