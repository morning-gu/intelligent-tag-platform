"""开放式生成流水线（MovieTagGen 移植版）：多轮生成 → 语义合并 → judge → 落库前产物。

与平台原 open 实现的差别（2026-11 替换）：
- 输入不是一份文本 prompt，而是"资产 JSON 的场景文本 + 按场景对齐的关键帧"；
- 一次任务跑 `mtg_runs` 轮（默认 3），跨轮并集去重后再做 e5 语义聚类；
- 产出先过 judge（deepseek-v4.1-flash）逐条判内容支持度，只有 supported
  且置信度过线的候选才落 `asset_tag`；
- 证据锚点必须是资产里真实存在的场景（模型编的 id 直接丢），
  与 MovieTagGen 的"证据可追溯"要求对齐。

保留的平台语义：
- 空目录不报错（与 closed 相反）；
- 标签库装满后按"库内对齐"分三档标记（`catalog_align`）：同名 → origin=catalog
  并挂 tag_id；疑似经 LLM 复核（`verify`）确认同一概念 → 替换成库内标准名并
  挂 tag_id（原词记进审计，供将来写别名）；其余走 generated，分档只进审计与
  排序，不删任何行；
- `persist_tags` 的 upsert 与审核已定集跳过规则不变（runner 负责落库）。

每任务一份审计 JSON（`mtg_report_dir/<task_id>.json`）：三轮原始候选、合并簇、
judge 判决、淘汰原因、tokens 与耗时——`generation_task` 表没有 usage 列，
不为此加迁移。
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import replace
from pathlib import Path

from sqlalchemy.orm import Session

from tagplatform.generation.base import Evidence, GenerationSpec, ProducedTag
from tagplatform.generation.mtg import keyframes as keyframes_mod
from tagplatform.generation.mtg.catalog_align import (
    BAND_DUPLICATE,
    BAND_NEW,
    BAND_RANK,
    BAND_SUSPECT,
    Alignment,
    align_candidates,
    recall_library_hints,
    summarize_bands,
)
from tagplatform.generation.mtg.judge import judge_candidates
from tagplatform.generation.mtg.merge import MergedCandidate, merge_candidates
from tagplatform.generation.mtg.parse import parse_generation_items
from tagplatform.generation.mtg.prompts import (
    build_generation_messages,
    render_content,
)
from tagplatform.generation.mtg.verify import verify_pairs
from tagplatform.library import (
    DirAssignment,
    library_index,
    rank_dir_assignments,
)
from tagplatform.mounting import resolve_mount
from tagplatform.settings import Settings, get_settings

logger = logging.getLogger(__name__)

# 口径版本号：写入 generation_task.prompt_version，重跑/换方案时可区分。
# v2 = 引入标签库（提示注入 + 候选分档 + 分档参与排序），与 v1（纯生成）分开记。
# v3 = 生成阶段就注入"可扩展目录"清单，由模型给出 dir（校验不过才回退向量归类）。
MTG_VERSION = "mtg-v3"

# 单条证据保留的场景数（审核卡展示用，多了没意义）
_MAX_EVIDENCE = 3

# judge 置信度下限：只挡"模型自己都不确定"的判定，不拿它跟
# policy.min_confidence 比——两者语义不同（一个是模型对标签的自评，一个是
# 审核判定把握），硬套 0.7 会误杀事实正确的标签（实测把 Memento 的
# "倒叙结构" 0.65 判死）。supported 的判定本来就该交给人工复核。
_JUDGE_CONFIDENCE_FLOOR = 0.5

# judge 必须引用原文：判决里给出 quote，与内容逐字对上（容忍标点/省略号差异）
# 才采信。这条把"judge 复读生成侧理由"变成可检测的失败：没有原文锚点的
# supported 一律淘汰，而不是凭一句"内容有依据"放行。
_QUOTE_MIN_CHARS = 8
_QUOTE_STRIP_RE = re.compile(
    r"[\s「」『』“”‘’\"'…—·,，。、；;：:！!？?（）()\[\]【】]+")


def _normalize_quote(text: str) -> str:
    return _QUOTE_STRIP_RE.sub("", text or "")


def _quote_ok(quote: str | None, haystack: str) -> bool:
    """quote 与内容有 ≥8 字连续重合即视为有效引用。"""
    normalized = _normalize_quote(str(quote or ""))
    if len(normalized) < _QUOTE_MIN_CHARS:
        return False
    for start in range(len(normalized) - _QUOTE_MIN_CHARS + 1):
        if normalized[start:start + _QUOTE_MIN_CHARS] in haystack:
            return True
    return False


def _require_quotes(verdicts: dict[str, dict], haystack: str) -> None:
    """引用不到原文的 supported 判决降级为 insufficient。

    并入判定本身（不单开一道闸、不加淘汰原因）：judge 说"有依据"但给不出
    可核对的原文，等同于没依据。
    """
    for verdict in verdicts.values():
        if verdict.get("verdict") != "supported":
            continue
        if _quote_ok(verdict.get("quote"), haystack):
            continue
        verdict["verdict"] = "insufficient"
        verdict["reason"] = (verdict.get("reason") or "未引用可核对原文")[:40]


# judge 重要度排序（core 优先），队列截断的第一关键字
_IMPORTANCE_RANK = {"core": 3, "secondary": 2, "incidental": 1}


def _add_usage(total: dict, usage: dict | None) -> None:
    usage = usage or {}
    total["calls"] = total.get("calls", 0) + 1
    for key in ("prompt_tokens", "completion_tokens"):
        total[key] = total.get(key, 0) + int(usage.get(key, 0) or 0)


def _make_judge_client(settings: Settings, llm):
    """judge 客户端：mock 路径复用传入的 mock；真实路径按 judge 模型另建。

    判定"mock 路径"有两个依据——配置开关，或调用方传进来的就是
    MockLLMClient（单元测试与进程内 e2e 都走后者），避免测试意外联网。
    """
    from tagplatform.llm.mock import MockLLMClient
    if settings.use_mock_llm or isinstance(llm, MockLLMClient):
        return llm
    from tagplatform.llm import OpenAIClient
    return OpenAIClient(
        model=settings.mtg_judge_model,
        base_url_env=settings.llm_base_url_env,
        api_key_env=settings.llm_api_key_env,
        temperature=settings.mtg_judge_temperature,
        request_interval=settings.llm_request_interval,
    )


def _make_verify_client(settings: Settings, llm):
    """相似度复核客户端：与 judge 同模型，但温度 0 且**卡输出上限**。

    judge 一次要出 20 条判决，输出量本来就大；复核只有几条二分类，实测却
    有一次烧掉 27k completion tokens（推理模型在模棱两可的对子上反复权衡），
    把 4 分钟的任务拖到 7 分钟。这里给 max_tokens 兜底，超了就解析失败 →
    不替换，代价可控。
    """
    from tagplatform.llm.mock import MockLLMClient
    if settings.use_mock_llm or isinstance(llm, MockLLMClient):
        return llm
    from tagplatform.llm import OpenAIClient
    return OpenAIClient(
        model=settings.mtg_judge_model,
        base_url_env=settings.llm_base_url_env,
        api_key_env=settings.llm_api_key_env,
        temperature=0.0,
        max_tokens=settings.open_library_verify_max_tokens,
        request_interval=settings.llm_request_interval,
    )


def _dir_hint_entries(tag_library, spec: GenerationSpec,
                      limit: int) -> list[dict]:
    """可新增标签的目录 → 提示词条目（id + 全路径 + 粒度）。

    只收 `dir_dynamic`（允许扩充）且落在本次生成范围内的目录——静态目录
    不该出现在提示词里，否则模型会往不允许新增的目录里塞词，然后在校验
    环节被打回，白烧一轮 token。
    """
    if tag_library is None or limit <= 0:
        return []
    entries: list[dict] = []
    for directory in tag_library.new_tag_dirs(spec.policy.dir_ids):
        path = "/".join(tag_library.path_of(directory.dir_id)) or directory.dir_name
        entries.append({"dir_id": directory.dir_id, "path": path,
                        "scope": tag_library.scope_of(directory.dir_id)})
    entries.sort(key=lambda item: (item["path"], item["dir_id"]))
    return entries[:limit]


def _resolve_dir_value(tag_library, raw: str | None) -> str | None:
    """模型给的目录串 → dir_id（容忍 id / 全路径 / 唯一目录名）。"""
    if not raw or tag_library is None:
        return None
    text = raw.strip()
    if tag_library.directory(text) is not None:
        return text
    by_path = {"/".join(tag_library.path_of(d.dir_id)): d.dir_id
               for d in tag_library.dirs()}
    if text in by_path:
        return by_path[text]
    by_name = [d.dir_id for d in tag_library.dirs() if d.dir_name == text]
    return by_name[0] if len(by_name) == 1 else None


def _validate_model_dir(tag_library, dir_id: str | None, spec: GenerationSpec,
                        contents, evidence: list[Evidence],
                        per_dir_count: dict[str, int]
                        ) -> tuple[DirAssignment | None, object | None, str]:
    """校验模型指定的目录：范围 → 存在 → 可扩展 → 配额 → 挂载点。

    任何一步不过都返回 (None, None, 原因)，由调用方回退到向量归类；不静默
    改写模型的目录归属（审核页要能看到"模型选错了"这件事）。
    """
    if not dir_id:
        return None, None, "模型未给目录"
    allowed = tag_library.descendants(spec.policy.dir_ids)
    if dir_id not in allowed:
        return None, None, f"模型给的目录 {dir_id} 不在本次生成范围"
    directory = tag_library.directory(dir_id)
    if directory is None:
        return None, None, f"模型给的目录 {dir_id} 不存在"
    if not directory.allows_new_tags:
        return None, None, f"目录「{directory.dir_name}」不允许新增标签"
    if (spec.policy.max_tags_per_dir
            and per_dir_count.get(dir_id, 0) >= spec.policy.max_tags_per_dir):
        return None, None, f"目录「{directory.dir_name}」已达产出上限"
    scope = tag_library.scope_of(dir_id)
    mount = resolve_mount(scope, spec.asset_id, contents, evidence)
    if mount is None:
        return None, None, (f"目录「{directory.dir_name}」适用范围 {scope} "
                            "解析不出唯一挂载点")
    return (DirAssignment(dir_id=dir_id, dir_name=directory.dir_name,
                          scope=scope, score=0.0, reason="模型指定目录"),
            mount, "")


def _evidence_for(candidate: MergedCandidate, verdict: dict | None,
                  scene_index: dict) -> list[Evidence]:
    """候选 → 证据列表：judge 引用优先，回退生成侧场景，只保留真实场景。

    judge 给了场景就只用它（那是"它凭什么判支持"的依据）；judge 没给场景
    时才回退生成侧的场景并集——不把两边混在一起，避免证据卡里出现
    与判定无关的场景。
    """
    verdict_scenes = [s for s in (verdict or {}).get("scenes") or []
                      if s in scene_index]
    source = verdict_scenes or [s for s in candidate.scenes if s in scene_index]
    evidence: list[Evidence] = []
    seen: set[str] = set()
    for scene_id in source:
        if scene_id in seen:
            continue
        seen.add(scene_id)
        scene = scene_index[scene_id]
        evidence.append(Evidence(
            scene_id=scene_id,
            start_ms=scene.time_range.start_ms,
            end_ms=scene.time_range.end_ms,
            description=(scene.description or "")[:120],
        ))
        if len(evidence) >= _MAX_EVIDENCE:
            break
    return evidence


def run_open_generation(spec: GenerationSpec, session: Session, llm, embedder,
                        judge_client=None) -> list[ProducedTag]:
    """跑一次开放式生成，返回落库前的 ProducedTag 列表（不写库）。"""
    settings = get_settings()
    started = time.monotonic()
    contents = spec.content
    frames = keyframes_mod.plan_scene_frames(
        contents, settings.dataset_root,
        per_scene=settings.mtg_frames_per_scene,
        max_frames=settings.mtg_max_frames,
    )
    frame_count = sum(len(v) for v in frames.values())
    usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}

    # ---- 0. 标签库：给模型看的"已有标签"清单（只读，绝不写库）----
    # 库来自 JSON 模拟（spec.library），按任务的目录范围过滤；向量按
    # 库/模型/范围缓存，同一进程内不重复嵌入。
    tag_library = spec.library if settings.open_library_enabled else None
    library = (library_index(tag_library, embedder, spec.policy.dir_ids)
               if tag_library is not None else [])
    hints: list[str] = []
    if library and settings.open_library_hint_enabled:
        hints = recall_library_hints(library, embedder, contents,
                                     settings.open_library_hint_count)
    # 可扩展目录清单：生成阶段就交给模型决定归属（"在某目录下新增某标签"），
    # 而不是等生成完再按向量猜。只列 dir_dynamic 且落在范围内的目录。
    dir_hint = (_dir_hint_entries(tag_library, spec, settings.open_dir_hint_max)
                if settings.open_dir_hint_max > 0 else [])

    # ---- 1. 多轮生成 ----
    entries: list[dict] = []
    runs: list[dict] = []
    for run_index in range(max(1, settings.mtg_runs)):
        messages = build_generation_messages(contents, frames, run_index,
                                             library_hint=hints,
                                             dir_hint=dir_hint)
        response = llm.complete(messages, f"{spec.run_id}:r{run_index}")
        _add_usage(usage, response.usage)
        if not response.ok:
            raise RuntimeError(
                f"LLM 调用失败（第 {run_index + 1} 轮）: {response.raw.get('error')}")
        try:
            items = parse_generation_items(response.content,
                                           settings.mtg_max_candidates_per_run)
        except Exception as exc:  # noqa: BLE001 —— 解析失败：本轮记 0，跨轮继续
            logger.warning("第 %d 轮输出解析失败：%s", run_index + 1, exc)
            items = []
        for item in items:
            entries.append({**item, "run": run_index})
        runs.append({"run": run_index, "items": len(items),
                     "prompt_tokens": int(response.usage.get("prompt_tokens", 0) or 0),
                     "completion_tokens": int(response.usage.get("completion_tokens", 0) or 0)})
        logger.info("mtg 第 %d 轮：%d 个候选", run_index + 1, len(items))

    if not entries:
        raise RuntimeError("各轮生成均无可解析标签")

    # ---- 2. 跨轮合并 ----
    merged = merge_candidates(entries, embedder, settings.mtg_merge_threshold)

    # ---- 3. judge ----
    verdicts: dict[str, dict] = {}
    judge_usage: dict = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
    if settings.mtg_judge_enabled and merged:
        client = judge_client or _make_judge_client(settings, llm)
        payload = [{"tag": m.tag, "dimension": m.dimension,
                    "reason": m.reason, "scenes": list(m.scenes)}
                   for m in merged]
        try:
            verdicts = judge_candidates(client, contents, payload,
                                        run_id=spec.run_id, usage=judge_usage)
        except Exception as exc:  # noqa: BLE001 —— 失败也要留审计现场
            _write_report(settings, spec.task_id, {
                "version": MTG_VERSION,
                "task_id": spec.task_id,
                "asset_id": spec.asset_id,
                "error": f"{type(exc).__name__}: {exc}",
                "scenes": len(contents.content.scenes),
                "frames": frame_count,
                "usage": usage,
                "judge_usage": judge_usage,
                "candidates": [m.tag for m in merged],
                "elapsed_s": round(time.monotonic() - started, 1),
            })
            raise

    # ---- 4. 过滤 + 证据 + 目录匹配 ----
    scene_index = {scene.scene_id: scene for scene in contents.content.scenes}
    if settings.mtg_judge_enabled:
        _require_quotes(verdicts, _normalize_quote(render_content(contents)))
    accepted: list[tuple[MergedCandidate, dict | None, list[Evidence]]] = []
    dropped: list[dict] = []
    for candidate in merged:
        verdict = verdicts.get(_key(candidate.tag))
        if settings.mtg_judge_enabled:
            if verdict is None:
                dropped.append({"tag": candidate.tag, "why": "judge 未返回判决"})
                continue
            if verdict["verdict"] != "supported":
                dropped.append({"tag": candidate.tag,
                                "why": f"judge={verdict['verdict']}"})
                continue
            if verdict["confidence"] < _JUDGE_CONFIDENCE_FLOOR:
                dropped.append({"tag": candidate.tag,
                                "why": f"judge 判定把握 {verdict['confidence']:.2f} "
                                       f"< {_JUDGE_CONFIDENCE_FLOOR}"})
                continue
        evidence = _evidence_for(candidate, verdict, scene_index)
        if not evidence:
            dropped.append({"tag": candidate.tag, "why": "无有效场景证据"})
            continue
        # 层级约束（只用于把关，不落库）：全片级必须有两个以上场景支撑，
        # 单场支撑的"全片级"标签一律不进队列——它本质是场景级措辞被写大了
        if candidate.scope == "film" and len(evidence) < 2:
            dropped.append({"tag": candidate.tag,
                            "why": "全片级但只有单场支撑"})
            continue
        accepted.append((candidate, verdict, evidence))

    # ---- 5. 库内对齐、复核、排序、截断、落库归属 ----
    # 对齐放在截断之前：分档要参与排序（新词优先占名额），也要在审计里
    # 记录"被挤掉的是哪一档"，截断后再对齐就只剩 15 条、看不到全貌。
    alignment: dict[str, Alignment] = {}
    replacements: list[dict] = []
    verify_usage: dict = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
    verify_trace: dict = {}
    if library and accepted:
        alignment = align_candidates(
            library, embedder, [candidate.tag for candidate, _, _ in accepted],
            suspect_cosine=settings.open_library_suspect_cosine,
            name_overlap=settings.open_library_name_overlap,
        )
        # 疑似对子交给模型复核：分档只能看"像不像"，替换要看"是不是"。
        # 复核失败/开关关掉都按"未复核"处理（不替换），并把结论写进审计。
        if settings.open_library_verify_enabled:
            pairs = [(name, item.matched_name, item.cosine or 0.0)
                     for name, item in alignment.items()
                     if item.band == BAND_SUSPECT and item.matched_name]
            if pairs:
                client = _make_verify_client(settings, llm)
                verdict_map = verify_pairs(client, pairs, run_id=spec.run_id,
                                           usage=verify_usage, trace=verify_trace)
                for name, same in verdict_map.items():
                    if name not in alignment:
                        continue
                    item = alignment[name]
                    if same is False and item.band == BAND_SUSPECT:
                        # 复核否定的就是"新标签"：分档要跟着改，否则它会被
                        # 一直当疑似压在排序后面，把名额让给别的疑似。
                        alignment[name] = replace(
                            item, same_concept=False, band=BAND_NEW,
                            reason=f"{item.reason}；复核判非同一概念")
                    else:
                        alignment[name] = replace(item, same_concept=same)

    limit = min(settings.mtg_max_final_tags, spec.policy.max_tags)

    def _band_rank(tag: str) -> int:
        item = alignment.get(tag)
        return BAND_RANK[item.band] if item is not None else BAND_RANK["new"]

    def _rank(item) -> tuple[int, int, float, int]:
        """队列排序：重要度 → 库内分档（新词优先）→ judge 把握 → 出现轮数。

        先看重要度而不是轮数：重复出现只说明"模型爱说"，不代表这条标签
        对影片重要；实测按轮数排会把 0.5 分的"自然光效运用"排进队列，
        而 core 的"视角反转"被挤掉。分档插在重要度之后、把握之前：
        同样重要的两条，优先留库外新词。
        """
        candidate, verdict, _ = item
        importance = _IMPORTANCE_RANK.get((verdict or {}).get("importance"), 0)
        confidence = verdict["confidence"] if verdict else 0.8
        return (-importance, -_band_rank(candidate.tag), -confidence,
                -candidate.runs)

    accepted.sort(key=_rank)

    def _plan(candidate) -> tuple[Alignment | None, bool]:
        """(对齐结论, 是否替换成库内标签)。

        同名 → 一律算库内标签（替换开关不影响这条：名字本来一样）；
        疑似 → 复核通过 + 替换开关打开才换成库内标准名。
        """
        item = alignment.get(candidate.tag)
        same_name = bool(item is not None and item.band == BAND_DUPLICATE
                         and item.matched_tag_id)
        replace_suspect = bool(item is not None
                               and settings.open_library_replace_enabled
                               and item.band == BAND_SUSPECT
                               and item.same_concept is True
                               and item.matched_tag_id)
        return item, same_name or replace_suspect

    plans = {candidate.tag: _plan(candidate) for candidate, _, _ in accepted}
    # 命中的库内标签不占新标签名额：它们本来就是"库里已有的东西"，
    # 让它挤掉一条真正的新词没有道理。两桶各自排序、各自受 limit 约束。
    replaced = [item for item in accepted if plans[item[0].tag][1]]
    fresh = [item for item in accepted if not plans[item[0].tag][1]]
    kept_replaced = replaced[:limit]
    truncated = fresh[limit:] + replaced[limit:]
    accepted = fresh[:limit] + kept_replaced

    tags: list[ProducedTag] = []
    tag_rows: list[dict] = []
    # 新词要落到"可扩展目录"里：先批量嵌入候选名，再逐个按目录描述相似度归属
    new_names = [candidate.tag for candidate, _, _ in accepted
                 if not plans.get(candidate.tag, (None, False))[1]]
    new_vectors = (embedder.embed(new_names, role="query")
                   if new_names else [])
    vector_by_name = {name: vec for name, vec in zip(new_names, new_vectors)}
    per_dir_count: dict[str, int] = {}
    for candidate, verdict, evidence in accepted:
        confidence = verdict["confidence"] if verdict else 0.8
        reason = (verdict or {}).get("reason") or candidate.reason
        # 层级随合并结果落库：film 需≥2 场景支撑（上游已校验），scene 为
        # 个别场景特征；scope 缺失（旧数据/解析失败）按 film 兜底
        level = candidate.scope if candidate.scope in ("film", "scene") else "film"
        item, is_replace = plans.get(candidate.tag, (None, False))
        if is_replace and item is not None:
            matched = next((tag for tag in library
                            if tag.tag_id == item.matched_tag_id), None)
            mount = resolve_mount(matched.scope if matched else "content",
                                  spec.asset_id, contents, evidence)
            if mount is None:
                dropped.append({"tag": candidate.tag,
                                "why": f"目录适用范围 {matched.scope if matched else '?'} "
                                       "解析不出唯一挂载点"})
                continue
            if item.band == BAND_SUSPECT:
                replacements.append({
                    "generated": candidate.tag,
                    "library": item.matched_name,
                    "tag_id": item.matched_tag_id,
                    "cosine": (None if item.cosine is None
                               else round(item.cosine, 3)),
                    "reason": item.reason,
                })
            tags.append(ProducedTag(
                origin="catalog", tag_id=item.matched_tag_id,
                tag_name=item.matched_name or candidate.tag,
                candidate_type=None, candidate_description=None,
                relevance_score=item.cosine, confidence=confidence,
                evidence=evidence, level=level,
                dir_id=matched.dir_id if matched else None,
                target_type=mount.target_type, target_id=mount.target_id,
                # 相似命中：模型原词将来写进 tag_define.aliases
                alias_name=(candidate.tag
                            if item.band == BAND_SUSPECT
                            and candidate.tag != (item.matched_name or "") else None),
            ))
            tag_rows.append({"tag": candidate.tag, "name": tags[-1].tag_name,
                             "origin": "catalog", "band": item.band,
                             "dimension": None,
                             "dir_id": tags[-1].dir_id,
                             "dir_source": "library",
                             "target_type": mount.target_type,
                             "confidence": confidence,
                             "evidence": [e.scene_id for e in evidence]})
            continue

        # 新词：先看模型给的目录能不能用，再回退到向量归类
        if not spec.policy.allow_new_tags:
            dropped.append({"tag": candidate.tag, "why": "策略禁止产出新词"})
            continue
        if tag_library is None:
            dropped.append({"tag": candidate.tag, "why": "标签库不可用，新词无处归属"})
            continue
        # ① 模型在生成时就选好的目录：只做校验，合法就直接用（这是"生成时
        #    考虑目录"的主路径）；不合法记下原因，回退②，不静默改写归属。
        assignment = None
        mount = None
        dir_source = None
        dir_reason = ""
        reasons: list[str] = []
        model_dir = _resolve_dir_value(tag_library, candidate.dir_raw)
        if candidate.dir_raw and model_dir is None:
            reasons.append(f"模型给的目录「{candidate.dir_raw}」无法解析")
        if model_dir:
            assignment, mount, why = _validate_model_dir(
                tag_library, model_dir, spec, contents, evidence, per_dir_count)
            if assignment is not None:
                dir_source = "model"
            else:
                reasons.append(why)
                assignment = mount = None
        # ② 回退：按与目录描述的向量相似度逐个试（模型没给/给错时的兜底）
        if assignment is None:
            for option in rank_dir_assignments(tag_library, embedder,
                                               vector_by_name.get(candidate.tag),
                                               spec.policy.dir_ids):
                if option.dir_id is None:
                    reasons.append(f"无可归属目录（{option.reason}）")
                    continue
                if (spec.policy.max_tags_per_dir
                        and per_dir_count.get(option.dir_id, 0)
                        >= spec.policy.max_tags_per_dir):
                    reasons.append(f"目录「{option.dir_name}」已达产出上限")
                    continue
                candidate_mount = resolve_mount(option.scope, spec.asset_id,
                                                contents, evidence)
                if candidate_mount is None:
                    reasons.append(f"目录「{option.dir_name}」适用范围 "
                                   f"{option.scope} 解析不出唯一挂载点")
                    continue
                assignment, mount = option, candidate_mount
                dir_source = "fallback"
                dir_reason = option.reason
                break
        if assignment is None or mount is None:
            dropped.append({"tag": candidate.tag,
                            "why": "；".join(reasons) or "无可归属目录"})
            continue
        per_dir_count[assignment.dir_id] = per_dir_count.get(assignment.dir_id, 0) + 1
        tags.append(ProducedTag(
            origin="generated", tag_id=None, tag_name=candidate.tag,
            candidate_type=candidate.dimension,
            candidate_description=reason or None,
            # 相关度 = 与库内最相近标签的余弦（对齐阶段的 top-1，判成新词也有
            # 值——"有多不像库里的东西"本身就是审核时要看的信号）。库里没标签
            # 或本条没参与对齐时才是 None。
            relevance_score=(item.cosine if item is not None else None),
            confidence=confidence,
            evidence=evidence, level=level,
            dir_id=assignment.dir_id,
            target_type=mount.target_type, target_id=mount.target_id,
        ))
        tag_rows.append({"tag": candidate.tag, "name": tags[-1].tag_name,
                         "origin": "generated",
                         "band": (item.band if item is not None else None),
                         "dimension": candidate.dimension,
                         "dir_id": assignment.dir_id,
                         "dir_name": assignment.dir_name,
                         "dir_source": dir_source,
                         "dir_model_raw": candidate.dir_raw,
                         "dir_reason": (assignment.reason
                                        if dir_source == "model"
                                        else (dir_reason or assignment.reason)),
                         "target_type": mount.target_type,
                         "confidence": confidence,
                         "evidence": [e.scene_id for e in evidence]})

    # ---- 6. 审计落盘 ----
    elapsed = time.monotonic() - started
    report = {
        "version": MTG_VERSION,
        "task_id": spec.task_id,
        "asset_id": spec.asset_id,
        "model": getattr(llm, "model", None),
        "judge_model": (settings.mtg_judge_model
                        if settings.mtg_judge_enabled and not settings.use_mock_llm
                        else "mock"),
        "params": {
            "runs": settings.mtg_runs,
            "frames_per_scene": settings.mtg_frames_per_scene,
            "max_frames": settings.mtg_max_frames,
            "merge_threshold": settings.mtg_merge_threshold,
            "judge_confidence_floor": _JUDGE_CONFIDENCE_FLOOR,
            "max_final_tags": limit,
            "library_enabled": bool(settings.open_library_enabled),
            "library_hint_count": len(hints),
            "suspect_cosine": settings.open_library_suspect_cosine,
            "name_overlap": settings.open_library_name_overlap,
            "verify_enabled": bool(settings.open_library_verify_enabled),
            "replace_enabled": bool(settings.open_library_replace_enabled),
            "dir_hint_max": settings.open_dir_hint_max,
            "dir_hint_count": len(dir_hint),
        },
        "scenes": len(contents.content.scenes),
        "frames": frame_count,
        "runs": runs,
        "usage": usage,
        "judge_usage": judge_usage,
        "merged": [{"tag": m.tag, "aliases": list(m.aliases),
                    "runs": m.runs, "dimension": m.dimension,
                    "scope": m.scope}
                   for m in merged],
        "verdicts": list(verdicts.values()),
        "accepted": [{"tag": row["name"], "candidate": row["tag"],
                      "origin": row["origin"],
                      "dir_id": row.get("dir_id"),
                      "dir_source": row.get("dir_source"),
                      "dir_model_raw": row.get("dir_model_raw"),
                      "dir_reason": row.get("dir_reason"),
                      "confidence": row["confidence"], "band": row["band"],
                      "evidence": row["evidence"]}
                     for row in tag_rows],
        "dir_hint": [entry["dir_id"] for entry in dir_hint],
        "library": {
            "enabled": bool(library),
            "size": len(library),
            "hints": hints,
            "bands": summarize_bands(alignment),
            "align": [
                {"tag": name, "band": item.band, "reason": item.reason,
                 "matched": item.matched_name,
                 "cosine": None if item.cosine is None else round(item.cosine, 3),
                 "same_concept": item.same_concept}
                for name, item in alignment.items()
            ],
            "replacements": replacements,
            "verify_raw": verify_trace.get("raw"),
            "truncated": [
                {"tag": candidate.tag,
                 "band": (alignment[candidate.tag].band
                          if candidate.tag in alignment else None)}
                for candidate, _, _ in truncated
            ],
        },
        "verify_usage": verify_usage,
        "dropped": dropped,
        "elapsed_s": round(elapsed, 1),
    }
    _write_report(settings, spec.task_id, report)
    logger.info("mtg 完成：%d 轮 / %d 帧 / 合并 %d / 接受 %d / %.1fs",
                settings.mtg_runs, frame_count, len(merged), len(tags), elapsed)
    return tags


def _key(tag: str) -> str:
    """与合并层/judge 层一致的归一化键。"""
    return "".join(ch for ch in tag.casefold() if ch.isalnum())


def _write_report(settings: Settings, task_id: str, report: dict) -> None:
    """审计报告落盘；写失败只告警，不影响生成结果。"""
    try:
        target = Path(settings.mtg_report_dir)
        target.mkdir(parents=True, exist_ok=True)
        (target / f"{task_id}.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError as exc:  # 磁盘问题不该让任务失败
        logger.warning("审计报告写入失败 %s：%s", task_id, exc)
