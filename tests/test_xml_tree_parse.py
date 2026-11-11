"""树状 XML 标签导入测试。"""

from tagplatform.governance import parse_tag_xml


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
    """level1/2/3 节点 → is_dir=True，<tag> → is_dir=False。

    末级空目录（其下无 <tag>）只有靠该标记才不会被治理流水线当成标签。
    """
    result = parse_tag_xml("<root><level1 name='内容'><level2 name='剧情'/></level1></root>".encode())

    assert not result.errors
    assert [row.tag_name for row in result.rows] == ["内容", "剧情"]
    assert [row.is_dir for row in result.rows] == [True, True]
