"""库内标签对齐：生成前给"已有标签"提示，生成后给候选分档。

分档本身只标记、不删行；真正改动产出的只有一条规则——"可以替换"：
同名直接换成库内标签（名字本来就一样）；疑似必须经 LLM 复核（`verify.py`）
确认是同一概念才替换成库内标准名，原词留给将来写别名。替换开关关掉时
退回"只标记"。

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

import logging
from dataclasses import dataclass

import numpy as np
from sqlalchemy.orm import Session

from tagplatform.db.models import TagDefine
from tagplatform.library import LibraryTag, is_deprecated  # 定义在 library，避免循环导入
from tagplatform.generation.mtg.merge import normalize_tag
from tagplatform.schemas import ContentJSON
from tagplatform.vectors import decode_vector

logger = logging.getLogger(__name__)

# 标签名参与"包含关系"判断的最短长度：单字标签（如"爱"）会把半张表都算成疑似
_MIN_CONTAIN_LEN = 2

BAND_DUPLICATE = "duplicate"
BAND_SUSPECT = "suspect"
BAND_NEW = "new"

# 排序用：数值越大越优先（新词优先占名额）
BAND_RANK = {BAND_NEW: 2, BAND_SUSPECT: 1, BAND_DUPLICATE: 0}

# 导入模板里的占位行（实测库里有"按 YYYY-MM-DD 填写"这种）。它们既不该
# 出现在提示词里，也不该被当成对齐目标——否则生成的新词可能被替换成一句模板。
_PLACEHOLDER_MARKS = ("YYYY-MM-DD", "yyyy-mm-dd", "填写", "示例", "TODO")


def _is_placeholder(name: str) -> bool:
    """模板占位行识别：命中明显是"填表说明"而非标签的写法。"""
    return any(mark in name for mark in _PLACEHOLDER_MARKS)




@dataclass(frozen=True)
class Alignment:
    """一条候选的对齐结论（只标记，调用方决定怎么用）。

    ``same_concept`` 是 LLM 复核结论：True=可以合并（替换成库内标准名），
    False=不是同一概念，None=未复核（库为空 / 复核开关关掉 / 复核失败）。
    """

    band: str
    reason: str
    matched_name: str | None = None
    matched_tag_id: str | None = None
    cosine: float | None = None
    same_concept: bool | None = None

    @property
    def replaceable(self) -> bool:
        """是否该把候选替换成库内标准名。

        同名（duplicate）无需复核即可替换；疑似必须复核通过才替换——
        "像"不等于"是"，规则层的误判率实测约 38%，替换错了会把
        "非线性叙事"并进"线性叙事"。
        """
        if self.matched_tag_id is None:
            return False
        if self.band == BAND_DUPLICATE:
            return True
        return self.band == BAND_SUSPECT and self.same_concept is True


def load_library_index(session: Session, *, dim: int | None = None) -> list[LibraryTag]:
    """读取库内标签（排除废弃行、无向量行与模板占位行），供提示与分档复用。

    v2 起标签本体在 `tag_define`、目录节点在 `tag_catalog`——"叶子"这一概念
    已经由表结构保证（目录不再混在标签里），所以直接取全部标签行即可，
    不会出现"候选撞上一个分类名"。

    `dim`：查询向量的维度。库内向量来自导入时算好的那份，换句向量模型后
    必须重灌（scripts/reembed_catalog.py）；万一忘了重灌，维度对不上会让
    search_similar 抛 ValueError、整条生成任务失败。这里直接跳过维度不符的
    行：宁可这一轮没有库可比（全部按新词出），也不要让任务挂掉。
    """
    rows = [row for row in session.query(TagDefine).all()
            if not is_deprecated(row.status) and row.embedding
            and not _is_placeholder(row.tag_name)]
    index: list[LibraryTag] = []
    skipped = 0
    for row in rows:
        vector = decode_vector(row.embedding)
        if dim is not None and vector.shape[0] != dim:
            skipped += 1
            continue
        index.append(LibraryTag(tag_id=row.tag_id, name=row.tag_name,
                                vector=vector))
    if skipped:
        logger.warning("标签库有 %d 条维度不符的向量（期望 %d），已跳过——"
                       "换过句向量模型就要跑 scripts/reembed_catalog.py", skipped, dim)
    return index


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

    by_key: dict[str, list[LibraryTag]] = {}
    for tag in index:
        by_key.setdefault(normalize_tag(tag.name), []).append(tag)
    library_names = [tag.name for tag in index]
    matrix = np.stack([tag.vector for tag in index])
    queries = embedder.embed(names, role="query")

    def _pick(candidates: list[LibraryTag]) -> tuple[LibraryTag, str]:
        """同名多条时按 tag_id 取稳定的一条，并把歧义写进 reason。

        库里有 48 个名字被多条标签共用（"其他" 出现 30 次，挂在不同的父节点
        下）。不排序直接取会随查询顺序变化，同一部片子重跑可能挂到不同 tag_id。
        """
        chosen = min(candidates, key=lambda tag: tag.tag_id)
        note = "" if len(candidates) == 1 else f"（库内同名 {len(candidates)} 条，取 {chosen.tag_id}）"
        return chosen, note

    result: dict[str, Alignment] = {}
    for name, vector in zip(names, queries):
        sims = matrix @ np.asarray(vector, dtype=np.float32)
        top = int(np.argmax(sims))
        cosine = float(sims[top])
        nearest = index[top]

        exact_hits = by_key.get(normalize_tag(name))
        if exact_hits:
            exact, note = _pick(exact_hits)
            result[name] = Alignment(
                band=BAND_DUPLICATE, reason="与库内标签同名" + note,
                matched_name=exact.name, matched_tag_id=exact.tag_id,
                cosine=cosine)
            continue

        contained = _contained_library_name(name, library_names)
        if contained is not None:
            matched, note = _pick([tag for tag in index if tag.name == contained])
            result[name] = Alignment(
                band=BAND_SUSPECT, reason=f"名字包含库内标签「{contained}」" + note,
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
