"""模式 A 生成器：封闭式标签提取（Task 5）。

流程（设计文档 §4.2）：按目录范围召回候选 → 剔除 dir_asset 目录的候选
（该类目录标签由元数据导入/联网搜索获取，不做内容语义打分）→ 空候选
显式失败（提示先导入标签库）→ 候选**按目录分组**、每组以 JSON 追加到
渲染后的 prompt 正文之后（组含目录名称/描述，标签含 id/名称/描述）→ 逐组调用 LLM 判定 → 契约校验
解析 → 终分 Sf = G·(a·Semb + b·Sapp + c·Sevi)（a/b/c = 0.2/0.5/0.3；
Semb=召回余弦、Sapp/Sevi 由 1–5 分线性映射到 0–1；G 为 Sapp≥0.5 门控，
A≤2 整条丢弃）→ 门内按 Sf 降序截断（max_tags 为单目录上限）→
合并各组结果 → ProducedTag(origin=
"catalog")。producer 不写库：结果落库是执行器（Task 8）的职责。

v2 增量（2026-11）：
- 候选来自**标签库 JSON**（`spec.library`），范围由 policy.dir_ids 限定；
- 每条候选带 `dir_id` 与挂载点（目录 applicable_scope → content/scene/
  character），角色级解析不出唯一角色时丢弃该条（宁可丢，不挂错）。

v3 增量（2026-12）：候选不再由 runner 经 `{candidates}` 占位符注入
prompt_text，改由本 producer 按目录分组、以 JSON 格式追加到正文末尾，逐组
调用 LLM 后合并结果（分组规则：目录下有召回标签才建组）。

v4 增量（2026-12）：LLM 判定改带 1–5 分的 applicability/evidence_strength；
终分 Sf = G·(0.2·Semb + 0.5·Sapp + 0.3·Sevi)，Semb 取召回候选自带的余弦
分，Sf 与标签成对（落 ProducedTag.relevance_score）；confidence 仍为
(E−1)/4，作为证据维度的独立读数随行保留。

v5 增量（2026-12）：组内候选再按 `closed_batch_size`（默认 1）分批送
LLM（每批一次调用，防单组标签过多导致输出截断）；组内全部批次完成后按
tag_id 去重合并（首次出现优先），再统一走门控、终分与 max_tags 截断——
批只影响调用切分，不改变组级语义。

v6 增量（2026-12）：dir_asset 目录（元数据导入/联网搜索来源）的标签跳过
内容语义打分——召回后按目录逐个过滤（dir_type 不沿子目录继承，与
allows_new_tags 同口径）；范围内仅剩 dir_asset 目录时按空候选显式失败。

导入即注册：`register_producer(ClosedTagProducer())` 在模块顶层执行。
分层红线：本模块不得 import fastapi（设计文档 §9.4）。
"""

from __future__ import annotations

import json
import logging

from sqlalchemy.orm import Session

from tagplatform.generation.base import (
    Evidence,
    GenerationSpec,
    ProducedTag,
    register_producer,
)
from tagplatform.generation.recall import embed_content_texts, recall_candidates
from tagplatform.library import DIR_ASSET
from tagplatform.llm.parsing import parse_closed_output
from tagplatform.mounting import resolve_mount
from tagplatform.settings import get_settings

logger = logging.getLogger(__name__)


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
        # 2. dir_asset 目录（元数据/联网搜索来源）不做内容语义打分：剔除其
        #    候选后再判空（dir_type 逐目录判定，不沿子目录继承）
        asset_dir_ids = {d.dir_id for d in library.dirs()
                         if d.dir_type == DIR_ASSET}
        candidates = [c for c in candidates if c.dir_id not in asset_dir_ids]
        # 3. 空候选属配置错误：目录库未导入，显式失败优于静默空结果
        if not candidates:
            raise ValueError("候选标签为空，请先导入标签库")
        by_id = {c.tag_id: c for c in candidates}
        # 4. 候选按目录分组（目录下有召回标签才建组；dict 插入序 = 召回序）
        groups: dict[str, list] = {}
        for c in candidates:
            groups.setdefault(c.dir_id, []).append(c)
        # 5. 逐组调用 LLM：组对象以 JSON 追加到渲染后的 prompt 正文之后；
        #    单组标签过多易截断模型输出，组内再按 closed_batch_size 分批，
        #    每批送同构组对象（目录信息相同、tags 为该批子集，批次按召回
        #    序连续切片）。组内全部批次完成后合并判定（tag_id 去重，首次
        #    出现优先——防真实模型跨批重复判定），再统一门控截断。
        #    run_id 沿用任务级 id（Mock 哈希采样键不变）
        items: list[tuple[float, dict]] = []
        batch_size = max(1, get_settings().closed_batch_size)
        for dir_id, members in groups.items():
            directory = library.directory(dir_id)
            group_parsed: list[dict] = []
            seen: set[str] = set()
            for start in range(0, len(members), batch_size):
                batch = members[start:start + batch_size]
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
                        for c in batch
                    ],
                }
                prompt = (
                    spec.prompt_text
                    + "\n\n## 候选标签分组\n"
                    + json.dumps(group_payload, ensure_ascii=False, indent=2)
                )
                print(f"ClosedTagProducer: prompt for dir {dir_id}, batch {start}-{start + len(batch)}:\n{prompt}")
                resp = llm.complete([{"role": "user", "content": prompt}],
                                    spec.run_id)
                # 传输失败 → 任务失败语义由执行器落库；此处显式抛错透出原因
                if not resp.ok:
                    raise RuntimeError(f"LLM 调用失败: {resp.raw.get('error')}")
                print(f"ClosedTagProducer: response for dir {dir_id}, batch {start}-{start + len(batch)}:\n{resp.content}")
                for it in parse_closed_output(resp.content):
                    if it["tag_id"] in seen:
                        continue  # 跨批重复判定：首次出现优先
                    seen.add(it["tag_id"])
                    group_parsed.append(it)
            logger.debug("闭环目录 %s 解析出 %d 条判定", dir_id,
                         len(group_parsed))
            # 6. 终分与门控（a/b/c = 0.2/0.5/0.3）：Sapp/Sevi 由 1–5 分
            #    线性映射到 0–1（(X−1)/4），Semb 取召回候选自带的余弦分；
            #    G 为 Sapp≥0.5 的门控（A≥3 放行，A≤2 → Sf=0 整条丢弃）。
            #    Sf 与标签成对保留，按 Sf 降序截断（max_tags 为单目录上限）
            scored: list[tuple[float, dict]] = []
            for it in group_parsed:
                if it["applicability"] < 3:
                    continue  # Sapp=(A−1)/4 < 0.5：门控关闭，G=0 → Sf=0
                candidate = by_id.get(it["tag_id"])
                s_emb = candidate.score if candidate is not None else 0.0
                s_app = (it["applicability"] - 1) / 4
                s_evi = (it["evidence_strength"] - 1) / 4
                scored.append((0.2 * s_emb + 0.5 * s_app + 0.3 * s_evi, it))
            scored.sort(key=lambda pair: -pair[0])
            items.extend(scored[: spec.policy.max_tags])
        # 7. 映射为审核前中间态：Sf 成对绑定到标签（relevance_score）；
        #    catalog 来源 tag_id 必有，候选目录直接取库
        produced: list[ProducedTag] = []
        scenes_by_id = {scene.scene_id: scene for scene in spec.content.content.scenes}
        for sf, it in items:
            candidate = by_id.get(it["tag_id"])
            if candidate is None:
                continue  # 模型编了不存在的 tag_id：契约外，丢弃
            evidence = []
            for item in it["evidences"]:
                scene = scenes_by_id.get(item["scene_id"])
                if scene is None:
                    logger.warning(
                        "闭环标签 %s 的证据引用未知场景 %s，已忽略该证据",
                        it["tag_id"], item["scene_id"])
                    continue
                evidence.append(Evidence(
                    scene_id=scene.scene_id,
                    start_ms=scene.time_range.start_ms,
                    end_ms=scene.time_range.end_ms,
                    description=item["evidence"],
                ))
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
                relevance_score=sf,
                confidence=(it["evidence_strength"] - 1) / 4,
                evidence=evidence,
                dir_id=candidate.dir_id,
                target_type=mount.target_type,
                target_id=mount.target_id,
            ))
        return produced


register_producer(ClosedTagProducer())
