"""模式 A 分组召回：内容多段文本嵌入 → 按标签类型分组取 top_k（Task 5）。

召回是封闭式提取的候选筛选层：把内容查询文本（剧情摘要 + 各场景描述，
见 `embed_content_texts`）一次性批量嵌入，对 tag_catalog 有效行（排除
status=="deprecate" 与 embedding 为空）按 tag_type 分组；每 query × 每组
余弦检索，组内合并命中取最高分截 top_k，跨组并集按 tag_id 去重。

检索复用 vectors.search_similar（Embedder 契约保证向量已 L2 归一化，
点积即余弦）；top_k 由 ClosedTagProducer 读全局配置传入。
分层红线：本模块不得 import fastapi（设计文档 §9.4）。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from tagplatform.db.models import TagCatalog
from tagplatform.schemas import ContentJSON
from tagplatform.vectors import decode_vector, search_similar


def embed_content_texts(content: ContentJSON) -> list[str]:
    """内容 JSON → 召回查询文本列表：[剧情摘要, 场景1描述, 场景2描述, ...]。"""
    return [content.content.summary,
            *(scene.description for scene in content.content.scenes)]


def recall_by_type(session: Session, queries: list[str], embedder,
                   top_k: int) -> list[TagCatalog]:
    """按 tag_type 分组召回：组内跨 query 取最高分 top_k，跨组按 tag_id 去重。

    - 有效行：status != "deprecate"（NULL 视为有效）且 embedding 非空
      （None / 空字节均排除）；
    - queries 一次批量嵌入（embedder 契约返回 (n, d)，逐行对应 queries）；
    - 无命中返回 []。
    """
    query_vecs = embedder.embed(queries)

    # 全量有效行按 tag_type 分组（deprecate 与无向量行不参与召回）
    groups: dict[str, list[TagCatalog]] = {}
    for row in session.query(TagCatalog).all():
        if row.status == "deprecate" or not row.embedding:
            continue
        groups.setdefault(row.tag_type, []).append(row)

    picked: list[TagCatalog] = []
    seen: set[str] = set()
    for tag_type in sorted(groups):  # 组序确定：按 tag_type 排序遍历
        rows = groups[tag_type]
        rows_by_id = {row.tag_id: row for row in rows}
        candidates = [(row.tag_id, decode_vector(row.embedding)) for row in rows]
        # 每 query 命中合并：同标签跨 query 取最高分
        best: dict[str, float] = {}
        for query_vec in query_vecs:
            for tag_id, similarity in search_similar(query_vec, candidates, top_k):
                if tag_id not in best or similarity > best[tag_id]:
                    best[tag_id] = similarity
        # 组内按最高分降序截 top_k（stable 排序：同分保持首命中顺序）
        top = sorted(best.items(), key=lambda kv: -kv[1])[:top_k]
        for tag_id, _similarity in top:
            if tag_id not in seen:  # 跨组并集按 tag_id 去重
                seen.add(tag_id)
                picked.append(rows_by_id[tag_id])
    return picked
