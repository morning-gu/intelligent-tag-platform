"""closed 生成器：封闭式标签提取（v2 origin 词表，Task 6）。

流程（设计文档 §4.2）：按目录分组召回候选 → 空候选显式失败（提示先导入
标签库）→ LLM 判定（prompt 已由执行器组装进 spec.prompt_text，含候选
清单，producer 不再重复组装）→ 契约校验解析 → 置信度过滤 + 相关度降序
截断 → ProducedTag(origin="closed")。producer 不写库：结果落
pre_asset_tag 与重跑替换都是执行器的职责。

origin 词表 v2：closed（命中目录内既有定义，tag_id 必填、candidate_*
为 None）| open（开放生成，Task 7 域）。v1 的 "catalog" 词退役。

导入即注册：`register_producer(ClosedTagProducer())` 在模块顶层执行，
`tagplatform.generation` 包导入后 list_producers() 含 "closed"。
分层红线：本模块不得 import fastapi（设计文档 §9.4）。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from tagplatform.generation.base import (
    Evidence,
    GenerationSpec,
    ProducedTag,
    register_producer,
)
from tagplatform.generation.recall import embed_content_texts, recall_by_dir
from tagplatform.llm.parsing import parse_closed_output
from tagplatform.settings import get_settings


class ClosedTagProducer:
    """封闭式标签提取：从目录候选中挑选，origin 恒为 "closed"。"""

    mode = "closed"

    def produce(self, spec: GenerationSpec, session: Session, llm,
                embedder) -> list[ProducedTag]:
        # 1. 按目录分组召回（producer 直接读全局配置的召回上限，显式传参留待统一重构）
        candidates = recall_by_dir(
            session,
            embed_content_texts(spec.content),
            embedder,
            get_settings().recall_top_k_per_type,
        )
        # 2. 空候选属配置错误：标签库未导入，显式失败优于静默空结果
        if not candidates:
            raise ValueError("候选标签为空，请先导入标签库")
        # 3. prompt 已由执行器组装（含候选清单），此处只负责调用
        resp = llm.complete([{"role": "user", "content": spec.prompt_text}],
                            spec.run_id)
        # 4. 传输失败 → 任务失败语义由执行器落库；此处显式抛错透出原因
        if not resp.ok:
            raise RuntimeError(f"LLM 调用失败: {resp.raw.get('error')}")
        items = parse_closed_output(resp.content)
        # 5. 置信度门槛过滤 + 相关度降序截断
        kept = [it for it in items if it["confidence"] >= spec.policy.min_confidence]
        kept.sort(key=lambda it: -it["relevance"])
        kept = kept[: spec.policy.max_tags]
        # 6. 映射为审核前中间态（closed 来源：tag_id 必有，candidate_* 为 None）
        return [
            ProducedTag(
                origin="closed",
                tag_id=it["tag_id"],
                tag_name=it["tag_name"],
                candidate_type=None,
                candidate_description=None,
                relevance_score=it["relevance"],
                confidence=it["confidence"],
                evidence=[Evidence(**e) for e in it["evidence"]],
            )
            for it in kept
        ]


register_producer(ClosedTagProducer())
