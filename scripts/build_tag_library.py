"""标签库模板（xlsx）→ JSON 目录/标签库（本地模拟 tag_catalog + tag_define）。

背景：标签库的两张正式表（`tag_catalog` 目录 / `tag_define` 标签）由外部
提供，本期不读写这两张表，改用 JSON 模拟其重要字段：

- `dirs.json`   ← `docs/movietags-template.xlsx` 的「标签目录」sheet
- `tags.json`   ← 同文件的「标签定义」sheet

生成规则（与设计文档字段一一对应）：
- 目录：dir_id / parent_id / level / dir_name / dir_type / applicable_scope /
  description / aliases；dir_type ∈ {dir_static, dir_dynamic}，
  applicable_scope ∈ {content, scene, character}；
- 标签：tag_id / dir_id / tag_name / tag_type / description / aliases / status，
  tag_type ∈ {val_fixed, val_generalized}。

id 生成：模板里 dir_id/tag_id 允许留空，这里按"目录全路径"和"目录+标签名"
的 sha1 前 8 位生成稳定 id——同一份模板重复生成结果一致，改名才会变。

用法：python scripts/build_tag_library.py [--xlsx docs/movietags-template.xlsx]
      [--out data/taglibrary]
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import openpyxl

DIR_STATIC = "dir_static"
DIR_DYNAMIC = "dir_dynamic"
SCOPES = ("content", "scene", "character")


def _stable_id(prefix: str, text: str) -> str:
    """稳定 id：前缀 + sha1 前 8 位（同一输入永远同一 id）。"""
    return f"{prefix}_{hashlib.sha1(text.encode('utf-8')).hexdigest()[:8]}"


def _split_aliases(raw: object) -> list[str]:
    """别名单元格（"a|b"）→ 列表；空值给空列表。"""
    text = str(raw or "").strip()
    if not text:
        return []
    return [part.strip() for part in text.split("|") if part.strip()]


def build_dirs(sheet) -> list[dict]:
    """「标签目录」sheet → 目录行列表（保持表内顺序，父先于子）。"""
    rows = [row for row in sheet.iter_rows(min_row=3, values_only=True) if row[3]]
    by_name: dict[str, str] = {}
    dirs: list[dict] = []
    for raw in rows:
        _dir_id_cell, level, parent_name, name, dir_type, scope, desc, aliases = raw[:8]
        name = str(name).strip()
        parent_id = by_name.get(str(parent_name or "").strip()) if parent_name else None
        path = f"{parent_id or ''}/{name}"
        dir_id = str(_dir_id_cell).strip() if _dir_id_cell else _stable_id("dir", path)
        by_name[name] = dir_id
        dirs.append({
            "dir_id": dir_id,
            "parent_id": parent_id,
            "level": int(level or 1),
            "dir_name": name,
            "dir_type": str(dir_type or DIR_STATIC).strip(),
            "applicable_scope": str(scope or "content").strip(),
            "description": str(desc or "").strip(),
            "aliases": _split_aliases(aliases),
        })
    return dirs


def build_tags(sheet, dirs: list[dict]) -> list[dict]:
    """「标签定义」sheet → 标签行列表；所属目录按名称解析成 dir_id。"""
    by_name = {d["dir_name"]: d["dir_id"] for d in dirs}
    tags: list[dict] = []
    for raw in sheet.iter_rows(min_row=3, values_only=True):
        if not raw[1]:
            continue
        tag_id_cell, name, dir_name, tag_type, desc, aliases = raw[:6]
        name = str(name).strip()
        dir_id = by_name.get(str(dir_name or "").strip())
        if dir_id is None:
            raise SystemExit(f"标签「{name}」的所属目录不存在: {dir_name!r}")
        tag_id = (str(tag_id_cell).strip() if tag_id_cell
                  else _stable_id("tag", f"{dir_id}/{name}"))
        tags.append({
            "tag_id": tag_id,
            "dir_id": dir_id,
            "tag_name": name,
            "tag_type": str(tag_type or "val_fixed").strip(),
            "description": str(desc or "").strip(),
            "aliases": _split_aliases(aliases),
            "status": "active",
        })
    return tags


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--xlsx", default="docs/movietags-template.xlsx")
    parser.add_argument("--out", default="data/taglibrary")
    args = parser.parse_args()

    workbook = openpyxl.load_workbook(args.xlsx, read_only=True)
    dirs = build_dirs(workbook["标签目录"])
    tags = build_tags(workbook["标签定义"], dirs)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    version = "2026-11-template"
    (out / "dirs.json").write_text(json.dumps({
        "version": version,
        "source": args.xlsx,
        "dirs": dirs,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "tags.json").write_text(json.dumps({
        "version": version,
        "source": args.xlsx,
        "tags": tags,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    dynamic = sum(1 for d in dirs if d["dir_type"] == DIR_DYNAMIC)
    print(f"目录 {len(dirs)} 个（可扩展 {dynamic}）；标签 {len(tags)} 个 → {out}")


if __name__ == "__main__":
    main()
