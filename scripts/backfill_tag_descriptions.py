"""把"剧情式"标签描述改写成通用释义（一次性数据修复）。

背景：生成侧曾把 `reason`（本片里的内容依据）直接当 `candidate_description`
落库，审核通过后又原样写进 `tag_define.description`，于是标签库里出现
"总统蜷身藏于货舱，空间封闭压迫"这种只对某一部片成立的文字。生成逻辑已经
改成要求"词本身的通用释义"，本脚本负责把**已经写坏的历史数据**改回来：

- `tag_define.description`：用创建日志里的来源资产（`source.asset_id`）取该片
  专名，判"是否掺了这部片的人名/片名"；不合规的重新补写。
- `pre_asset_tag.candidate_description`：待审候选同理（按所在资产的内容判）。

改写一律走 `fill_descriptions`（与生成链路同一个提示词与校验），补不出来就
**留空**而不是保留原剧情文字。每次改写都落 `tag_change_log(action='update')`，
before/after 可回溯。

默认为 dry-run（只打印将要改什么），加 `--apply` 才写库。

用法：
    python scripts/backfill_tag_descriptions.py                 # 看看会改哪些
    python scripts/backfill_tag_descriptions.py --apply         # 真改
    python scripts/backfill_tag_descriptions.py --asset movie_tt0118571 --apply
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from tagplatform.content_io import load_content
from tagplatform.db import create_engine_for, make_session_factory
from tagplatform.db.models import (
    MediaAsset,
    PreAssetTag,
    TagCatalog,
    TagChangeLog,
    TagDefine,
)
from tagplatform.generation.mtg.descriptions import (
    clean_description,
    content_names,
    fill_descriptions,
)
from tagplatform.generation.mtg.merge import normalize_tag
from tagplatform.settings import get_settings

_BATCH = 8


def _dir_paths(session: Session) -> dict[str, str]:
    """dir_id → 全路径（"父 / 子"）；补写提示里只作归属提示用。"""
    rows = {row.dir_id: row for row in session.query(TagCatalog).all()}
    paths: dict[str, str] = {}

    def path_of(dir_id: str) -> str:
        if dir_id in paths:
            return paths[dir_id]
        names, seen = [], set()
        current = rows.get(dir_id)
        while current is not None and current.dir_id not in seen:
            seen.add(current.dir_id)
            names.append(current.dir_name)
            current = rows.get(current.parent_id) if current.parent_id else None
        paths[dir_id] = " / ".join(reversed(names))
        return paths[dir_id]

    for dir_id in rows:
        path_of(dir_id)
    return paths


def _names_of_asset(session: Session, asset_id: str | None) -> tuple[str, ...]:
    if not asset_id:
        return ()
    row = session.get(MediaAsset, asset_id)
    if row is None or not row.content_json:
        return ()
    try:
        return content_names(load_content(row.content_json))
    except Exception:  # noqa: BLE001 —— 内容写坏的资产按"取不到专名"处理
        return ()


def _create_sources(session: Session) -> dict[str, str]:
    """tag_id → 创建时的来源资产 id（审核入库写了 source.asset_id）。"""
    sources: dict[str, str] = {}
    for log in (session.query(TagChangeLog)
                .filter(TagChangeLog.action == "create").all()):
        try:
            payload = json.loads(log.after_json or "{}")
        except json.JSONDecodeError:
            continue
        asset_id = (payload.get("source") or {}).get("asset_id")
        if asset_id:
            sources[log.tag_id] = asset_id
    return sources


def _batched(items: list[dict], size: int = _BATCH) -> list[list[dict]]:
    return [items[index:index + size] for index in range(0, len(items), size)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把剧情式标签描述改成通用释义")
    parser.add_argument("--database", default=None, help="SQLite 数据库路径")
    parser.add_argument("--asset", default=None, help="只处理某个资产（asset_id）")
    parser.add_argument("--apply", action="store_true", help="真正写库")
    args = parser.parse_args(argv)

    settings = get_settings()
    database = args.database or settings.database_path
    if not Path(database).is_file():
        print(f"数据库不存在: {database}", file=sys.stderr)
        return 2

    from tagplatform.llm import OpenAIClient

    client = OpenAIClient(
        model=settings.mtg_judge_model,
        base_url_env=settings.llm_base_url_env,
        api_key_env=settings.llm_api_key_env,
        temperature=0.0,
        request_interval=settings.llm_request_interval,
        max_tokens=2000,
    )

    engine = create_engine_for(database)
    session = make_session_factory(engine)()
    try:
        paths = _dir_paths(session)
        sources = _create_sources(session)
        now = datetime.now(timezone.utc)

        # ---- 1. tag_define：只碰"生成 → 审核入库"造出来的标签 ----
        # 库里 500+ 条导入标签的描述来自人工整理的表，不能用启发式去改；
        # 只有带创建日志来源资产（source.asset_id）的才是审核新建的产物——这类
        # 描述按构造来自生成侧，一律重写成通用释义（不靠文本启发式筛选：专名
        # 清单未必覆盖到具体人名，"总统蜷身藏于货舱"这种就可能漏判）。
        define_targets: list[tuple[TagDefine, tuple[str, ...]]] = []
        for asset_id, tag_id in sources.items():
            row = session.get(TagDefine, tag_id)
            if row is None or not (row.description or "").strip():
                continue
            define_targets.append((row, _names_of_asset(session, asset_id)))

        # ---- 2. pre_asset_tag：生成候选（candidate_name 非空）的描述 ----
        # 这些行的描述按构造就是"本片里的依据"（生成侧把 reason 写在这里），
        # 不做启发式筛选——全部重写成通用释义。
        pre_query = session.query(PreAssetTag).filter(
            PreAssetTag.candidate_description.isnot(None),
            PreAssetTag.candidate_name.isnot(None),
            PreAssetTag.review_status == "pending")
        if args.asset:
            pre_query = pre_query.filter(PreAssetTag.asset_id == args.asset)
        pre_by_asset: dict[str, list[PreAssetTag]] = {}
        for row in pre_query.all():
            pre_by_asset.setdefault(row.asset_id, []).append(row)

        print(f"数据库: {database}")
        print(f"待改写的已入库标签描述: {len(define_targets)} 条")
        for row, _names in define_targets:
            print(f"  - {row.tag_name}（{paths.get(row.dir_id, '?')}）：{row.description}")
        total_pre = sum(len(rows) for rows in pre_by_asset.values())
        print(f"待改写的待审候选描述: {total_pre} 条（{len(pre_by_asset)} 个资产）")

        if not define_targets and not pre_by_asset:
            print("\n没有需要改写的描述。")
            return 0

        # ---- 3. 补写（dry-run 也真调一次，才能预览将要写入的内容）----
        planned: list[tuple[TagDefine | PreAssetTag, str, str | None, tuple]] = []

        # 专名清单相同的行一起补写：同一部片来源的标签共用一份 names
        groups: dict[tuple[str, ...], list[TagDefine]] = {}
        for row, names in define_targets:
            groups.setdefault(names, []).append(row)
        for names, group_rows in groups.items():
            for batch in _batched(group_rows, _BATCH):
                items = [{"tag": row.tag_name, "dir": paths.get(row.dir_id)}
                         for row in batch]
                filled = fill_descriptions(
                    client, items, names=names, run_id="backfill:define")
                for row in batch:
                    planned.append((row, "tag_define",
                                    filled.get(normalize_tag(row.tag_name)), names))
        for asset_id, rows in pre_by_asset.items():
            names = _names_of_asset(session, asset_id)
            items = [{"tag": row.candidate_name or "", "dir": paths.get(row.candidate_dir_id)}
                     for row in rows]
            filled = fill_descriptions(
                client, [item for item in items if item["tag"]],
                names=names, run_id=f"backfill:{asset_id}")
            for row, item in zip(rows, items):
                planned.append((row, "pre_asset_tag",
                                filled.get(normalize_tag(item["tag"])), names))

        changed = 0
        for row, kind, desc, _names in planned:
            old = (row.description if kind == "tag_define"
                   else row.candidate_description)
            if desc == clean_description(old):
                continue
            changed += 1
            label = row.tag_name if kind == "tag_define" else row.candidate_name
            print(f"  [{kind}] {label}: {old!r} -> {desc!r}")
            if not args.apply:
                continue
            if kind == "tag_define":
                row.description = desc or ""
                row.updated_at = now
                session.add(TagChangeLog(
                    tag_id=row.tag_id, action="update",
                    before_json=json.dumps({"description": old}, ensure_ascii=False),
                    after_json=json.dumps(
                        {"tag_id": row.tag_id, "tag_name": row.tag_name,
                         "dir_id": row.dir_id, "description": row.description},
                        ensure_ascii=False),
                    operator="backfill", created_at=now))
            else:
                row.candidate_description = desc
                row.updated_at = now

        if not args.apply:
            print(f"\n[dry-run] 共 {changed} 条会改写；确认无误后加 --apply 执行。")
            return 0
        session.commit()
        print(f"\n已改写 {changed} 条。")
        return 0
    finally:
        session.close()
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
