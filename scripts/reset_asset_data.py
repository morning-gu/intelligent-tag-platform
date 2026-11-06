"""清空资产侧数据（上传的影片、场景、构建任务、内容上下文、标签）。

用途：资产 id 统一成 `asset_xxxxxxxx` 之后，旧的 `movie_<imdb>` 存量不再
需要——重新上传生成即可。本脚本按外键依赖顺序删，避免留下孤儿行。

**会删**（资产侧全部）：
  review_record → asset_tag → generation_task → media_context
  → asset_build_task → media_asset
**不动**：tag_catalog（标签库/分类目录）、import_batch、user_account、
  prompt_template、alembic_version。

默认为 dry-run（只打印会删多少行），加 `--yes` 才真正执行。
数据库路径默认取 `TAGPLT_DATABASE_PATH`/Settings，可用 `--database` 覆盖。

用法：
    python scripts/reset_asset_data.py                    # 看看会删什么
    python scripts/reset_asset_data.py --yes              # 真删
    python scripts/reset_asset_data.py --database /path/x.db --yes
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sqlalchemy import delete, func, select

from tagplatform.db import create_engine_for, make_session_factory
from tagplatform.db.models import (
    AssetBuildTask,
    AssetTag,
    GenerationTask,
    MediaAsset,
    MediaContext,
    ReviewRecord,
)
from tagplatform.settings import get_settings

# (展示名, 计数语句, 删除语句)；顺序 = 外键依赖的反向
_PLAN = [
    ("review_record", select(func.count()).select_from(ReviewRecord),
     delete(ReviewRecord)),
    ("asset_tag", select(func.count()).select_from(AssetTag), delete(AssetTag)),
    ("generation_task", select(func.count()).select_from(GenerationTask),
     delete(GenerationTask)),
    ("media_context", select(func.count()).select_from(MediaContext),
     delete(MediaContext)),
    ("asset_build_task", select(func.count()).select_from(AssetBuildTask),
     delete(AssetBuildTask)),
    # 自引用外键：先删场景行，再删影片行
    ("media_asset(scene)",
     select(func.count()).select_from(MediaAsset).where(
         MediaAsset.asset_type == "scene"),
     delete(MediaAsset).where(MediaAsset.asset_type == "scene")),
    ("media_asset(movie)",
     select(func.count()).select_from(MediaAsset).where(
         MediaAsset.asset_type != "scene"),
     delete(MediaAsset).where(MediaAsset.asset_type != "scene")),
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="清空资产侧数据（默认 dry-run）")
    parser.add_argument("--database", default=None, help="SQLite 数据库路径")
    parser.add_argument("--yes", action="store_true", help="真正执行删除")
    args = parser.parse_args(argv)

    database = args.database or get_settings().database_path
    if not Path(database).is_file():
        print(f"数据库不存在: {database}", file=sys.stderr)
        return 2

    engine = create_engine_for(database)
    session = make_session_factory(engine)()
    try:
        counts = [(name, session.execute(count_stmt).scalar_one())
                  for name, count_stmt, _delete_stmt in _PLAN]
        total = sum(count for _, count in counts)
        print(f"数据库: {database}")
        for name, count in counts:
            print(f"  {name:<22} {count:>7} 行")
        if total == 0:
            print("没有可清理的数据。")
            return 0
        if not args.yes:
            print(f"\n[dry-run] 共 {total} 行待删除；确认无误后加 --yes 执行。")
            return 0

        for _name, _count_stmt, delete_stmt in _PLAN:
            session.execute(delete_stmt)
        session.commit()
        print(f"\n已删除 {total} 行。tag_catalog 未动。")
        return 0
    finally:
        session.close()
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
