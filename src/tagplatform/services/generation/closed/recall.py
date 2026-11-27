"""模式 A 向量召回：多段内容查询 + 最高余弦 + 全局 top_k。

1. 摘要和各场景分别作为查询，对每条标签取跨查询最高余弦；
2. 按最高余弦降序取全局 top_k，返回候选；候选 score 即该余弦，供
   prompt、落库归属与 closed 终分公式（Semb）使用。

索引通过 TagLibrary 只读视图取得，不在本模块直接查询数据库。

分层红线：本模块不得 import fastapi（设计文档 §9.4）。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from tagplatform.services.tags.library import TagLibrary, library_index
from tagplatform.schemas import ContentJSON


@dataclass(frozen=True)
class RecallCandidate:
    """闭式召回的候选行（渲染 candidates 文本 / 生成 catalog 行都用它）。

    候选列表按向量余弦降序；`score` 是该标签与内容查询文本的最高余弦
    （closed 终分公式的 Semb 输入）。默认 0 兼容手构候选行。
    """

    tag_id: str
    tag_name: str
    tag_type: str
    description: str
    dir_id: str
    scope: str
    score: float = 0.0


def embed_content_texts(content: ContentJSON) -> list[str]:
    """内容 JSON → 召回查询文本列表：[剧情摘要, 场景1描述, 场景2描述, ...]。"""
    return [content.content.summary,
            *(scene.description for scene in content.content.scenes)]


def recall_candidates(
    library: TagLibrary,
    embedder,
    queries: list[str],
    *,
    dir_ids: list[str] | None = None,
    top_k: int = 50,
) -> list[RecallCandidate]:
    """向量召回，按跨查询最高余弦降序返回全局 top_k。

    - 范围：`dir_ids`（含子目录）；空 = 全库；
    - 库为空 / 查询为空 → 返回 []（调用方决定是否按"库未就绪"失败）；
    - 每条标签取跨 query 的最高余弦，排序后截断到 top_k；
    - 候选 score 即最高余弦，供闭环终分使用。
    """
    if not queries or top_k <= 0:
        return []
    nonempty_queries = [query for query in queries if query.strip()]
    if not nonempty_queries:
        return []
    index = library_index(library, embedder, dir_ids)
    if not index:
        return []

    query_vecs = np.asarray(
        embedder.embed(nonempty_queries, role="query"), dtype=np.float32)
    if query_vecs.size == 0:
        return []
    matrix = np.stack([tag.vector for tag in index])
    best = (matrix @ query_vecs.T).max(axis=1)

    ranked = sorted(range(len(index)),
                    key=lambda pos: (-float(best[pos]), pos))
    by_id = {tag.tag_id: tag for tag in library.tags()}
    picked: list[RecallCandidate] = []
    seen: set[str] = set()
    for pos in ranked:
        tag = index[pos]
        if tag.tag_id in seen:
            continue
        seen.add(tag.tag_id)
        definition = by_id.get(tag.tag_id)
        if definition is None:
            continue
        picked.append(RecallCandidate(
            tag_id=definition.tag_id,
            tag_name=definition.tag_name,
            tag_type=definition.tag_type,
            description=definition.description,
            dir_id=definition.dir_id,
            scope=library.scope_of(definition.dir_id),
            score=float(best[pos]),
        ))
        if len(picked) >= top_k:
            break
    return picked
