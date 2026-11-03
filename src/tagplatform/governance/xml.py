import xml.etree.ElementTree as ET

from tagplatform.governance.models import (
    BadCell,
    ImportParseResult,
    REQUIRED_COLUMNS,
    RowError,
    TagRow,
    build_tag_row,
    cell_text,
)

_FIELDS = ("tag_id", "tag_name", "tag_type", "description", "parent_tag",
           "aliases", "applicable_scope", "status")

__all__ = ["parse_tag_xml"]


def _xml_tag_row(element: ET.Element, parent_name: str | None) -> TagRow:
    """将树节点转换为标签行；层级节点使用 name 属性，tag 使用文本。"""
    node_name = _local_name(element.tag)
    if node_name == "tag":
        name = cell_text(element.text)
    else:
        name = cell_text(element.attrib.get("name"))
    if name is None:
        raise BadCell(name)
    generation_type = cell_text(
        element.attrib.get("type") or element.attrib.get("标签生成方式"))
    description = cell_text(
        element.attrib.get("description") or element.attrib.get("标签描述")) or ""
    return TagRow(
        tag_id=cell_text(element.attrib.get("id")),
        tag_name=name,
        tag_type="genre",
        description=description,
        parent_tag=parent_name,
        aliases=[],
        applicable_scope=None,
        status=None,
        is_manual=generation_type is None or generation_type == "manual",
    )


def _parse_tree_records(root: ET.Element) -> ImportParseResult:
    """解析 level1/level2/level3/tag 树，并保留每个节点的父级。"""
    result = ImportParseResult()
    node_names = {"level1", "level2", "level3", "tag"}

    def visit(element: ET.Element, parent_name: str | None, row_number: int) -> int:
        name = _local_name(element.tag)
        if name not in node_names:
            for child in element:
                row_number = visit(child, parent_name, row_number)
            return row_number
        try:
            row = _xml_tag_row(element, parent_name)
        except BadCell:
            result.errors.append(RowError(
                row_number=row_number,
                tag_name=cell_text(element.attrib.get("name")),
                code="missing_field",
                detail="缺少标签名称",
            ))
            return row_number + 1
        result.rows.append(row)
        row_number += 1
        for child in element:
            row_number = visit(child, row.tag_name, row_number)
        return row_number

    row_number = 2
    for child in root:
        row_number = visit(child, None, row_number)
    return result


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _element_value(element: ET.Element, field: str) -> str | None:
    """读取字段属性或子元素；aliases 支持多个 alias 子元素。"""
    if field in element.attrib:
        return cell_text(element.attrib[field])
    children = [child for child in element if _local_name(child.tag) == field]
    if not children:
        return None
    if field == "aliases" and len(children) == 1:
        alias_children = [
            child for child in children[0]
            if _local_name(child.tag) in {"alias", "item", "value"}
        ]
        if alias_children:
            return ", ".join(child.text or "" for child in alias_children)
    return cell_text(" ".join(child.itertext()) if len(children) > 1 else
                     (children[0].text or ""))


def _record_elements(root: ET.Element) -> list[ET.Element]:
    records = [
        child for child in root
        if _local_name(child.tag) in {"tag", "item", "row"}
    ]
    if records:
        return records
    return [root] if _local_name(root.tag) in {"tag", "item", "row"} else []


def parse_tag_xml(data: bytes) -> ImportParseResult:
    """解析标签 XML，返回与 ``parse_tag_excel`` 相同的结果结构。

    支持树状 ``level1/level2/level3/tag`` 节点；层级节点使用 ``name``
    属性，标签节点使用文本内容。也兼容旧的扁平 ``<tags><tag>...</tag>``
    结构。
    """
    root = ET.fromstring(data)
    if any(_local_name(child.tag) in {"level1", "level2", "level3"}
           for child in root):
        return _parse_tree_records(root)
    records = _record_elements(root)
    if not records:
        return ImportParseResult(header_error="XML 缺少标签记录（tag/item/row）")

    result = ImportParseResult()
    for row_number, element in enumerate(records, start=2):
        texts: dict[str, str | None] = {}
        for field in _FIELDS:
            try:
                texts[field] = _element_value(element, field)
            except BadCell:
                result.errors.append(RowError(
                    row_number=row_number,
                    tag_name=texts.get("tag_name"),
                    code="bad_type",
                    detail=f"列 {field} 的值无法转换为文本",
                ))
                break
        else:
            if all(value is None for value in texts.values()):
                continue
            missing = [field for field in REQUIRED_COLUMNS if texts.get(field) is None]
            if missing:
                result.errors.append(RowError(
                    row_number=row_number,
                    tag_name=texts.get("tag_name"),
                    code="missing_field",
                    detail=f"缺少必需字段：{'、'.join(missing)}",
                ))
                continue
            result.rows.append(build_tag_row(texts))
    return result
