"""模式 B 生成器：开放式标签生成（自由生成 + 归一匹配）（Task 6）。

流程（设计文档 §5.2）：LLM 自由生成候选（prompt 已由任务 8 执行器组装进
spec.prompt_text，producer 不组装）→ parse_open_output 契约解析（LLM 产出
的多余键含 tag_id 静默丢弃——tag_id 只能由匹配层赋值）→ 全部候选一次批量
嵌入（f"{tag_name}。{description}"），逐个与 tag_catalog 有效行（排除
deprecate 与无向量）search_similar 取最高分：最高分 >= open_match_threshold
（含等于）→ origin="catalog" + tag_id + relevance_score=相似度；否则
origin="generated"（tag_id=None、candidate_* 填充、relevance_score=None）→
min_confidence 过滤 + confidence 降序截断 max_tags。

与模式 A 相反，空目录不报错：开放式生成不依赖既有标签库，全部候选落
generated 待审核，新标签只有审核通过/合并后才进正式标签库。producer 不写
库：结果落库是执行器（Task 8）的职责。
导入即注册：`register_producer(OpenTagProducer())` 在模块顶层执行，
`tagplatform.generation` 包导入后 list_producers() 含 "open"。
分层红线：本模块不得 import fastapi（设计文档 §9.4）。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from tagplatform.db.models import TagCatalog
from tagplatform.generation.base import (
    Evidence,
    GenerationSpec,
    ProducedTag,
    register_producer,
)
from tagplatform.llm.parsing import parse_open_output
from tagplatform.settings import get_settings
from tagplatform.vectors import decode_vector, search_similar


class OpenTagProducer:
    """开放式标签生成：LLM 自由候选 + 目录归一匹配，origin 按匹配结果分流。"""

    mode = "open"

    def produce(self, spec: GenerationSpec, session: Session, llm,
                embedder) -> list[ProducedTag]:
        # 1. prompt 已由执行器组装，此处只负责调用；传输失败显式抛错（同模式 A）
        resp = llm.complete([{"role": "user", "content": spec.prompt_text}],
                            spec.run_id)
        if not resp.ok:
            raise RuntimeError(f"LLM 调用失败: {resp.raw.get('error')}")
        items = parse_open_output(resp.content)
        # 2. 全部候选一次批量嵌入（embedder 契约：返回逐行对应的 (n, d) 矩阵）
        vecs = embedder.embed(
            [f"{it['tag_name']}。{it['description']}" for it in items])
        # 3. 有效目录行一次取全（排除 deprecate 与无向量行，与召回口径一致）
        candidates = [
            (row.tag_id, decode_vector(row.embedding))
            for row in session.query(TagCatalog).all()
            if row.status != "deprecate" and row.embedding
        ]
        threshold = get_settings().open_match_threshold
        produced: list[ProducedTag] = []
        for item, vec in zip(items, vecs):
            # 4. 逐候选取最高分命中；命中且 >= 阈值（含等于）→ catalog
            hits = search_similar(vec, candidates, top_k=1)
            if hits and hits[0][1] >= threshold:
                produced.append(ProducedTag(
                    origin="catalog", tag_id=hits[0][0],
                    tag_name=item["tag_name"],
                    candidate_type=None, candidate_description=None,
                    relevance_score=hits[0][1], confidence=item["confidence"],
                    evidence=[Evidence(**e) for e in item["evidence"]],
                ))
            else:
                # 5. 未命中/低于阈值 → generated 新标签候选（relevance_score=None）
                produced.append(ProducedTag(
                    origin="generated", tag_id=None,
                    tag_name=item["tag_name"],
                    candidate_type=item["tag_type"],
                    candidate_description=item["description"],
                    relevance_score=None, confidence=item["confidence"],
                    evidence=[Evidence(**e) for e in item["evidence"]],
                ))
        # 6. 置信度门槛过滤 + confidence 降序截断
        kept = [t for t in produced if t.confidence >= spec.policy.min_confidence]
        kept.sort(key=lambda t: -t.confidence)
        return kept[: spec.policy.max_tags]


register_producer(OpenTagProducer())
