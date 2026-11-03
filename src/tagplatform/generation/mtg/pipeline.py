"""开放式生成流水线（MovieTagGen 移植版）：多轮生成 → 语义合并 → judge → 落库前产物。

与平台原 open 实现的差别（2026-11 替换）：
- 输入不是一份文本 prompt，而是"资产 JSON 的场景文本 + 按场景对齐的关键帧"；
- 一次任务跑 `mtg_runs` 轮（默认 3），跨轮并集去重后再做 e5 语义聚类；
- 产出先过 judge（deepseek-v4.1-flash）逐条判内容支持度，只有 supported
  且置信度过线的候选才落 `asset_tag`；
- 证据锚点必须是资产里真实存在的场景（模型编的 id 直接丢），
  与 MovieTagGen 的"证据可追溯"要求对齐。

保留的平台语义：
- 空目录不报错（与 closed 相反），命中 `tag_catalog` 的候选仍标
  origin=catalog（阈值 `open_match_threshold`），今天目录为空时全部 generated；
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
from pathlib import Path

from sqlalchemy.orm import Session

from tagplatform.db.models import TagCatalog
from tagplatform.generation.base import Evidence, GenerationSpec, ProducedTag
from tagplatform.generation.mtg import keyframes as keyframes_mod
from tagplatform.generation.mtg.judge import judge_candidates
from tagplatform.generation.mtg.merge import MergedCandidate, merge_candidates
from tagplatform.generation.mtg.parse import parse_generation_items
from tagplatform.generation.mtg.prompts import (
    build_generation_messages,
    render_content,
)
from tagplatform.settings import Settings, get_settings
from tagplatform.vectors import decode_vector, search_similar

logger = logging.getLogger(__name__)

# 口径版本号：写入 generation_task.prompt_version，重跑/换方案时可区分
MTG_VERSION = "mtg-v1"

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


def _catalog_hits(session: Session, embedder, names: list[str],
                  threshold: float) -> dict[str, tuple[str, float]]:
    """候选名 → (tag_id, 相似度)：命中目录（>= 阈值）才算，空目录返回空。"""
    rows = [row for row in session.query(TagCatalog).all()
            if row.status != "deprecate" and row.embedding]
    if not rows or not names:
        return {}
    vectors = embedder.embed(names, role="query")
    candidates = [(row.tag_id, decode_vector(row.embedding)) for row in rows]
    hits: dict[str, tuple[str, float]] = {}
    for name, vector in zip(names, vectors):
        matched = search_similar(vector, candidates, top_k=1)
        if matched and matched[0][1] >= threshold:
            hits[name] = (matched[0][0], float(matched[0][1]))
    return hits


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

    # ---- 1. 多轮生成 ----
    entries: list[dict] = []
    runs: list[dict] = []
    for run_index in range(max(1, settings.mtg_runs)):
        messages = build_generation_messages(contents, frames, run_index)
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

    # ---- 5. 排序、截断、目录匹配 ----
    limit = min(settings.mtg_max_final_tags, spec.policy.max_tags)

    def _rank(item) -> tuple[int, float, int]:
        """队列排序：重要度（core 优先）→ judge 把握 → 出现轮数。

        先看重要度而不是轮数：重复出现只说明"模型爱说"，不代表这条标签
        对影片重要；实测按轮数排会把 0.5 分的"自然光效运用"排进队列，
        而 core 的"视角反转"被挤掉。
        """
        candidate, verdict, _ = item
        importance = _IMPORTANCE_RANK.get((verdict or {}).get("importance"), 0)
        confidence = verdict["confidence"] if verdict else 0.8
        return (-importance, -confidence, -candidate.runs)

    accepted.sort(key=_rank)
    accepted = accepted[:limit]

    names = [candidate.tag for candidate, _, _ in accepted]
    hits = _catalog_hits(session, embedder, names,
                         settings.open_match_threshold) if names else {}

    tags: list[ProducedTag] = []
    for candidate, verdict, evidence in accepted:
        confidence = verdict["confidence"] if verdict else 0.8
        reason = (verdict or {}).get("reason") or candidate.reason
        if candidate.tag in hits:
            tag_id, similarity = hits[candidate.tag]
            tags.append(ProducedTag(
                origin="catalog", tag_id=tag_id, tag_name=candidate.tag,
                candidate_type=None, candidate_description=None,
                relevance_score=similarity, confidence=confidence,
                evidence=evidence,
            ))
        else:
            tags.append(ProducedTag(
                origin="generated", tag_id=None, tag_name=candidate.tag,
                candidate_type=candidate.dimension,
                candidate_description=reason or None,
                relevance_score=None, confidence=confidence,
                evidence=evidence,
            ))

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
        "accepted": [{"tag": t.tag_name, "origin": t.origin,
                      "dimension": t.candidate_type,
                      "confidence": t.confidence,
                      "evidence": [e.scene_id for e in t.evidence]}
                     for t in tags],
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
