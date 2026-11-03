"""Excel 标签模板解析测试（Task 6）。

覆盖契约：
- 合法模板（含 aliases 中英文逗号分隔、可选列）解析出 TagRow；
- 畸形行逐行报告 missing_field 且不中断：带错误的行不进入 rows（rows 与
  errors 不相交，导入 API 将 rows 直接交给治理流水线）、空行跳过、
  数字单元格转字符串；
- 缺必需表头列 → header_error 非空且 rows 为空；
- （补充）全 8 列模板：tag_id/applicable_scope 可选列、按列名映射、
  数字 tag_id 转字符串、aliases 中英文分号/逗号混用去空段。

xlsx 字节流由 build_xlsx 用 openpyxl 现场构造，不依赖外部文件。
"""

import io

import openpyxl

from tagplatform.governance import parse_tag_excel

# 模板常用 6 列（tag_id/applicable_scope 两列可选，见 test_full_eight_column_template）；
# 外层再包一行，使 build_xlsx(HEADERS + [...]) 拼出的都是"行"的列表
HEADERS = [["tag_name", "tag_type", "description", "parent_tag", "aliases", "status"]]


def build_xlsx(rows: list[list]) -> bytes:
    """用 openpyxl 现场构造 xlsx 字节流（首行为表头）。"""
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    for row in rows:
        worksheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def test_valid_template():
    r = parse_tag_excel(build_xlsx([
        ["tag_name", "tag_type", "description", "parent_tag", "aliases", "status"],
        ["悬疑", "genre", "以悬念为核心", "剧情", "悬念,推理", "active"],
    ]))
    assert r.rows[0].aliases == ["悬念", "推理"] and r.rows[0].parent_tag == "剧情"


def test_excel_malformed_rows_reported():
    r = parse_tag_excel(build_xlsx(HEADERS + [
        ["悬疑", "genre", "以悬念为核心"],              # 合法
        [None, "genre", "缺名称"],                      # 行级 missing_field(tag_name)
        ["数量", "genre"],                              # 描述为空时仍合法
        ["风格", "genre", "d", "aliases列乱填数字", 123],  # bad_type→aliases 数字转字符串"123"
        [None, None, None],                             # 空行跳过
    ]))
    assert len(r.rows) == 3 and len(r.errors) == 1
    assert {e.code for e in r.errors} == {"missing_field"}
    assert r.errors[0].row_number == 3 and r.errors[0].tag_name is None
    assert r.rows[1].description == ""
    assert r.rows[2].aliases == ["123"]  # 数字单元格转字符串


def test_header_missing_required():
    r = parse_tag_excel(build_xlsx([["tag_name", "tag_type"]]))
    assert r.header_error and not r.rows


def test_full_eight_column_template():
    r = parse_tag_excel(build_xlsx([
        ["tag_id", "tag_name", "tag_type", "description", "parent_tag",
         "aliases", "applicable_scope", "status"],
        [42, "悬疑", "genre", "以悬念为核心", None, "悬念；推理，", "电影", "active"],
        [None, "悬疑", "genre", "以悬念为核心"],  # 缺可选列单元格与可选列头 → 不报错
    ]))
    assert not r.errors and len(r.rows) == 2
    first = r.rows[0]
    assert first.tag_id == "42" and first.aliases == ["悬念", "推理"]
    assert first.parent_tag is None and first.applicable_scope == "电影"
    assert first.status == "active"
    assert r.rows[1].aliases == [] and r.rows[1].parent_tag is None


def test_hierarchy_template():
    r = parse_tag_excel(build_xlsx([
        ["一级标签名称", "二级标签名称", "三级标签名称", "标签", "标签描述", "标签生成方式"],
        ["内容", "剧情", "悬疑", "反转", "结局出人意料", "genre"],
        ["内容", "剧情", None, "喜剧", "带有喜剧元素", None],
    ]))

    assert not r.errors and len(r.rows) == 5
    assert [row.tag_name for row in r.rows] == ["内容", "剧情", "悬疑", "反转", "喜剧"]
    assert r.rows[3].tag_type == "genre"
    assert not r.rows[3].is_manual
    assert r.rows[3].parent_tag == "悬疑"
    assert r.rows[4].tag_type == "genre"
    assert r.rows[4].is_manual
    assert r.rows[4].parent_tag == "剧情"


def test_hierarchy_template_required_columns():
    r = parse_tag_excel(build_xlsx([
        ["一级标签名称", "二级标签名称", "三级标签名称", "标签", "标签描述", "标签生成方式"],
        ["内容", None, None, "标签", "描述"],
    ]))

    assert len(r.errors) == 1
    assert r.errors[0].code == "missing_field"
    assert "二级标签名称" in r.errors[0].detail


def test_hierarchy_template_allows_empty_description():
    r = parse_tag_excel(build_xlsx([
        ["一级标签名称", "二级标签名称", "三级标签名称", "标签", "标签描述", "标签生成方式"],
        ["内容", "剧情", None, "喜剧", None, None],
    ]))

    assert not r.errors
    assert r.rows[-1].tag_name == "喜剧"
    assert r.rows[-1].description == ""
