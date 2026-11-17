"""模式 A 生成器：封闭式标签提取（Task 5）。

流程（设计文档 §4.2）：按目录范围召回候选 → 空候选显式失败（提示先导入
标签库）→ 候选**按目录分组**、每组以 JSON 追加到渲染后的 prompt 正文之后
（组含目录名称/描述，标签含 id/名称/描述）→ 逐组调用 LLM 判定 → 契约校验
解析 → 组内置信度过滤 + 相关度降序截断（max_tags 为单目录上限）→
合并各组结果 → ProducedTag(origin=
"catalog")。producer 不写库：结果落库是执行器（Task 8）的职责。

v2 增量（2026-11）：
- 候选来自**标签库 JSON**（`spec.library`），范围由 policy.dir_ids 限定；
- 每条候选带 `dir_id` 与挂载点（目录 applicable_scope → content/scene/
  character），角色级解析不出唯一角色时丢弃该条（宁可丢，不挂错）。

v3 增量（2026-12）：候选不再由 runner 经 `{candidates}` 占位符注入
prompt_text，改由本 producer 按目录分组、以 JSON 格式追加到正文末尾，逐组
调用 LLM 后合并结果（分组规则：目录下有召回标签才建组）。

导入即注册：`register_producer(ClosedTagProducer())` 在模块顶层执行。
分层红线：本模块不得 import fastapi（设计文档 §9.4）。
"""

from __future__ import annotations

import json

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
        # 3. 候选按目录分组（目录下有召回标签才建组；dict 插入序 = 召回序）
        groups: dict[str, list] = {}
        for c in candidates:
            groups.setdefault(c.dir_id, []).append(c)
        # 4. 逐组调用 LLM：组对象以 JSON 追加到渲染后的 prompt 正文之后，
        #    各组结果合并作为产出（run_id 沿用任务级 id，Mock 哈希采样键不变）
        items: list[dict] = []
        for dir_id, members in groups.items():
            directory = library.directory(dir_id)
            group_payload = {
                "dir_id": dir_id,
                "dir_name": directory.dir_name if directory else "未分类",
                "dir_description": directory.description if directory else "",
                "tags": [
                    {
                        "tag_id": c.tag_id,
                        "tag_name": c.tag_name,
                        "description": c.description,
                    }
                    for c in members
                ],
            }
            prompt = (
                spec.prompt_text
                + "\n\n## 候选标签分组\n"
                + json.dumps(group_payload, ensure_ascii=False, indent=2)
            )
            resp = llm.complete([{"role": "user", "content": prompt}],
                                spec.run_id)
            # 传输失败 → 任务失败语义由执行器落库；此处显式抛错透出原因
            if not resp.ok:
                raise RuntimeError(f"LLM 调用失败: {resp.raw.get('error')}")
            print(f"ClosedTagProducer: raw response for dir_id={dir_id}:\n{resp.content}")
            # 5. 组内置信度门槛过滤 + 相关度降序截断（max_tags 为单目录
            #    上限；2026/11/17 由合并后全集改为组内执行，目录间互不挤占）
            kept = [it for it in parse_closed_output(resp.content) if it["confidence"] >= spec.policy.min_confidence]
            kept.sort(key=lambda it: -it["relevance"])
            kept = kept[: spec.policy.max_tags]
            items.extend(kept)
        # 6. 映射为审核前中间态；catalog 来源 tag_id 必有，候选目录直接取库
        produced: list[ProducedTag] = []
        for it in items:
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
