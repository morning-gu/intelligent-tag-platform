"""把历史"审核已通过但没有正式挂载"的 pre 行补写进 `asset_tag`（一次性修复）。

背景：`asset_tag` 是资产标签的权威口径（审核生效 → 复制一行），但早期版本
审核通过只改 `pre_asset_tag.review_status='approved'`，没有落正式表；此外
早期"库内命中自动通过"（reviewer_id='system'）的行同样只有 pre 行。于是一个
资产在 `GET /assets/{id}/tags` 切到 asset_tag 之后会丢掉这些标签。

本脚本只补 `asset_tag`，不改 pre 行（保留审核痕迹）：

- 取 `pre_asset_tag` 中 `review_status='approved'` 且 `tag_id` 非空的行；
- 同键（asset_id/target_type/target_id/tag_id）已挂的跳过（幂等）；
- `tag_name` 取 `tag_define` 现值（与审核链路一致：candidate_* 不参与）；
- 默认 dry-run，只打印将写入的行；`--apply` 才落库。

用法：
    python scripts/backfill_asset_tags.py                # 预览
    python scripts/backfill_asset_tags.py --apply        # 落库
    python scripts/backfill_asset_tags.py --asset movie_tt0974661
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tagplatform.db import create_engine_for, make_session_factory  # noqa: E402
from tagplatform.db.models import (  # noqa: E402
    AssetTag,
    PreAssetTag,
    TagDefine,
)
from tagplatform.settings import get_settings  # noqa: E402


def _db_path(explicit: str | None) -> str:
    if explicit:
        return explicit
    return get_settings().database_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="真正写库")
    parser.add_argument("--asset", help="只处理某个资产 id")
    parser.add_argument("--database", help="SQLite 文件路径（默认取 Settings）")
    args = parser.parse_args(argv)

    db = _db_path(args.database)
    print(f"数据库: {db}")
    engine = create_engine_for(db)
    now = datetime.now(timezone.utc)
    session = make_session_factory(engine)()
    try:
        query = session.query(PreAssetTag).filter(
            PreAssetTag.review_status == "approved",
            PreAssetTag.tag_id.isnot(None),
        )
        if args.asset:
            query = query.filter(PreAssetTag.asset_id == args.asset)
        rows = query.order_by(PreAssetTag.asset_id, PreAssetTag.id).all()

        mounted: set[tuple[str, str, str, str]] = {
            (m.asset_id, m.target_type, m.target_id, m.tag_id)
            for m in session.query(AssetTag).all()
        }
        define_names = {
            t.tag_id: t.tag_name for t in session.query(TagDefine).all()
        }

        plan: list[tuple[PreAssetTag, str]] = []
        skipped_no_define: list[PreAssetTag] = []
        for row in rows:
            key = (row.asset_id, row.target_type, row.target_id, row.tag_id or "")
            if key in mounted:
                continue
            name = define_names.get(row.tag_id or "")
            if not name:
                skipped_no_define.append(row)
                continue
            plan.append((row, name))

        print(f"pre 中 approved 且有 tag_id: {len(rows)} 条")
        orphans = session.query(PreAssetTag).filter(
            PreAssetTag.review_status == "approved",
            PreAssetTag.tag_id.is_(None)).count()
        if orphans:
            print(f"approved 但行内无 tag_id      : {orphans} 条"
                  "（早期新词审核没回填 tag_id，挂载行已存在则无需处理）")
        print(f"已挂载（跳过）            : {len(rows) - len(plan) - len(skipped_no_define)} 条")
        print(f"待补写                    : {len(plan)} 条")
        if skipped_no_define:
            print(f"库内无定义（跳过，需人工查）: {len(skipped_no_define)} 条")
            for row in skipped_no_define[:10]:
                print(f"  - {row.asset_id} / {row.tag_id} / {row.candidate_name}")
        for row, name in plan:
            print(f"  [{row.asset_id}] {name}（tag_id={row.tag_id}，"
                  f"target={row.target_type}:{row.target_id}，origin={row.origin}）")

        if not args.apply:
            print("\n（dry-run：未写库；确认后加 --apply）")
            return 0

        for row, name in plan:
            session.add(AssetTag(
                asset_id=row.asset_id,
                target_type=row.target_type,
                target_id=row.target_id,
                tag_id=row.tag_id,
                tag_name=name,
                created_at=now,
                updated_at=now,
            ))
        session.commit()
        print(f"\n已补写 {len(plan)} 条 asset_tag 行。")
        total = session.query(AssetTag).count()
        approved = session.query(PreAssetTag).filter(
            PreAssetTag.review_status == "approved",
            PreAssetTag.tag_id.isnot(None)).count()
        print(f"对账：asset_tag={total} 行，pre approved(有 tag_id)={approved} 行")
        return 0
    finally:
        session.close()
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
