"""模式 A 生成器：封闭式标签提取（Task 5）。

流程（设计文档 §4.2）：按目录范围召回候选 → 剔除 dir_asset 目录的候选
（该类目录标签由元数据导入/联网搜索获取，不做内容语义打分）→ 空候选
显式失败（提示先导入标签库）→ 候选**按目录分组**、每组以 JSON 追加到
渲染后的 prompt 正文之后（组含目录名称/描述，标签含 id/名称/描述）→
逐组调用 LLM 判定（组内按 closed_batch_size 分批，防单组标签过多导致
输出截断；解析失败追加格式纠正提示重调本批一次，仍失败抛错落任务失败）
→ 契约校验解析 → 终分 Sf = G·(a·Semb + b·Sapp + c·Sevi)（a/b/c =
0.2/0.5/0.3；Semb=召回余弦、Sapp/Sevi 由 1–5 分线性映射到 0–1，Sevi 为
content/scene/frame 三模态加权融合；G 为 Sapp≥0.75 门控，A≤3 整条
丢弃）→ 挂载解析前置（证据展开 + 目录粒度 → 挂载点，解析不出的判定
在选择前丢弃、不占名额）→ 全局选择（max_tags = 全局输出上限 M：每组
Sf 最高 1 条保底，保底间按 Sf 竞争截断，剩余按 Sf 降序补足 M−N）→
映射 ProducedTag(origin="catalog")。producer 不写库：结果落库是执行器
（Task 8）的职责。

v2 增量（2026-11）：
- 候选来自**标签库 JSON**（`spec.library`），范围由 policy.dir_ids 限定；
- 每条候选带 `dir_id` 与挂载点（目录 applicable_scope → content/scene/
  character），角色级解析不出唯一角色时丢弃该条（宁可丢，不挂错）。

v3 增量（2026-12）：候选不再由 runner 经 `{candidates}` 占位符注入
prompt_text，改由本 producer 按目录分组、以 JSON 格式追加到正文末尾，逐组
调用 LLM 后合并结果（分组规则：目录下有召回标签才建组）。

v4 增量（2026-12）：LLM 判定改带 1–5 分的 applicability/evidence_strength；
终分 Sf = G·(0.2·Semb + 0.5·Sapp + 0.3·Sevi)，Semb 取召回候选自带的余弦
分，Sf 与标签成对（落 ProducedTag.relevance_score）；confidence 保留为
证据维度的独立读数随行保存（v8 起为三模态融合 Sevi）。

v5 增量（2026-12）：组内候选再按 `closed_batch_size`（默认 1）分批送
LLM（每批一次调用，防单组标签过多导致输出截断）；同批解析失败追加
格式纠正提示重调一次（详见 v7）。门控收紧为 Sapp≥0.75（applicability≥4，
A=3 起整条丢弃）；max_tags 语义从单目录上限改为**全局输出上限 M**——
每组 Sf 最高 1 条保底，组数超 M 时保底间按 Sf 竞争截断，剩余判定按 Sf
降序补足；编造 tag_id 的过滤前置到打分前（不再占走组内保底名额后才被
剔除）；重复判定按 tag_id 去重保留 Sf 最高分（天然涵盖跨批重复）。

v6 增量（2026-12）：dir_asset 目录（元数据导入/联网搜索来源）的标签跳过
内容语义打分——召回后按目录逐个过滤（dir_type 不沿子目录继承，与
allows_new_tags 同口径）；范围内仅剩 dir_asset 目录时按空候选显式失败。
挂载解析前置到全局选择**之前**——解析不出挂载点的判定在选择阶段即淘汰、
不占 M 名额（修复"选满 30 只出 28"：角色目录标签在多角色内容上挂载必败，
原先在第 6 步后置丢弃且不回填，缺额不补）；门控丢弃、挂载丢弃、编造 id
丢弃三处补 warning 日志，选择完成打一条 INFO 汇总（组头/补足/各类丢弃
计数）。

v7 增量（2026-12）：组级解析重试加固——真实端点偶发返回缺键元素（合法
JSON 非截断），原先一次坏输出就让整任务作废且零线索。现在每批解析失败
先记 warning（含 dir_id 与原始输出前 400 字），追加格式纠正提示重调本
批一次，仍失败才抛 LLMParseError；传输失败不在此层重试（client 层已有
退避）。严格语义不宽容降级：与 mtg/judge 的解析契约同口径。

v8 增量（2026-12 多模态融合评分）：Sevi 从单一 evidence_strength 改为
三模态加权融合——content（剧情描述）/ scene（场景描述）/ frame（关键
帧）各按 1–5 打分，融合分 Sevi = Σ w_m×(S_m−1)/4（权重
`closed_evi_weights`，默认 0.3/0.3/0.4，本地计算不依赖模型算术）。LLM
输出契约为嵌套 evidence 对象（三模态节各含 evidence_strength + evidences
数组）。有帧可用时（`movie_asset_root` 下按 asset_id 反查）每场景取头/
中/尾三帧，消息改多模态 parts（文本 + image_url），帧跨批复用；无帧
（老资产/测试环境）自动退化为纯文本消息。三模态 evidences 条目展平成
统一 Evidence 列表（scene_id 校验保留），confidence 改为融合 Sevi。

导入即注册：`register_producer(ClosedTagProducer())` 在模块顶层执行。
分层红线：本模块不得 import fastapi（设计文档 §9.4）。
"""

from __future__ import annotations

import json
import logging

from sqlalchemy.orm import Session

from tagplatform.capabilities.prompts.templates import CLOSED_RETRY_HINT
from tagplatform.services.generation.base import (
    Evidence,
    GenerationSpec,
    ProducedTag,
    register_producer,
)
from tagplatform.services.generation.trace import ProduceTrace, TraceCandidate
from tagplatform.services.generation.open.mtg.keyframes import _local_roots
from tagplatform.services.generation.open.mtg.prompts import encode_image, keyframe_parts
from tagplatform.services.generation.closed.recall import embed_content_texts, recall_candidates
from tagplatform.services.tags.library import DIR_ASSET
from tagplatform.services.generation.closed.parse import LLMParseError, parse_closed_output
from tagplatform.services.tags.mounting import Mount, resolve_mount
from tagplatform.services.content.movie_import import resolve_scene_frames
from tagplatform.settings import get_settings

logger = logging.getLogger(__name__)

# 解析重试的格式纠正提示自文案集中起移驻
# tagplatform.capabilities.prompts.templates.CLOSED_RETRY_HINT（本模块 import 消费）。


def _pick_head_mid_tail(frames: tuple) -> list:
    """场景帧序列 → 头/中/尾三帧（不足三张时去重收缩，保持顺序）。"""
    if not frames:
        return []
    picked = [frames[0], frames[len(frames) // 2], frames[-1]]
    seen: list = []
    for frame in picked:
        if frame not in seen:
            seen.append(frame)
    return seen


def _resolve_frame_parts(spec: GenerationSpec) -> list[dict]:
    """按 asset_id 反查帧目录，为每个场景构建头/中/尾关键帧消息 parts。

    帧定位失败（根目录不存在/资产不在根下）不是错误：返回空列表，
    消息组装退化为纯文本。每帧 parts 复用 mtg 的 keyframe_parts 格式
    （〔关键帧〕场景 ID 第 N/M 张 + base64 image_url）。
    """
    settings = get_settings()
    roots = _local_roots(settings.movie_asset_root,
                         settings.movie_asset_extra_roots)
    parts: list[dict] = []
    found_scenes = 0
    for scene in spec.content.content.scenes:
        try:
            frames = resolve_scene_frames(
                roots, scene.scene_id, asset_id=spec.asset_id)
        except OSError as exc:
            logger.warning("场景 %s 帧定位失败：%s", scene.scene_id, exc)
            continue
        if not frames:
            continue
        found_scenes += 1
        picked = _pick_head_mid_tail(frames)
        for order, frame in enumerate(picked, start=1):
            try:
                encoded = encode_image(frame)
            except (OSError, ValueError) as exc:
                logger.warning("场景 %s 帧 %s 编码失败，跳过：%s",
                               scene.scene_id, frame.name, exc)
                continue
            parts.extend(keyframe_parts(
                scene.scene_id, order, len(picked), encoded))
    if parts:
        logger.info(
            "闭环多模态输入就绪：%d 个场景命中关键帧，共 %d 张"
            "（头/中/尾）", found_scenes, len(parts) // 2)
    return parts


class ClosedTagProducer:
    """封闭式标签提取：从目录候选中挑选，origin 恒为 "catalog"。"""

    mode = "closed"
    # 协议声明（Task 4）：本模式不自拼 prompt——runner 装配统一渲染本地
    # 模板常量（capabilities/prompts/templates.py CLOSED_TEMPLATE_TEXT）
    builds_prompt = False
    # 任务口径留痕（generation_task.prompt_version，TEXT 列字符串）：沿用
    # DB 版本机制时代的 v1 值——模板即 v1 就地演进（git 即版本）
    prompt_version = "1"

    def produce(self, spec: GenerationSpec, session: Session, llm,
                embedder, trace: ProduceTrace | None = None,
                ) -> list[ProducedTag]:
        """按 spec 生成候选标签列表。

        trace 非空时在各既有流程点顺路上报漏斗踪迹（纯旁观，不参与任何
        判定）；None 时与无参调用逐字节同行为（生产路径零行为变化红线）。
        """
        library = spec.library
        if library is None:
            raise ValueError("标签库未就绪，无法执行封闭式生成")
        # 1. 混合召回全局 top-k，再按目录范围过滤（候选带目录与挂载粒度）
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
        if trace is not None:
            # 采集点 1：全局输出上限 M（策略原值；选择阶段的钳负是兜底，
            # 追踪按策略原值记录）
            trace.set_max_tags(spec.policy.max_tags)
        # 4. 候选按目录分组（目录下有召回标签才建组；dict 插入序 = 召回序）
        groups: dict[str, list] = {}
        for c in candidates:
            groups.setdefault(c.dir_id, []).append(c)
        # 4.5 关键帧装配（v8）：produce 开头一次性解析全部场景帧并编码成
        #     消息 parts，跨目录组/批次复用（每批全量重发）。无帧（老资产
        #     或帧根未配置）退化为纯文本消息——与既有行为兼容。
        frame_parts = _resolve_frame_parts(spec)
        # 5. 逐组调用 LLM：组对象以 JSON 追加到渲染后的 prompt 正文之后；
        #    单组标签过多易截断模型输出，组内再按 closed_batch_size 分批，
        #    每批送同构组对象（目录信息相同、tags 为该批子集，批次按召回
        #    序连续切片）。每批解析失败追加格式纠正提示重调本批一次（v7），
        #    仍失败抛错落任务失败；传输失败不重试（client 层已有退避）。
        #    判定按 tag_id 去重保留 Sf 最高分（天然涵盖跨批重复——真实
        #    模型可能跨批重复判定同一标签）；组内不截断，全局选择统一在
        #    5.5 步进行。run_id 沿用任务级 id（Mock 哈希采样键不变）。
        #    丢弃计数贯穿全流程：门控（A≤3）/编造 id/挂载失败三类
        items: list[tuple[float, dict]] = []
        dropped_gate = 0
        dropped_fabricated = 0
        batch_size = max(1, get_settings().closed_batch_size)
        # 三模态权重（v8）：Sevi = Σ w_m×(S_m−1)/4，本地计算融合分
        w_content, w_scene, w_frame = get_settings().closed_evi_weight_values
        for dir_id, members in groups.items():
            directory = library.directory(dir_id)
            member_ids = {c.tag_id for c in members}
            scored_by_tag: dict[str, tuple[float, dict]] = {}
            for start in range(0, len(members), batch_size):
                batch = members[start:start + batch_size]
                if trace is not None:
                    # 采集点 2：本批候选入册（batch 序 = 切片起点 ÷ 批大小，
                    # recall = 召回余弦 Semb；结局留待各丢弃点 resolve 回填）
                    trace.add_candidates([
                        TraceCandidate(tag_id=c.tag_id, tag_name=c.tag_name,
                                       dir_id=c.dir_id, recall=c.score,
                                       batch=start // batch_size)
                        for c in batch
                    ])
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
                # 5a. 单批调用与解析重试（v7）：真实端点偶发返回缺键元素，
                #     一次坏输出不再让整任务作废——记 warning（含 dir_id
                #     与原始输出前 400 字，此前零线索），追加格式纠正提示
                #     重调本批一次，仍失败才抛错。传输失败不在本层重试
                #     （client 层已做退避重试），此处带组语境直接抛错。
                #     有帧时消息走多模态 parts（v8）：prompt 全文与重试
                #     提示都在同一个 text part 里，帧 parts 跟在后面；
                #     无帧保持纯字符串格式（与既有测试桩兼容）。
                parsed: list[dict] | None = None
                parse_error: Exception | None = None
                last_raw = ""
                for attempt in range(2):
                    text = prompt if attempt == 0 else prompt + CLOSED_RETRY_HINT
                    request = ([{"role": "user", "content": text}]
                               if not frame_parts else
                               [{"role": "user", "content": [
                                   {"type": "text", "text": text},
                                   *frame_parts,
                               ]}])
                    resp = llm.complete(request, spec.run_id)
                    # 传输失败 → 任务失败语义由执行器落库；此处显式抛错
                    # 透出原因（带组语境，便于定位是哪个目录组的调用坏了）
                    if not resp.ok:
                        raise RuntimeError(
                            f"LLM 调用失败（目录组 {dir_id}）: "
                            f"{resp.raw.get('error')}")
                    try:
                        parsed = parse_closed_output(resp.content)
                        break
                    except LLMParseError as exc:
                        parse_error = exc
                        last_raw = resp.content or ""
                        logger.warning(
                            "闭环目录 %s 第 %d 次输出解析失败：%s；原始输出"
                            "前 400 字：%r", dir_id, attempt + 1, exc,
                            last_raw[:400])
                if parsed is None:
                    raise LLMParseError(
                        f"目录组 {dir_id} 重试后仍解析失败: {parse_error}；"
                        f"最后一次原始输出前 400 字：{last_raw[:400]!r}")
                if trace is not None:
                    # 采集点 3：批次落册（紧随解析成功 break——attempt 为
                    # 成功那次的下标，parse_retried = 首败重调过；raw 只存
                    # 最终成功那份输出，首败原文仅存留于上方 warning 日志）
                    trace.note_batch(dir_id, start // batch_size,
                                     len(batch), attempt == 1, resp.content)
                # 6. 终分与门控（a/b/c = 0.2/0.5/0.3）：Sapp 由 1–5 分线性
                #    映射到 0–1（(A−1)/4），Semb 取召回候选自带的余弦分；
                #    Sevi 为三模态融合分（v8）；G 为 Sapp≥0.75 的门控
                #    （A≥4 放行，A≤3 → Sf=0 整条丢弃）。Sf 与标签成对
                #    保留。编造 tag_id 在打分前剔除（否则会先占走组内保底
                #    名额再被丢，导致该组一条都不出）
                logger.debug("闭环目录 %s 本批解析出 %d 条判定", dir_id,
                             len(parsed))
                for it in parsed:
                    if it["applicability"] < 4:
                        # Sapp=(A−1)/4 < 0.75：门控关闭，G=0 → Sf=0 丢弃
                        dropped_gate += 1
                        if trace is not None:
                            # 采集点 4：门控丢弃（A≤3 → Sapp<0.75）
                            trace.resolve(it["tag_id"], "gate",
                                          applicability=it["applicability"])
                        logger.warning(
                            "闭环目录 %s 标签 %s 门控不过"
                            "（applicability=%d < 4），已丢弃", dir_id,
                            it.get("tag_id"), it["applicability"])
                        continue
                    if it["tag_id"] not in member_ids:
                        # 编造或越组引用（本组 prompt 只提供本组标签）：在
                        # 打分前剔除，避免先占走组内保底名额才被丢，或经
                        # 别组重复入选
                        dropped_fabricated += 1
                        if trace is not None:
                            # 采集点 5：编造/越组 id（按判定出现次序累积，
                            # 不去重——同 id 重复编造各记一条）
                            trace.note_fabricated(it["tag_id"])
                        logger.warning(
                            "闭环目录 %s 判定引用本组候选外标签 %s，已丢弃",
                            dir_id, it["tag_id"])
                        continue
                    candidate = by_id[it["tag_id"]]
                    s_app = (it["applicability"] - 1) / 4
                    sections = it["evidence"]
                    s_content = (sections["content"]["evidence_strength"] - 1) / 4
                    s_scene = (sections["scene"]["evidence_strength"] - 1) / 4
                    s_frame = (sections["frame"]["evidence_strength"] - 1) / 4
                    s_evi = (w_content * s_content + w_scene * s_scene
                             + w_frame * s_frame)
                    sf = (0.2 * candidate.score + 0.5 * s_app
                          + 0.3 * s_evi)
                    prev = scored_by_tag.get(it["tag_id"])
                    if prev is not None:
                        # tag_id 去重：契约不拒绝重复输出，persist 也不
                        # 去重；保留 Sf 最高分（涵盖跨批重复判定）
                        if sf <= prev[0]:
                            continue
                        logger.warning(
                            "闭环目录 %s 重复判定标签 %s，仅保留最高分",
                            dir_id, it["tag_id"])
                    scored_by_tag[it["tag_id"]] = (sf, it)
            scored = sorted(scored_by_tag.values(), key=lambda pair: -pair[0])
            logger.debug("闭环目录 %s 合并去重后 %d 条判定", dir_id,
                         len(scored))
            items.extend(scored)
        # 5.4 挂载解析前置（v6）：证据展开 + 目录粒度 → 挂载点。解析不出
        #     挂载点（典型：角色目录标签在多角色内容上定位不到唯一角色）
        #     的判定在**全局选择之前**淘汰，不占 M 名额——修复原先在第 6
        #     步后置丢弃且不回填导致的"选满 30 只出 28"缺额；宁可丢，不
        #     挂错。v8：三模态 evidences 条目展平成统一 Evidence 列表
        #     （顺序 content → scene → frame，条目 scene_id 校验保留）。
        #     item 结构从 (sf, it) 扩成 (sf, it, evidence, mount)
        scenes_by_id = {scene.scene_id: scene
                        for scene in spec.content.content.scenes}
        mounted: list[tuple[float, dict, list[Evidence], Mount]] = []
        dropped_unmountable = 0
        for sf, it in items:
            candidate = by_id[it["tag_id"]]  # 编造 id 已在打分前过滤
            evidence = []
            for modality in ("content", "scene", "frame"):
                for item in it["evidence"][modality]["evidences"]:
                    scene = scenes_by_id.get(item["scene_id"])
                    if scene is None:
                        logger.warning(
                            "闭环标签 %s 的 %s 模态证据引用未知场景 %s，"
                            "已忽略该证据", it["tag_id"], modality,
                            item["scene_id"])
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
                dropped_unmountable += 1
                if trace is not None:
                    # 采集点 6：挂载解析失败（选择前淘汰不占名额；sf 在
                    # 打分阶段已算出，随结局一并留痕）
                    trace.resolve(it["tag_id"], "unmountable",
                                  applicability=it["applicability"], sf=sf)
                logger.warning(
                    "闭环标签 %s（目录 %s，scope=%s）解析不出挂载点，"
                    "已在全局选择前丢弃，不占输出名额",
                    it["tag_id"], candidate.dir_id, candidate.scope)
                continue
            mounted.append((sf, it, evidence, mount))
        items = mounted
        # 5.5 全局选择（max_tags = 全局输出上限 M）：每组 Sf 最高 1 条作组
        #     保底；组数超 M 时保底间按 Sf 竞争截断，严格不超 M；剩余判定
        #     按 Sf 降序取前 M−len(保底) 补足。选择发生在挂载解析**之后**
        #     （v6），不可挂载的判定已被淘汰，选出的每一条都能落库。M 钳
        #     到 ≥0：直接调 API 传负数时避免负切片产生截尾怪相（前端
        #     InputNumber 已 min=1）
        max_tags = max(spec.policy.max_tags, 0)
        best_per_dir: dict[str, tuple] = {}
        for entry in items:
            dir_id = by_id[entry[1]["tag_id"]].dir_id
            kept = best_per_dir.get(dir_id)
            if kept is None or entry[0] > kept[0]:
                best_per_dir[dir_id] = entry
        tops = sorted(best_per_dir.values(),
                      key=lambda entry: -entry[0])[:max_tags]
        top_ids = {entry[1]["tag_id"] for entry in tops}
        rest = sorted((entry for entry in items
                       if entry[1]["tag_id"] not in top_ids),
                      key=lambda entry: -entry[0])
        # 输出整体按 Sf 降序：保底与补足合并后重排（入库行序 = 相关度
        # 降序的既有口径，v5 起如此）
        selected = sorted(tops + rest[: max(max_tags - len(tops), 0)],
                          key=lambda entry: -entry[0])
        if trace is not None:
            # 采集点 7：全局选择落定——selected 全员 kept；mounted 减
            # selected 的差集（可挂载但被 M 截下）记 m_cap。两组 tag_id
            # 不相交，与 resolve 末次为准的覆盖语义互不干扰
            selected_ids = {entry[1]["tag_id"] for entry in selected}
            for sf, it, _evidence, _mount in selected:
                trace.resolve(it["tag_id"], "kept",
                              applicability=it["applicability"], sf=sf)
            for sf, it, _evidence, _mount in mounted:
                if it["tag_id"] not in selected_ids:
                    trace.resolve(it["tag_id"], "m_cap",
                                  applicability=it["applicability"], sf=sf)
        # 5.6 选择汇总：一条日志回答"为什么只出了 N 条"——进入选择的判定
        #     数、组保底数、补足数、最终输出数，以及三类丢弃计数
        logger.info(
            "闭环全局选择完成：进入选择 %d 条 → 组保底 %d + 补足 %d = 输出 "
            "%d 条（上限 M=%d）；丢弃：门控不过 %d、挂载失败 %d、"
            "编造/越组 id %d",
            len(items), len(tops), len(selected) - len(tops), len(selected),
            max_tags, dropped_gate, dropped_unmountable, dropped_fabricated)
        # 6. 映射为审核前中间态：Sf 成对绑定到标签（relevance_score）；
        #    catalog 来源 tag_id 必有，候选目录直接取库。v8：confidence
        #    改为三模态融合 Sevi（与 Sf 里的 Sevi 项同口径）
        produced: list[ProducedTag] = []
        for sf, it, evidence, mount in selected:
            candidate = by_id[it["tag_id"]]  # 编造 id 已在打分前过滤
            sections = it["evidence"]
            s_evi = (w_content * (sections["content"]["evidence_strength"] - 1) / 4
                     + w_scene * (sections["scene"]["evidence_strength"] - 1) / 4
                     + w_frame * (sections["frame"]["evidence_strength"] - 1) / 4)
            produced.append(ProducedTag(
                origin="catalog",
                tag_id=candidate.tag_id,
                tag_name=candidate.tag_name,
                candidate_type=None,
                candidate_description=None,
                relevance_score=sf,
                confidence=s_evi,
                evidence=evidence,
                dir_id=candidate.dir_id,
                target_type=mount.target_type,
                target_id=mount.target_id,
                evidence_scores={
                    "content": sections["content"]["evidence_strength"],
                    "scene": sections["scene"]["evidence_strength"],
                    "frame": sections["frame"]["evidence_strength"],
                },
            ))
        return produced


register_producer(ClosedTagProducer())
