"""judge 审核：按统一三维评分标准逐候选评分（评审归一 Task 6 接统一引擎）。

候选投影成 `mtg.verdicts.VerdictTarget` 后交 `run_verdicts` 统一批编排：
固定 20 条一批、漏判（含整批格式失败）带共享重试提示补试一轮、预算 2n+2。
解析（parse_verdicts）、证据核验（quote 逐字 + 场景计数压分）与 value 三方向
聚合（finalize_verdict）全部由引擎承载，本模块不再保留私有解析/映射/复核
逻辑——judge 与开放评测评审共用同一解析器、同一核验终判与同一批循环，两侧
分歧只剩策略参数（judge：分批 20、漏判重试一轮、名称兜底开）。

返回 {key: 终判}：key = candidate_id 或归一化标签名，值为 finalize_verdict
输出（relevance/semantics {"score","reason"}、value 聚合块、quote、scenes、
confidence、reason、verify_fail）。漏判候选不在返回值也不抛错——上层
`_filter_candidates` 按未返回淘汰；零判决时抛 JudgeError（任务置 failed）。

trace 由引擎写统一键族（batches/errors/salvaged，见 run_verdicts docstring），
与 legacy 的 raw 列表不同——审计读侧按新键族消费（逐批原始响应与漏判可还原）。

judge 客户端与生成客户端分离：生成用 qwen3.7-plus，judge 用
`settings.mtg_judge_model`（deepseek-v4.1-flash），temperature=0。
"""
from __future__ import annotations

import logging
from collections.abc import Mapping

from tagplatform.capabilities.prompts.mtg import JUDGE_RETRY_HINT
from tagplatform.services.generation.open.mtg.normalization import normalize_key
from tagplatform.services.generation.open.mtg.prompts import (
    build_judge_messages, candidate_line, judge_dir_block, render_content,
    render_judge_messages, render_scene,
)
from tagplatform.services.generation.open.mtg.verdicts import (
    VerdictPolicy, VerdictTarget, _normalize_quote, run_verdicts,
)
from tagplatform.services.tags.value_scoring import normalize_tag_kind
from tagplatform.schemas import ContentJSON


logger = logging.getLogger(__name__)

_MAX_BATCH_SIZE = 20


def build_judge_template(*, with_candidate_ids: bool = True) -> dict:
    marks = "标签种类 {{标签种类}}｜目录 {{目录 ID}}"
    if with_candidate_ids:
        marks = "excerpt｜依据场景 {{来源场景 ID}}｜" + marks + "｜候选 ID {{候选 ID}}"
    line = candidate_line(
        "{{候选标签}}", "{{维度}}",
        f"〔{marks}〕释义：{{{{生成侧通用释义}}}}；依据：{{{{生成侧的简短理由}}}}",
    )
    dirs = [{"dir_id": "{{目录 ID}}", "path": "{{目录全路径}}",
             "scope": "content", "description": "{{目录定义}}"}]
    return {
        "messages": render_judge_messages(
            "{{影片资料与按场景组织的内容文本}}", line, dir_block=judge_dir_block(dirs),
            with_candidate_ids=with_candidate_ids, with_excerpts=with_candidate_ids),
        "retry_suffix": JUDGE_RETRY_HINT,
    }


class JudgeError(RuntimeError):
    """judge 调用或解析失败（任务应置 failed，不得静默放行）。"""


def _candidate_tag_kind(candidate: dict, dir_index: dict[str, dict]) -> str:
    raw = candidate.get("tag_kind")
    if not raw and candidate.get("dir_ids"):
        first_dir = dir_index.get(candidate["dir_ids"][0]) or {}
        raw = first_dir.get("scope")
    return normalize_tag_kind(raw)


def judge_candidates(
    client, content: ContentJSON, candidates: list[dict],
    *, run_id: str, retries: int = 1,
    include_dialogue: bool = True,
    usage: dict | None = None,
    trace: dict | None = None,
    dirs: list[dict] | None = None,
    value_weights: Mapping[str, tuple[float, float, float]] | None = None,
) -> dict[str, dict]:
    """候选 → {候选 ID 或归一化标签名: 终判 dict}（finalize_verdict 形状）。

    批编排/解析/证据核验/价值聚合由 `mtg.verdicts.run_verdicts` 承载：
    20 条一批、漏判带重试提示补试一轮后落 errors（不整批盲重试——与 legacy
    的有意分歧）、预算 2n+2。`retries` 参数保留兼容旧签名：整批重试语义由
    引擎策略承载，取值不影响行为。usage/trace 原样透传引擎。候选与目标
    按 key 一一对应（`candidates_by_key`），build_messages 收到 VerdictTarget
    批时映射回候选 dict 渲染候选行。`candidates` 为空直接返回空表；全部
    未判时抛 JudgeError，部分漏判不抛错。
    """
    if not candidates:
        return {}
    dir_index = {entry["dir_id"]: entry for entry in (dirs or [])}
    modalities = {"script", "dialogue"} if include_dialogue else {"script"}
    # 证据核验底本与生成侧同口径：消融关掉台词后再拿全文去核，会让"引用了
    # 模型没看过的台词"误判为有效（与原过滤段引用核对的归一化口径一致）。
    haystack = _normalize_quote(render_content(content, modalities))
    scene_texts = {scene.scene_id: _normalize_quote(render_scene(scene, modalities))
                   for scene in content.content.scenes}
    scene_ids = frozenset(scene_texts)
    candidates_by_key: dict[str, dict] = {}
    targets: list[VerdictTarget] = []
    for candidate in candidates:
        key = candidate.get("candidate_id") or normalize_key(candidate["tag"])
        if candidate.get("scope") in ("scene", "excerpt"):
            level = "scene"
            allowed = frozenset(s for s in candidate.get("scenes") or []
                                if s in scene_ids)
        else:
            level = "film"      # scope 缺失按 film 兜底
            allowed = scene_ids
        targets.append(VerdictTarget(
            key=key, tag=candidate["tag"], level=level, allowed_scenes=allowed,
            tag_kind=_candidate_tag_kind(candidate, dir_index),
            has_desc=bool(candidate.get("desc"))))
        candidates_by_key[key] = candidate

    def build_messages(batch: list[VerdictTarget], attempt: int) -> list[dict]:
        batch_candidates = [candidates_by_key[target.key] for target in batch]
        dir_ids = dict.fromkeys(dir_id for row in batch_candidates
                                for dir_id in row.get("dir_ids", [])
                                if dir_id in dir_index)
        request = build_judge_messages(
            content, batch_candidates, include_dialogue=include_dialogue,
            dirs=[dir_index[dir_id] for dir_id in dir_ids])
        if attempt:
            request[0]["content"] += JUDGE_RETRY_HINT
        return request

    verdicts, errors = run_verdicts(
        client, targets, build_messages=build_messages, run_id=run_id,
        policy=VerdictPolicy(max_batch_size=_MAX_BATCH_SIZE),
        haystack=haystack, scene_texts=scene_texts, value_weights=value_weights,
        match_by_name=True, trace=trace, usage=usage)
    if errors:
        logger.warning("judge %d 条候选未取得判决：%s", len(errors),
                       "；".join(errors.values()))
    if not verdicts:
        raise JudgeError("；".join(dict.fromkeys(errors.values()))
                         or "judge 未返回任何有效判决")
    return verdicts
