"""模式 A 混合召回：多段内容查询 + 向量/BM25 双通道 + RRF。

1. 摘要和各场景分别作为查询，向量通道对每条标签取跨查询最高余弦；
2. BM25 文档由标签名、描述和别名构成，中文用字/二元/三元片段检索；
3. 别名命中映射回其规范 tag_id，按 tag_id 合并两路候选；
4. 按通道名次做 RRF，返回全局 top_k；候选 score 仍保留最高余弦，供
   prompt、落库归属与 closed 终分公式（Semb）使用。

索引通过 TagLibrary 只读视图取得，不在本模块直接查询数据库。

分层红线：本模块不得 import fastapi（设计文档 §9.4）。
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass

import numpy as np

from tagplatform.services.tags.library import TagLibrary, library_index
from tagplatform.schemas import ContentJSON

_RRF_K = 60
_BM25_K1 = 1.5
_BM25_B = 0.75
_KEYWORD_TOKEN_RE = re.compile(
    r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+|[^\W_]+",
    re.UNICODE,
)
_HAN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")


@dataclass(frozen=True)
class RecallCandidate:
    """闭式召回的候选行（渲染 candidates 文本 / 生成 catalog 行都用它）。

    候选列表按向量和关键词通道的 RRF 排序；`score` 保留该标签与内容查询
    文本的最高余弦（closed 终分公式的 Semb 输入）。默认 0 兼容手构候选行。
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


def _keyword_tokens(text: str) -> list[str]:
    """切分 BM25 词项：拉丁/数字按词，汉字串按字、二元与三元字符片段。"""
    normalized = unicodedata.normalize("NFKC", text).casefold()
    terms: list[str] = []
    for token in _KEYWORD_TOKEN_RE.findall(normalized):
        if _HAN_RE.fullmatch(token):
            terms.extend(token)
            terms.extend(token[pos:pos + 2] for pos in range(len(token) - 1))
            terms.extend(token[pos:pos + 3] for pos in range(len(token) - 2))
        else:
            terms.append(token)
    return terms


def _compact_keyword(text: str) -> str:
    """折叠空白和标点，供标签名/别名的精确短语命中判断。"""
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return "".join(char for char in normalized if char.isalnum())


def _bm25_scores(
    documents: list[list[str]],
    queries: list[list[str]],
) -> list[float]:
    """对每条内容查询分别计算 BM25，保留每个标签的最高查询分。"""
    document_count = len(documents)
    if not document_count:
        return []

    term_frequencies = [Counter(document) for document in documents]
    postings: dict[str, list[tuple[int, int]]] = {}
    for pos, frequencies in enumerate(term_frequencies):
        for term, frequency in frequencies.items():
            postings.setdefault(term, []).append((pos, frequency))

    lengths = [len(document) for document in documents]
    average_length = sum(lengths) / document_count
    if average_length == 0:
        return [0.0] * document_count

    idf = {}
    for term, term_postings in postings.items():
        document_frequency = len(term_postings)
        idf[term] = (
            0.0 if document_frequency == document_count else
            math.log1p(
                (document_count - document_frequency + 0.5)
                / (document_frequency + 0.5))
        )
    length_norms = [
        _BM25_K1 * (1 - _BM25_B + _BM25_B * length / average_length)
        for length in lengths
    ]
    best_scores = [0.0] * document_count
    for query in queries:
        query_frequencies = Counter(query)
        if not query_frequencies:
            continue
        query_scores = [0.0] * document_count
        for term, query_frequency in query_frequencies.items():
            term_idf = idf.get(term)
            if term_idf is None:
                continue
            for pos, term_frequency in postings[term]:
                query_scores[pos] += (
                    query_frequency * term_idf * term_frequency
                    * (_BM25_K1 + 1)
                    / (term_frequency + length_norms[pos])
                )
        best_scores = [
            max(best, score)
            for best, score in zip(best_scores, query_scores)
        ]
    return best_scores


def recall_candidates(
    library: TagLibrary,
    embedder,
    queries: list[str],
    *,
    dir_ids: list[str] | None = None,
    top_k: int = 50,
) -> list[RecallCandidate]:
    """融合向量与 BM25 关键词召回，RRF 排序后返回全局 top_k。

    - 范围：`dir_ids`（含子目录）；空 = 全库；
    - 库为空 / 查询为空 → 返回 []（调用方决定是否按"库未就绪"失败）；
    - 关键词语料包含标签名、描述和别名，命中别名后归一到其 tag_id；
    - 每个通道按跨 query 的最高分取 top_k，通道名次通过 RRF 融合；
    - 候选 score 保留最高余弦，供闭环终分使用。
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

    tags = library.tags()
    by_id = {tag.tag_id: tag for tag in tags}
    definitions = [by_id.get(tag.tag_id) for tag in index]
    documents = [
        _keyword_tokens(" ".join(dict.fromkeys(
            (definition.tag_name, definition.description, *definition.aliases))))
        if definition is not None else []
        for definition in definitions
    ]
    keyword_scores = _bm25_scores(
        documents, [_keyword_tokens(query) for query in nonempty_queries])
    compact_queries = [_compact_keyword(query) for query in nonempty_queries]
    exact_matches = [
        definition is not None and any(
            len(phrase) > 1 and phrase in query
            for phrase in (_compact_keyword(value) for value in
                           (definition.tag_name, *definition.aliases))
            for query in compact_queries
        )
        for definition in definitions
    ]

    vector_ranked = sorted(
        range(len(index)), key=lambda pos: (-float(best[pos]), pos))
    keyword_ranked = sorted(
        (pos for pos, score in enumerate(keyword_scores)
         if score > 0 or exact_matches[pos]),
        key=lambda pos: (not exact_matches[pos], -keyword_scores[pos], pos),
    )
    fused_scores = [0.0] * len(index)
    vector_ranks: dict[int, int] = {}
    keyword_ranks: dict[int, int] = {}
    candidate_positions: set[int] = set()
    for ranking, channel_ranks in (
        (vector_ranked, vector_ranks),
        (keyword_ranked, keyword_ranks),
    ):
        for rank, pos in enumerate(ranking[:top_k], start=1):
            candidate_positions.add(pos)
            channel_ranks[pos] = rank
            fused_scores[pos] += 1 / (_RRF_K + rank)

    fused_ranked = sorted(
        candidate_positions,
        key=lambda pos: (
            -fused_scores[pos],
            min(vector_ranks.get(pos, math.inf),
                keyword_ranks.get(pos, math.inf)),
            keyword_ranks.get(pos, math.inf),
            -float(best[pos]),
            pos,
        ),
    )
    picked: list[RecallCandidate] = []
    seen: set[str] = set()
    for pos in fused_ranked:
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
