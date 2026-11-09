"""模式 A 生成器：封闭式标签提取（Task 5）。

流程（设计文档 §4.2）：按目录范围召回候选 → 空候选显式失败（提示先导入
标签库）→ LLM 判定（prompt 已由执行器组装进 spec.prompt_text，含候选清单）
→ 契约校验解析 → 置信度过滤 + 相关度降序截断 → ProducedTag(origin="catalog")。
producer 不写库：结果落库是执行器（Task 8）的职责。

v2 增量（2026-11）：
- 候选来自**标签库 JSON**（`spec.library`），范围由 policy.dir_ids 限定；
- 每条候选带 `dir_id` 与挂载点（目录 applicable_scope → content/scene/
  character），角色级解析不出唯一角色时丢弃该条（宁可丢，不挂错）。

导入即注册：`register_producer(ClosedTagProducer())` 在模块顶层执行。
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
from tagplatform.generation.recall import embed_content_texts, recall_candidates
from tagplatform.llm.parsing import parse_closed_output
from tagplatform.mounting import resolve_mount
from tagplatform.settings import get_settings


class ClosedTagProducer:
    """封闭式标签提取：从目录候选中挑选，origin 恒为 "catalog"。"""

    mode = "closed"

    def produce(self, spec: GenerationSpec, session: Session, llm,
                embedder) -> list[ProducedTag]:
        library = spec.library
        if library is None:
            raise ValueError("标签库未就绪，无法执行封闭式生成")
        # 1. 按目录范围分组召回（候选带目录与挂载粒度）
        candidates = recall_candidates(
            library,
            embedder,
            embed_content_texts(spec.content),
            dir_ids=spec.policy.dir_ids,
            top_k=get_settings().recall_top_k_per_type,
        )
        # 2. 空候选属配置错误：目录库未导入，显式失败优于静默空结果
        if not candidates:
            raise ValueError("候选标签为空，请先导入标签库")
        by_id = {c.tag_id: c for c in candidates}
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
        # 6. 映射为审核前中间态；catalog 来源 tag_id 必有，候选目录直接取库
        produced: list[ProducedTag] = []
        for it in kept:
            candidate = by_id.get(it["tag_id"])
            if candidate is None:
                continue  # 模型编了不存在的 tag_id：契约外，丢弃
            evidence = [Evidence(**e) for e in it["evidence"]]
            mount = resolve_mount(candidate.scope, spec.asset_id,
                                  spec.content, evidence)
            if mount is None:
                continue  # 角色级解析不出唯一角色，宁可丢
            produced.append(ProducedTag(
                origin="catalog",
                tag_id=candidate.tag_id,
                tag_name=candidate.tag_name,
                candidate_type=None,
                candidate_description=None,
                relevance_score=it["relevance"],
                confidence=it["confidence"],
                evidence=evidence,
                dir_id=candidate.dir_id,
                target_type=mount.target_type,
                target_id=mount.target_id,
            ))
        return produced


register_producer(ClosedTagProducer())
