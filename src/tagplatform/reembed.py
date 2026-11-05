"""全量向量重建：目录/标签双通道按 e5 passage 契约重嵌（v2）。

v2 表结构（目录/标签拆双表）下的 reembed 职责：把 tag_catalog（目录）与
tag_define（标签）两张表的**全部行**按当前嵌入器重算 embedding 覆盖落库
——全表语义，NULL 行也回填（0007 重建后库可为全新，本模块即回填/换模
工具）。context 通道删除：v2 media_asset 无 embedding 列（内容向量在
生成任务启动时实时计算，doc §3.1）。

两通道文本与各自写入路径（治理入库）逐字同源同形、role 一律 passage
（入库语料是被检索的 passage 侧，e5 契约——迁移/重建后与后续新增行落在
同一空间同一分布）：

- 目录行文本 ``f"{dir_name}。{description}"``；
- 标签行文本 ``f"{tag_name}。{description}"``；

不 commit：调用方（CLI main / 测试）统一事务边界。

用法（服务器，加载 .env 走真实 e5）::

    cd /cache/g00619218/intelligent-tag-platform && \\
    set -a && source .env && set +a && \\
    .venv/bin/python -m tagplatform.reembed

分层红线：本模块不得 import fastapi（设计文档 §9.4）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from tagplatform.db.models import TagCatalog, TagDefine
from tagplatform.vectors import encode_vector


@dataclass
class ReembedSummary:
    """重建结果摘要：重嵌的目录行数 / 标签行数。"""

    dir_rows: int
    tag_rows: int


def reembed_all(session: Session, embedder) -> ReembedSummary:
    """全表重嵌目录与标签两通道（不 commit，调用方提交事务）。

    目录通道在前、标签通道在后，各自一次批量调用；两通道均 passage
    （被检索语料），文本与治理入库写入路径逐字同源（"名。描述"）。
    """
    dirs = session.query(TagCatalog).all()
    tags = session.query(TagDefine).all()
    now = datetime.now(timezone.utc)

    if dirs:
        vecs = embedder.embed(
            [f"{row.dir_name}。{row.description}" for row in dirs],
            role="passage")
        for row, vec in zip(dirs, vecs):
            row.embedding = encode_vector(vec)
            row.updated_at = now
    if tags:
        vecs = embedder.embed(
            [f"{row.tag_name}。{row.description}" for row in tags],
            role="passage")
        for row, vec in zip(tags, vecs):
            row.embedding = encode_vector(vec)
            row.updated_at = now

    return ReembedSummary(dir_rows=len(dirs), tag_rows=len(tags))


def main() -> None:
    """CLI 入口：按运行环境配置（.env / TAGPLT_*）连接库并执行重建。"""
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
        print(f"reembed 完成：tag_catalog {summary.dir_rows} 行、"
              f"tag_define {summary.tag_rows} 行"
              f"（embedder: {embedder.name}）")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
