"""XML 标签导入解析测试。"""

from tagplatform.governance import parse_tag_xml


def test_xml_matches_excel_result_shape():
    result = parse_tag_xml("""<?xml version="1.0"?>
        <tags>
          <tag>
            <tag_id>42</tag_id>
            <tag_name>悬疑</tag_name>
            <tag_type>genre</tag_type>
            <description>以悬念为核心</description>
            <parent_tag>剧情</parent_tag>
            <aliases><alias>悬念</alias><alias>推理</alias></aliases>
            <applicable_scope>电影</applicable_scope>
            <status>active</status>
          </tag>
        </tags>""".encode())

    assert result.header_error is None and not result.errors
    assert result.rows[0].tag_id == "42"
    assert result.rows[0].aliases == ["悬念", "推理"]


def test_xml_supports_attributes_and_reports_row_errors():
    result = parse_tag_xml("""<tags>
      <tag tag_name="悬疑" tag_type="val_fixed" description="描述"
           aliases="悬念, 推理"/>
      <tag tag_name="缺描述" tag_type="val_fixed"/>
    </tags>""".encode())

    assert len(result.rows) == 1
    assert result.rows[0].aliases == ["悬念", "推理"]
    assert len(result.errors) == 1
    assert result.errors[0].row_number == 3
    assert result.errors[0].code == "missing_field"
    assert result.errors[0].tag_name == "缺描述"
