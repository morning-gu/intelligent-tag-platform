"""把存量视频文件改名成 `{asset_id}{后缀}` 并清理孤儿文件。

背景：早期上传按 uuid 命名存盘，重复上传会在 media 目录里堆孤儿文件。
现在存盘名固定为 `{asset_id}{后缀}`（一资产一个文件），存量数据需要一次性
对齐：重命名 → 改 `video_uri`（影片行与场景行同时改）→ 删掉没人引用的文件。

场景行与影片行指向同一个物理文件（场景没有独立视频），所以命名按
"文件的归属资产"取：非 scene 行优先，其次父级 id；同一路径的所有行
一起改。

默认为 dry-run（只打印计划）。加 `--yes` 才真正改名/写库/删文件；
`--keep-orphans` 可只改名不删孤儿。

用法：
    python scripts/normalize_media_files.py                 # 看看会动什么
    python scripts/normalize_media_files.py --yes           # 改名 + 删孤儿
    python scripts/normalize_media_files.py --yes --keep-orphans

数据库路径默认取 `TAGPLT_DATABASE_PATH`/Settings，可用 `--database` 覆盖；
媒体目录默认取 `TAGPLT_MEDIA_ROOT`/Settings，可用 `--media-root` 覆盖。
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from tagplatform.db.models import MediaAsset  # noqa: E402


def _owner_id(group: list[MediaAsset]) -> str:
    """一个物理文件的归属资产 id：非场景行优先，其次父级，最后兜底。"""
    for asset in group:
        if asset.asset_type != "scene":
            return asset.asset_id
    parent = next((a.parent_id for a in group if a.parent_id), None)
    if parent:
        return parent
    # 兜底：去掉场景分隔符，避免 `::` 在文件名里出现
    return group[0].asset_id.replace("::", "__")


def plan(session: Session, media_root: Path) -> tuple[list[tuple[Path, Path, list[MediaAsset]]], list[Path]]:
    """返回 (改名计划 [(旧, 新, 受影响行)], 孤儿文件)。"""
    by_path: dict[Path, list[MediaAsset]] = defaultdict(list)
    for asset in session.scalars(select(MediaAsset)):
        if not asset.video_uri:
            continue
        path = Path(asset.video_uri)
        if path.is_absolute():  # 只处理绝对路径（相对路径是历史测试数据）
            by_path[path].append(asset)

    moves: list[tuple[Path, Path, list[MediaAsset]]] = []
    keep: set[Path] = set()
    for old, group in by_path.items():
        target = media_root / f"{_owner_id(group)}{old.suffix.lower()}"
        keep.add(target)
        if old != target:
            moves.append((old, target, group))

    existing = {p for p in media_root.iterdir() if p.is_file()} if media_root.is_dir() else set()
    orphans = sorted(existing - keep)
    return moves, orphans


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="视频文件改名 + 孤儿清理（默认 dry-run）")
    parser.add_argument("--database", help="SQLite 路径（默认取 TAGPLT_DATABASE_PATH）")
    parser.add_argument("--media-root", help="媒体目录（默认取 TAGPLT_MEDIA_ROOT）")
    parser.add_argument("--yes", action="store_true", help="真正执行")
    parser.add_argument("--keep-orphans", action="store_true", help="只改名，不删孤儿")
    args = parser.parse_args(argv)

    from tagplatform.settings import get_settings
    settings = get_settings()
    database = args.database or settings.database_path
    media_root = Path(args.media_root or settings.media_root)

    engine = create_engine(f"sqlite:///{database}", future=True)
    with Session(engine) as session:
        moves, orphans = plan(session, media_root)
        print(f"数据库: {database}")
        print(f"媒体目录: {media_root}")
        for old, new, group in moves:
            ids = ", ".join(a.asset_id for a in group)
            print(f"  改名 {old.name} → {new.name}   [{ids}]")
        if not moves:
            print("  没有需要改名的文件")
        if not args.keep_orphans:
            total = sum(p.stat().st_size for p in orphans)
            print(f"  孤儿文件 {len(orphans)} 个，共 {total / 1048576:.1f} MB")
            for path in orphans:
                print(f"    {path.name}  {path.stat().st_size} 字节")

        if not args.yes:
            print("\n[dry-run] 加 --yes 才会真正执行。")
            return 0

        conflicts = [new for old, new, _ in moves if new.exists() and new != old]
        if conflicts:
            print("\n目标文件已存在，拒绝覆盖（请先人工确认）：")
            for path in conflicts:
                print(f"  {path}")
            return 1

        for old, new, group in moves:
            if old.exists():
                os.replace(old, new)
            for asset in group:
                asset.video_uri = str(new.resolve())
                session.add(asset)
        session.commit()

        removed = 0
        if not args.keep_orphans:
            for path in orphans:
                path.unlink(missing_ok=True)
                removed += 1
        print(f"\n已改名 {len(moves)} 个文件，删除孤儿 {removed} 个。")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
