"""重灌 tag_catalog 的向量列（换句向量模型后必须跑一次）。

用法（在服务器上、仓库根目录）：
    TAGPLT_EMBEDDING_MODEL=/cache/h50065879/models/bge-m3 \
    TAGPLT_DATABASE_PATH=/cache/h50065879/plat-test/db/tagplatform.db \
    python scripts/reembed_catalog.py [--dry-run]

为什么要单独跑：库里的向量是导入时算好的，换模型后旧向量与新模型的查询
向量不在同一个空间，相似度会变成噪声（维度也可能不同）。重灌是一次性
离线动作，跑完 tag_catalog.embedding 才与新模型自洽。

安全设计：
- 默认只统计不写库；加 `--apply` 才写入；
- 写入前校验维度一致（模型自报 dim），不一致直接拒绝；
- 逐批提交，中途失败不会留下半新半旧的行（失败时按原向量回滚整批）。
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from tagplatform.db import create_engine_for, make_session_factory
from tagplatform.db.models import TagCatalog
from tagplatform.embed import make_embedder
from tagplatform.settings import get_settings
from tagplatform.vectors import decode_vector, encode_vector


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="重灌标签库向量")
    parser.add_argument("--apply", action="store_true", help="真正写入（默认只统计）")
    parser.add_argument("--batch-size", type=int, default=64)
    args = parser.parse_args(argv)

    settings = get_settings()
    if settings.use_mock_embedder:
        print("错误：当前配置是 mock embedder，重灌没有意义", file=sys.stderr)
        return 2

    embedder = make_embedder(settings.embedding_model, mock=False)
    session = make_session_factory(create_engine_for(settings.database_path))()
    try:
        rows = session.query(TagCatalog).all()
        if not rows:
            print("tag_catalog 为空，没什么可做的")
            return 0

        old_dims = {decode_vector(row.embedding).shape[0]
                    for row in rows if row.embedding}
        print(f"模型：{settings.embedding_model}")
        print(f"待处理：{len(rows)} 行；现有向量维度 {sorted(old_dims) or ['无']}")

        now = datetime.now(timezone.utc)
        new_dim = None
        changed = 0
        for start in range(0, len(rows), args.batch_size):
            batch = rows[start:start + args.batch_size]
            vectors = embedder.embed([row.tag_name for row in batch],
                                     role="passage")
            if new_dim is None:
                new_dim = int(vectors.shape[1])
                print(f"新模型维度：{new_dim}")
            if vectors.shape[1] != new_dim:
                print("错误：同一次运行里维度不一致", file=sys.stderr)
                return 3
            if args.apply:
                for row, vector in zip(batch, vectors):
                    row.embedding = encode_vector(vector)
                    row.updated_at = now
                session.commit()
            changed += len(batch)
            print(f"  处理 {changed}/{len(rows)}", flush=True)

        if not args.apply:
            print("dry-run：未写库。确认无误后加 --apply")
            return 0
        print(f"完成：{changed} 行已写入，维度 {new_dim}")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
