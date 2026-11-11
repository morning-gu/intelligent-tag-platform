"""模式 A 分组召回：内容多段文本嵌入 → 按目录分组取 top_k。

召回是封闭式提取的候选筛选层，数据来自**标签库（JSON 模拟）**，不直接读
`tag_catalog`：

1. 内容查询文本（剧情摘要 + 各场景描述）一次性批量嵌入；
2. 库内标签向量（`library.library_index`，按库/模型/范围缓存）与每个 query
   做余弦；同一标签跨 query 取最高分；
3. **按目录分组**：每组（一个目录）取最高分的 top_k 条，再跨组按分数降序
   合并——这样"冷门目录"不会被热门目录整片挤掉；
4. 候选带上目录与挂载粒度，供 prompt 展示和落库归属使用。

分层红线：本模块不得 import fastapi（设计文档 §9.4）。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from tagplatform.library import TagLibrary, library_index
from tagplatform.schemas import ContentJSON


@dataclass(frozen=True)
class RecallCandidate:
    """闭式召回的候选行（渲染 candidates 文本 / 生成 catalog 行都用它）。"""

    tag_id: str
    tag_name: str
    tag_type: str
    description: str
    dir_id: str
    scope: str


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
    """按目录分组召回：组内跨 query 取最高分 top_k，跨组按分数降序合并。

    - 范围：`dir_ids`（含子目录）；空 = 全库；
    - 库为空 / 查询为空 → 返回 []（调用方决定是否按"库未就绪"失败）；
    - 分数是余弦（向量已归一化，点积即余弦）。
    """
    index = library_index(library, embedder, dir_ids)
    if not index or not queries:
        return []
    query_vecs = np.asarray(embedder.embed([q for q in queries if q], role="query"),
                            dtype=np.float32)
    if query_vecs.size == 0:
        return []
    matrix = np.stack([tag.vector for tag in index])
    best = (matrix @ query_vecs.T).max(axis=1)

    # 目录 → 候选下标（保持库内顺序，保证同分时结果稳定）
    groups: dict[str, list[int]] = {}
    for pos, tag in enumerate(index):
        groups.setdefault(tag.dir_id or "", []).append(pos)

    scored: list[tuple[float, int]] = []
    for positions in groups.values():
        ranked = sorted(positions, key=lambda pos: (-float(best[pos]), pos))
        scored.extend((float(best[pos]), pos) for pos in ranked[:top_k])
    scored.sort(key=lambda item: (-item[0], item[1]))

    tags = library.tags()
    by_id = {tag.tag_id: tag for tag in tags}
    picked: list[RecallCandidate] = []
    seen: set[str] = set()
    for score, pos in scored:
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
        ))
    return picked
