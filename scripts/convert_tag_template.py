"""CLI：双表标签模板（标签目录 + 标签定义 sheet）→ 树状导入 XML。

docs/movietags-template.xlsx 是运营填写模板：每行一条目录（标签目录
sheet，目录层级 + 上级目录名称 + 目录名称 建树）或一条标签（标签定义
sheet，所属目录名称 挂叶子）。本脚本把它转换为治理流水线可直接导入的
树状 XML（level1/level2/level3 + tag，与 examples/tag-import-validation/
tags.xml 同构），配合 POST /api/v1/tags/import 使用。

转换映射（可选列未填时输出默认值属性，有值时属性保真）：
- 目录行 → <level1|2|3 name="...">，按目录层级嵌套；
  dir_id 有值时输出 id 属性（解析器读作行 tag_id → 目录 dir_id）；
- 标签行 → 所属目录节点下的 <tag>名称</tag>；tag_id 有值时输出 id；
- 目录属性：dir_type（未填输出 dir_dynamic）、scope（未填输出
  content,scene,character；有值时按 content→scene→character 规范序
  重组，如 scene|content → content,scene）、description/aliases（有值
  才输出，导入层缺省空串/空别名）；
- 标签属性：tag_type（未填输出 val_fixed）、description/aliases
  （有值才输出）。全部属性均被导入解析器读取并入库。

校验（发现问题即退出非零、不产出 XML；与导入链路的拦截点对应）：
- 目录层级 ∈ {1,2,3}；L1 不得有上级，Lk 的上级须为存在的 L(k-1) 目录；
- 目录类型 ∈ {dir_static, dir_dynamic}；
- 适用范围按 |/，/；拆分后每项 ∈ {content, scene, character}；
- 标签类型 ∈ {val_fixed, val_generalized}；
- 目录名称全局唯一（树构建需要；导入层 (parent, name) 唯一键只拦同级）；
- 标签所属目录必须存在（导入层 parent_not_found）；
- 同目录标签名不重复（导入层 (dir, name) duplicate_name）；
- 标签名不得与目录名相同（治理按"名称被 parent 引用"判目录行，同名
  会让行分类歧义）；
- 跨目录同名标签合法（v2 唯一键是 (dir_id, tag_name)），仅 stderr 提示。

生成后自检：用 parse_tag_xml 解析产物，行数须等于目录数 + 标签数且
零错误，否则退出非零（保证输出永远可导入）。

用法：
    python scripts/convert_movietags_template.py \
        --xlsx docs/movietags-template.xlsx --out docs/movietags-template.xml
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tagplatform.governance.xml import parse_tag_xml  # noqa: E402

# sheet 名与列头（列头单元格形如 "目录ID\n(dir_id)"，取换行前段匹配）
DIR_SHEET = "标签目录"
TAG_SHEET = "标签定义"
DIR_COLUMNS = ("目录ID", "目录层级", "上级目录名称", "目录名称",
               "目录类型", "适用范围", "目录描述", "别名")
TAG_COLUMNS = ("标签ID", "标签名称", "所属目录名称", "标签类型",
               "标签描述", "别名")

# 枚举与默认值（与治理 pipeline 归一口径一致）
DIR_TYPES = ("dir_static", "dir_dynamic")
DEFAULT_DIR_TYPE = "dir_dynamic"
SCOPES = ("content", "scene", "character")
DEFAULT_SCOPE = "content,scene,character"
TAG_TYPES = ("val_fixed", "val_generalized")
DEFAULT_TAG_TYPE = "val_fixed"
_SCOPE_SPLIT = re.compile(r"[,，;；|]")


def _cell_text(value: object) -> str | None:
    """单元格 → 规范文本；空值/空白 → None，数字转字符串。"""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, bool):
        raise ValueError(f"布尔单元格无法文本化: {value}")
    if isinstance(value, (int, float)):
        return str(value)
    raise ValueError(f"无法文本化的单元格类型: {type(value).__name__}")


def _header_index(cells: tuple, columns: tuple[str, ...]) -> dict[str, int]:
    """表头行 → {列名: 下标}；取每个表头单元格换行前的中文名匹配。"""
    index: dict[str, int] = {}
    for idx, value in enumerate(cells):
        if not isinstance(value, str):
            continue
        key = value.split("\n")[0].strip()
        if key in columns and key not in index:
            index[key] = idx
    return index


def _sheet_rows(workbook, name: str, columns: tuple[str, ...],
                source: Path) -> tuple[list[dict], dict[str, int]]:
    """读双表模板的一个 sheet：定位列头行 → 数据行列表（字段按列名）。"""
    if name not in workbook.sheetnames:
        raise ValueError(f"{source.name} 缺少 sheet「{name}」")
    raw_rows = list(workbook[name].iter_rows(values_only=True))
    header_map: dict[str, int] = {}
    header_row_number = 0
    for row_number, cells in enumerate(raw_rows, start=1):
        header_map = _header_index(cells, columns)
        if len(header_map) == len(columns):
            header_row_number = row_number
            break
    if not header_row_number:
        missing = [c for c in columns if c not in header_map]
        raise ValueError(
            f"sheet「{name}」未找到完整列头，缺列：{'、'.join(missing)}")

    rows: list[dict] = []
    for row_number, cells in enumerate(
            raw_rows[header_row_number:], start=header_row_number + 1):
        if all(v is None or (isinstance(v, str) and not v.strip())
               for v in cells):
            continue  # 空行跳过
        record = {"_row": row_number}
        for column, idx in header_map.items():
            try:
                record[column] = _cell_text(cells[idx])
            except ValueError as exc:
                raise ValueError(
                    f"sheet「{name}」第 {row_number} 行 {column} 列：{exc}"
                ) from exc
        rows.append(record)
    return rows, header_map


def _read_template(source: Path) -> tuple[list[dict], list[dict]]:
    """读模板两 sheet → (目录行列表, 标签行列表)。"""
    import openpyxl

    workbook = openpyxl.load_workbook(source, read_only=True)
    try:
        dir_rows, _ = _sheet_rows(workbook, DIR_SHEET, DIR_COLUMNS, source)
        tag_rows, _ = _sheet_rows(workbook, TAG_SHEET, TAG_COLUMNS, source)
    finally:
        workbook.close()
    return dir_rows, tag_rows


def _dir_level(row: dict) -> int:
    """目录层级单元格 → 1/2/3；非法值即报错。"""
    raw = row.get("目录层级")
    try:
        level = int(raw)
    except (TypeError, ValueError):
        raise ValueError(
            f"标签目录 sheet 第 {row['_row']} 行：目录层级「{raw}」不是 1/2/3"
        ) from None
    if level not in (1, 2, 3):
        raise ValueError(
            f"标签目录 sheet 第 {row['_row']} 行：目录层级「{level}」不是 1/2/3")
    return level


def _normalize_scope(raw: str) -> str:
    """适用范围单元格 → 规范序组合串；非法值抛 ValueError（validate 拦截）。"""
    tokens = [t.strip() for t in _SCOPE_SPLIT.split(raw)]
    unknown = [t for t in tokens if t not in SCOPES]
    if unknown:
        raise ValueError(
            f"适用范围「{raw}」含非法值 {'、'.join(unknown)}"
            f"（须为 {'/'.join(SCOPES)} 的组合，以 | 或 , 分隔）")
    ordered = [s for s in SCOPES if s in tokens]
    return ",".join(ordered)


def validate(dir_rows: list[dict], tag_rows: list[dict],
             ) -> tuple[list[str], list[str]]:
    """模板级校验 → (errors, warnings)。errors 非空时不产出 XML。"""
    errors: list[str] = []
    warnings: list[str] = []

    by_name: dict[str, list[dict]] = {}
    for row in dir_rows:
        name = row.get("目录名称")
        if name is None:
            errors.append(f"标签目录 sheet 第 {row['_row']} 行：目录名称为空")
            continue
        by_name.setdefault(name, []).append(row)
    for name, rows in by_name.items():
        if len(rows) > 1:
            errors.append(f"目录名称「{name}」出现 {len(rows)} 次（须全局唯一）")

    for row in dir_rows:
        name = row.get("目录名称")
        if name is None:
            continue
        level = _dir_level(row)  # 非法层级直接抛错，走调用方统一报告
        parent = row.get("上级目录名称")
        if level == 1 and parent is not None:
            errors.append(f"目录「{name}」是一级目录却有上级「{parent}」")
        if level > 1:
            if parent is None:
                errors.append(f"目录「{name}」是 {level} 级目录却缺上级目录名称")
            else:
                candidates = by_name.get(parent, [])
                if not any(_dir_level(c) == level - 1 for c in candidates):
                    errors.append(
                        f"目录「{name}」({level} 级) 的上级「{parent}」"
                        f"不是存在的 {level - 1} 级目录")
        dir_type = row.get("目录类型")
        if dir_type is not None and dir_type not in DIR_TYPES:
            errors.append(
                f"目录「{name}」的目录类型「{dir_type}」非法"
                f"（须为 {' 或 '.join(DIR_TYPES)}）")
        scope = row.get("适用范围")
        if scope is not None:
            try:
                _normalize_scope(scope)
            except ValueError as exc:
                errors.append(
                    f"标签目录 sheet 第 {row['_row']} 行目录「{name}」：{exc}")

    dir_names = set(by_name)
    for row in tag_rows:
        name = row.get("标签名称")
        if name is None:
            errors.append(f"标签定义 sheet 第 {row['_row']} 行：标签名称为空")
            continue
        if name in dir_names:
            errors.append(f"标签「{name}」与目录同名（行分类歧义，请改名）")
        parent = row.get("所属目录名称")
        if parent is None:
            errors.append(f"标签「{name}」缺所属目录名称")
        elif parent not in dir_names:
            errors.append(f"标签「{name}」的所属目录「{parent}」不存在")

    same_dir = Counter((r.get("所属目录名称"), r.get("标签名称")) for r in tag_rows)
    for (parent, name), count in same_dir.items():
        if count > 1 and name is not None:
            errors.append(f"目录「{parent}」下标签「{name}」出现 {count} 次")

    for row in tag_rows:
        name = row.get("标签名称")
        if name is None:
            continue
        tag_type = row.get("标签类型")
        if tag_type is not None and tag_type not in TAG_TYPES:
            errors.append(
                f"标签「{name}」的标签类型「{tag_type}」非法"
                f"（须为 {' 或 '.join(TAG_TYPES)}）")

    name_dirs: dict[str, set[str | None]] = {}
    for row in tag_rows:
        name = row.get("标签名称")
        if name:
            name_dirs.setdefault(name, set()).add(row.get("所属目录名称"))
    for name, parents in sorted(name_dirs.items()):
        if len(parents) > 1:
            warnings.append(
                f"标签「{name}」跨 {len(parents)} 个目录出现——v2 唯一键为 "
                f"(目录, 名称)，属合法共存，仅提示")
    return errors, warnings


def build_tree(dir_rows: list[dict], tag_rows: list[dict]) -> ET.Element:
    """目录 + 标签行 → 树状 XML root（先子目录后标签，均按模板行序）。

    枚举列未填输出默认值属性（dir_type=dir_dynamic、scope=全选组合、
    tag_type=val_fixed），description/aliases 有值才输出（导入层缺省
    空串/空别名）；scope 有值时按规范序重组。
    """
    root = ET.Element("root")
    nodes: dict[str, ET.Element] = {}
    for row in dir_rows:
        name = row["目录名称"]
        level = _dir_level(row)
        element = ET.Element(f"level{level}")
        element.set("name", name)
        if row.get("目录ID"):
            element.set("id", row["目录ID"])
        element.set("dir_type", row.get("目录类型") or DEFAULT_DIR_TYPE)
        element.set("scope",
                    _normalize_scope(row["适用范围"])
                    if row.get("适用范围") else DEFAULT_SCOPE)
        if row.get("目录描述"):
            element.set("description", row["目录描述"])
        if row.get("别名"):
            element.set("aliases", row["别名"])
        nodes[name] = element
        parent = row.get("上级目录名称")
        (nodes[parent] if parent else root).append(element)

    tags_by_dir: dict[str, list[dict]] = {}
    for row in tag_rows:
        tags_by_dir.setdefault(row["所属目录名称"], []).append(row)
    for dir_name, elements in tags_by_dir.items():
        for row in elements:
            tag = ET.SubElement(nodes[dir_name], "tag")
            tag.text = row["标签名称"]
            if row.get("标签ID"):
                tag.set("id", row["标签ID"])
            tag.set("tag_type", row.get("标签类型") or DEFAULT_TAG_TYPE)
            if row.get("标签描述"):
                tag.set("description", row["标签描述"])
            if row.get("别名"):
                tag.set("aliases", row["别名"])
    return root


def main() -> int:
    parser = argparse.ArgumentParser(
        description="双表标签模板（标签目录 + 标签定义）→ 树状导入 XML")
    parser.add_argument("--xlsx", default="docs/movietags-template.xlsx",
                        help="模板文件路径（默认 docs/movietags-template.xlsx）")
    parser.add_argument("--out", default="docs/movietags-template.xml",
                        help="输出 XML 路径（默认 docs/movietags-template.xml）")
    args = parser.parse_args()

    source = Path(args.xlsx)
    if not source.is_file():
        print(f"错误：模板文件不存在：{source}", file=sys.stderr)
        return 1
    try:
        dir_rows, tag_rows = _read_template(source)
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    print(f"读取 {source}：目录 {len(dir_rows)} 行，标签 {len(tag_rows)} 行")

    try:
        errors, warnings = validate(dir_rows, tag_rows)
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    for message in warnings:
        print(f"提示：{message}", file=sys.stderr)
    if errors:
        print(f"校验失败（{len(errors)} 项），未生成 XML：", file=sys.stderr)
        for message in errors:
            print(f"  - {message}", file=sys.stderr)
        return 1

    root = build_tree(dir_rows, tag_rows)
    ET.indent(root, space="    ")
    data = ET.tostring(root, encoding="utf-8", xml_declaration=True)

    # 自检：产物必须能被导入解析器完整还原（目录 + 标签行数一致、零错误）
    parsed = parse_tag_xml(data)
    expected = len(dir_rows) + len(tag_rows)
    if parsed.errors or parsed.header_error or len(parsed.rows) != expected:
        print(f"错误：自检失败——解析得 {len(parsed.rows)} 行（期望 {expected}），"
              f"错误 {len(parsed.errors)} 条", file=sys.stderr)
        for error in parsed.errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    Path(args.out).write_bytes(data)
    levels = Counter(_dir_level(r) for r in dir_rows)
    print(f"已生成 {args.out}："
          f"L1 {levels[1]} / L2 {levels[2]} / L3 {levels[3]} 目录，"
          f"标签 {len(tag_rows)}，共 {expected} 行（自检通过）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
