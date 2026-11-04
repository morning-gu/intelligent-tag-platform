"""库内标签对齐：生成前给"已有标签"提示，生成后给候选分档（只标记，不删除）。

与旧 `pipeline._catalog_hits` 的区别：
- 旧：截断**之后**拿候选名去库取 top-1，≥ `open_match_threshold`(0.85) 就把
  整条改成 origin=catalog；
- 新：截断**之前**给全部幸存候选分档，并把"同名"作为唯一的确定重复信号。
  库装满后 0.85 会把几乎所有候选判成库内标签——实测候选对库的随机配对均值
  就有 0.808，top-1 全部 ≥ 0.836。

分档口径（2026-11 用 8101 库 1308 个叶子标签 + 87 个真实候选标定）：
- ``duplicate``：名字归一后完全相同；
- ``suspect``：库内某个名字（≥2 字）被候选包含、或与最像那条的字符重合
  ≥ ``name_overlap``、或与最像那条的余弦 ≥ ``suspect_cosine``
  （0.87 ≈ 候选对库随机配对的 99.9% 分位 0.866）；
- ``new``：其余。

余弦只在"候选（query）× 库标签（passage）"之间比较。库内两条标签互比的
分数（均值 0.896）不能当基线：角色前缀不同，两者不是一条分布。

库内向量直接用导入时算好并落在 ``tag_catalog.embedding`` 的那份——再嵌一遍
1400 条要几分钟，且会与库里已有向量口径不一致。

分层红线：本模块不得 import fastapi（设计文档 §9.4）。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from sqlalchemy.orm import Session

from tagplatform.db.models import TagCatalog
from tagplatform.generation.mtg.merge import normalize_tag
from tagplatform.schemas import ContentJSON
from tagplatform.vectors import decode_vector

# 标签名参与"包含关系"判断的最短长度：单字标签（如"爱"）会把半张表都算成疑似
_MIN_CONTAIN_LEN = 2

BAND_DUPLICATE = "duplicate"
BAND_SUSPECT = "suspect"
BAND_NEW = "new"

# 排序用：数值越大越优先（新词优先占名额）
BAND_RANK = {BAND_NEW: 2, BAND_SUSPECT: 1, BAND_DUPLICATE: 0}


@dataclass(frozen=True)
class LibraryTag:
    """库内一枚叶子标签：名字 + 主键 + 存库向量。"""

    tag_id: str
    name: str
    vector: np.ndarray


@dataclass(frozen=True)
class Alignment:
    """一条候选的对齐结论（只标记，调用方决定怎么用）。"""

    band: str
    reason: str
    matched_name: str | None = None
    matched_tag_id: str | None = None
    cosine: float | None = None


def load_library_index(session: Session) -> list[LibraryTag]:
    """读取库内叶子标签（排除废弃行与无向量行），供提示与分档复用。

    叶子 = 没有任何行把它当父节点——目录节点（"内容元数据标签"这类）不参与
    对齐，否则候选可能"撞上一个分类名"。
    """
    rows = [row for row in session.query(TagCatalog).all()
            if row.status != "deprecate" and row.embedding]
    # 父节点集合按全表算（含废弃行）：否则"父节点已废弃"的子节点会被误当叶子
    all_parent_ids = {row.parent_id for row in session.query(TagCatalog).all()
                      if row.parent_id}
    return [
        LibraryTag(tag_id=row.tag_id, name=row.tag_name,
                   vector=decode_vector(row.embedding))
        for row in rows
        if row.tag_id not in all_parent_ids
    ]


def _content_texts(content: ContentJSON) -> list[str]:
    """内容 → 查询文本（剧情摘要 + 各场景描述），与闭环召回同口径。"""
    return [content.content.summary,
            *(scene.description for scene in content.content.scenes)]


def recall_library_hints(index: list[LibraryTag], embedder, content: ContentJSON,
                         top_n: int) -> list[str]:
    """内容 → 库里最相关的 top_n 个标签名（只给名字，按相关度降序）。"""
    if not index or top_n <= 0:
        return []
    texts = [text for text in _content_texts(content) if text]
    if not texts:
        return []
    queries = embedder.embed(texts, role="query")
    matrix = np.stack([tag.vector for tag in index])
    best = (matrix @ np.asarray(queries, dtype=np.float32).T).max(axis=1)
    order = np.argsort(-best, kind="stable")[:top_n]
    return [index[int(i)].name for i in order]


def _char_overlap(left: str, right: str) -> float:
    """字符集合的 Jaccard 重合度（短标签上比编辑距离稳）。"""
    a, b = set(left), set(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _contained_library_name(name: str, names: list[str]) -> str | None:
    """库内名字被候选包含时返回最长的那个（"复仇"优先于"仇"）。"""
    hits = [lib for lib in names
            if len(lib) >= _MIN_CONTAIN_LEN and lib in name and lib != name]
    return max(hits, key=len) if hits else None


def align_candidates(index: list[LibraryTag], embedder, names: list[str], *,
                     suspect_cosine: float, name_overlap: float,
                     ) -> dict[str, Alignment]:
    """候选名 → 分档结论。库为空或无候选时返回空字典。

    判定顺序：同名 → 名字包含 → 字符重合 → 余弦 → 新词。先命中先算数，
    ``reason`` 记录是哪一条规则命中的，便于事后复查规则是否过松。
    """
    if not index or not names:
        return {}

    by_key = {normalize_tag(tag.name): tag for tag in index}
    library_names = [tag.name for tag in index]
    matrix = np.stack([tag.vector for tag in index])
    queries = embedder.embed(names, role="query")

    result: dict[str, Alignment] = {}
    for name, vector in zip(names, queries):
        sims = matrix @ np.asarray(vector, dtype=np.float32)
        top = int(np.argmax(sims))
        cosine = float(sims[top])
        nearest = index[top]

        exact = by_key.get(normalize_tag(name))
        if exact is not None:
            result[name] = Alignment(
                band=BAND_DUPLICATE, reason="与库内标签同名",
                matched_name=exact.name, matched_tag_id=exact.tag_id,
                cosine=cosine)
            continue

        contained = _contained_library_name(name, library_names)
        if contained is not None:
            matched = next(tag for tag in index if tag.name == contained)
            result[name] = Alignment(
                band=BAND_SUSPECT, reason=f"名字包含库内标签「{contained}」",
                matched_name=contained, matched_tag_id=matched.tag_id,
                cosine=cosine)
            continue

        overlap = _char_overlap(name, nearest.name)
        if overlap >= name_overlap:
            result[name] = Alignment(
                band=BAND_SUSPECT,
                reason=f"与「{nearest.name}」字符重合 {overlap:.2f}",
                matched_name=nearest.name, matched_tag_id=nearest.tag_id,
                cosine=cosine)
            continue

        if cosine >= suspect_cosine:
            result[name] = Alignment(
                band=BAND_SUSPECT,
                reason=f"与「{nearest.name}」相似度 {cosine:.2f}",
                matched_name=nearest.name, matched_tag_id=nearest.tag_id,
                cosine=cosine)
            continue

        result[name] = Alignment(
            band=BAND_NEW, reason="库内无相近标签",
            matched_name=nearest.name, matched_tag_id=None, cosine=cosine)
    return result


def summarize_bands(alignments: dict[str, Alignment]) -> dict[str, int]:
    """分档计数（写审计报告用）。"""
    counts = {BAND_DUPLICATE: 0, BAND_SUSPECT: 0, BAND_NEW: 0}
    for item in alignments.values():
        counts[item.band] = counts.get(item.band, 0) + 1
    return counts
