"""一次性向量迁移：存量无前缀向量 → e5 role 前缀空间重嵌（2026-11-02）。

背景：LocalEmbedder 落地 query:/passage: 前缀后，历史入库向量（无前缀
空间）与新向量不在同一空间——本模块把 tag_catalog / media_context 的
存量向量按新契约重嵌。纯数据迁移，无 schema 变更；幂等（重跑只是再嵌
一遍）。

- 目录行文本 ``f"{tag_name}。{description}"``、role=passage（与治理入库
  同源同形）；
- 上下文行文本取 content_json 的剧情摘要、role=passage（与 runner 模式 A
  回填同源同形）；
- 仅处理 embedding 非空的行（NULL 保持 NULL：纯迁移不新增行为）；
- 不 commit：调用方（CLI main / 测试）统一事务边界。

用法（服务器，加载 .env 走真实 e5）::

    cd /cache/g00619218/intelligent-tag-platform && \\
    set -a && source .env && set +a && \\
    .venv/bin/python -m tagplatform.reembed

分层红线：本模块不得 import fastapi（设计文档 §9.4）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from tagplatform.db.models import MediaContext, TagCatalog
from tagplatform.content_io import load_content
from tagplatform.schemas import parse_content
from tagplatform.vectors import encode_vector


@dataclass
class ReembedSummary:
    """迁移结果摘要：重嵌的目录行数 / 上下文行数。"""

    catalog_rows: int
    context_rows: int


def reembed_all(session: Session, embedder) -> ReembedSummary:
    """重嵌库内全部存量向量（不 commit，调用方提交事务）。

    目录与上下文两侧均为 passage（被检索语料 / 内容侧文档），文本与
    各自的原始写入路径（治理入库 / runner 回填）逐字同源——保证迁移后
    与后续新增行落在同一空间同一分布。
    """
    catalog_rows = [
        row for row in session.query(TagCatalog).all() if row.embedding is not None
    ]
    context_rows = [
        row for row in session.query(MediaContext).all()
        if row.embedding is not None
    ]
    now = datetime.now(timezone.utc)

    if catalog_rows:
        texts = [f"{row.tag_name}。{row.description}" for row in catalog_rows]
        vecs = embedder.embed(texts, role="passage")
        for row, vec in zip(catalog_rows, vecs):
            row.embedding = encode_vector(vec)
            row.updated_at = now
    if context_rows:
        texts = [
            load_content(row.content_json).content.summary
            for row in context_rows
        ]
        vecs = embedder.embed(texts, role="passage")
        for row, vec in zip(context_rows, vecs):
            row.embedding = encode_vector(vec)

    return ReembedSummary(catalog_rows=len(catalog_rows),
                          context_rows=len(context_rows))


def main() -> None:
    """CLI 入口：按运行环境配置（.env / TAGPLT_*）连接库并执行迁移。"""
    from tagplatform.db import create_engine_for, make_session_factory
    from tagplatform.embed import make_embedder
    from tagplatform.settings import get_settings

    settings = get_settings()
    engine = create_engine_for(settings.database_path)
    embedder = make_embedder(settings.embedding_model,
                             mock=settings.use_mock_embedder)
    try:
        factory = make_session_factory(engine)
        with factory() as session:
            summary = reembed_all(session, embedder)
            session.commit()
        print(f"reembed 完成：tag_catalog {summary.catalog_rows} 行、"
              f"media_context {summary.context_rows} 行"
              f"（embedder: {embedder.name}）")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
