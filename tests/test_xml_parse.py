"""XML 标签导入解析测试（扁平模板与树状层级模板）。"""

from tagplatform.services.tags.governance import parse_tag_xml


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


def test_parse_nested_xml_imports_hierarchy_nodes():
    result = parse_tag_xml("""<?xml version="1.0" encoding="UTF-8"?>
      <root>
        <level1 name="内容">
          <level2 name="剧情">
            <tag description="带有喜剧元素" type="manual">喜剧</tag>
          </level2>
        </level1>
      </root>""".encode())

    assert not result.errors
    assert [row.tag_name for row in result.rows] == ["内容", "剧情", "喜剧"]
    assert [row.parent_tag for row in result.rows] == [None, "内容", "剧情"]
    assert all(row.tag_type == "val_fixed" for row in result.rows)
    assert result.rows[2].is_manual


def test_parse_xml_marks_level_nodes_as_directories():
    """level1/2/3 节点 → is_dir=True，<tag> → is_dir=False。"""
    result = parse_tag_xml(
        "<root><level1 name='内容'><level2 name='剧情'/></level1></root>".encode()
    )

    assert not result.errors
    assert [row.tag_name for row in result.rows] == ["内容", "剧情"]
    assert [row.is_dir for row in result.rows] == [True, True]


def test_parse_xml_carries_dir_type_attribute():
    """目录节点的 dir_type 属性透传（含 dir_asset），归一交入库层。"""
    result = parse_tag_xml("""<?xml version="1.0" encoding="UTF-8"?>
      <root>
        <level1 name="元数据目录" dir_type="dir_asset">
          <level2 name="搜索词" dir_type="dir_static">
            <tag>关键词标签</tag>
          </level2>
        </level1>
      </root>""".encode())

    assert not result.errors
    assert [row.dir_type for row in result.rows] == ["dir_asset", "dir_static", None]
    assert result.rows[2].is_dir is False  # 标签行无目录类型
