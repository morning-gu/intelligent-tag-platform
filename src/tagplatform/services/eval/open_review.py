"""开放评测的整片盲评：统一判决引擎（mtg.verdicts）的评审侧薄封装（Task 7）。

与生产 Judge 共用同一解析器（parse_verdicts）、同一证据核验终判
（finalize_verdict：quote 逐字核验 + 场景计数压 1）与同一批循环
（run_verdicts），分歧只剩策略参数（评审：整批一队、漏判拆批恢复、
单条复试排子批之后）与裁决复算（verdict_pass 阈值——模型只输出评分与
证据，是否保留由程序复算，不再收 pass/总评）。

请求形状（与 llm/mock._quality_output 咬合，逐字保留 legacy）：user 消息 =
渲染后整片材料 + "\\n\\n## 待评估标签\\n" + 待评估标签视图 JSON（无
「该片其他标签（上下文）」块）；system 消息 = REVIEW_PROMPT（v14）。证据
核验基准 = 归一化文本（haystack/scene_texts，与模型看到原文分离），与
judge 同口径。

行回填：成功行 = 终判各键 + evidence_scene_ids（= scenes）+ pass（复算）+
evaluated；失败行 = 三维 {score: None, reason: 错误串}、pass None、
evaluated False（open_service 行投影消费的现契约）。零终判时抛
ReviewRequestError（运行器按「该片评审失败」处理，None≠0 不判 0）。
"""
from __future__ import annotations

import json
from collections.abc import Mapping

from tagplatform.capabilities.prompts.open_review import (
    REVIEW_PROMPT, REVIEW_PROMPT_VERSION, REVIEW_RETRY_HINT,
)
from tagplatform.schemas import ContentJSON
from tagplatform.services.generation.open.mtg.prompts import (
    _active, render_content, render_scene,
)
from tagplatform.services.generation.open.mtg.verdicts import (
    VerdictPolicy, VerdictTarget, _normalize_quote, run_verdicts, verdict_pass,
)
from tagplatform.services.tags.value_scoring import normalize_tag_kind
from tagplatform.settings import Settings

# REVIEW_PROMPT_VERSION 从 capabilities/prompts.open_review 同驻 re-export：
# canonical 在 prompt 常量模块（文案与版本同驻），这里转出口给运行器记账
# （meta.review.prompt_version），import 路径与 make_review_client 对齐。
__all__ = [
    "REVIEW_PROMPT_VERSION",
    "ReviewRequestError",
    "make_review_client",
    "review_calibre",
    "review_movie_tags",
]


_REVIEW_DIMENSIONS = ("relevance", "semantics", "value")


class ReviewRequestError(RuntimeError):
    """评审调用失败（请求失败 / 格式两连败 / 全部漏判）。

    上层（运行器）按「该片评审失败」处理：produce 产出完整保留、该片
    全部标签不计入通过率/标定（None≠0 纪律），绝不判 0。
    """


def make_review_client(settings: Settings, llm):
    """真实评审使用独立客户端，默认沿用主端点和模型。"""
    if settings.use_mock_llm or getattr(llm, "zero_network", False):
        return llm
    from tagplatform.capabilities.llm import OpenAIClient

    extra_body = dict(settings.llm_extra_body or {}) if not settings.open_eval_review_model else {}
    extra_body.pop("reasoning_effort", None)
    if settings.open_eval_review_reasoning_effort:
        extra_body["reasoning_effort"] = settings.open_eval_review_reasoning_effort
    return OpenAIClient(
        model=settings.open_eval_review_model or settings.llm_model,
        base_url_env=settings.open_eval_review_base_url_env or settings.llm_base_url_env,
        api_key_env=settings.open_eval_review_api_key_env or settings.llm_api_key_env,
        temperature=0.0,
        request_interval=settings.llm_request_interval,
        max_tokens=settings.open_eval_review_max_tokens,
        extra_body=extra_body,
    )


def review_calibre(settings: Settings, llm, client=None) -> dict:
    client = client if client is not None else make_review_client(settings, llm)
    model = getattr(client, "model", settings.open_eval_review_model or settings.llm_model)
    return {
        "model": model,
        "same_model": model == getattr(llm, "model", settings.llm_model),
        "reasoning_effort": (getattr(client, "extra_body", None) or {}).get("reasoning_effort"),
        "max_tokens": getattr(client, "max_tokens", None),
        "max_concurrent_requests": settings.open_eval_review_concurrency,
        # pass 由 verdict_pass 阈值复算（评审归一），不再是模型保留意见。
        "pass_mode": "threshold",
        "batch_mode": "movie",
    }


def review_movie_tags(
    client, content: ContentJSON, tag_rows: list[dict], *,
    run_id: str, modalities=("script", "dialogue"),
    request_slot=None, trace: dict | None = None,
    value_weights: Mapping[str, tuple[float, float, float]] | None = None,
) -> list[dict]:
    """整片盲评（接 `mtg.verdicts.run_verdicts` 统一引擎）。

    批编排/解析/证据核验/价值聚合全部由引擎承载：整批一队、漏判拆批恢复
    （已判不重发、零命中对半拆）、单条复试排所有子批之后（一次机会）、
    预算 2 * 标签数 + 2。模型只输出评分与证据，`pass` 由
    `verdict_pass`（rel≥3/sem≥3/val≥2）复算；semantics 缺释义置 null 仅
    评测侧生效。标签价值由模型对内容呈现/内容搜索/内容推荐三方向打分，
    引擎按 target_type 权重聚合为保留两位小数的 value.score。
    """
    if not tag_rows:
        return []
    active = _active(modalities)
    material = render_content(content, active)
    if not material.strip():
        raise ReviewRequestError("评审材料为空（modalities 未含 script）")
    # 核验基准（引擎消费）与模型材料分离：归一化去标点空白，quote 逐字比对。
    haystack = _normalize_quote(material)
    scene_texts = {scene.scene_id: _normalize_quote(render_scene(scene, active))
                   for scene in content.content.scenes}
    scene_ids = frozenset(scene_texts)

    labels: dict[str, dict] = {}
    targets: list[VerdictTarget] = []
    for index, row in enumerate(tag_rows):
        key = f"t{index}"
        level = "scene" if row.get("level") == "scene" else "film"
        tag_kind = normalize_tag_kind(
            row.get("target_type")
            or ("scene" if level == "scene" else "content")
        )
        targets.append(VerdictTarget(
            key=key, tag=row.get("name"), level=level,
            allowed_scenes=(scene_ids if level == "film"
                            else frozenset(scene_ids.intersection(row.get("scene_ids") or []))),
            tag_kind=tag_kind, has_desc=bool(row.get("desc")),
            id_aliases=(str(index),)))
        labels[key] = {
            "id": key,
            "name": row.get("name"),
            "desc": row.get("desc"),
            "level": row.get("level"),
            "target_type": tag_kind,
            "dir_path": row.get("dir_path") or row.get("dir_name"),
            "dir_description": row.get("dir_description"),
            "scene_ids": row.get("scene_ids") or [],
        }

    total = len(targets)

    def build_messages(batch: list[VerdictTarget], attempt: int) -> list[dict]:
        batch_labels = [labels[target.key] for target in batch]
        user = (material + "\n\n## 待评估标签\n"
                + json.dumps(batch_labels, ensure_ascii=False))
        # 恢复批次也带格式提醒；派生子批（整批之外的请求）在 attempt 0 同样
        # 附 hint——legacy「targets is not labels」语义，按批大小推断派生性。
        if attempt or len(batch) < total:
            user += REVIEW_RETRY_HINT
        return [{"role": "system", "content": REVIEW_PROMPT},
                {"role": "user", "content": user}]

    verdicts, errors = run_verdicts(
        client, targets, build_messages=build_messages, run_id=run_id,
        policy=VerdictPolicy(split_on_missing=True, defer_single_retries=True),
        haystack=haystack, scene_texts=scene_texts, value_weights=value_weights,
        semantics_null_without_desc=True, trace=trace, request_slot=request_slot)

    for index, row in enumerate(tag_rows):
        verdict = verdicts.get(f"t{index}")
        if verdict is None:
            error = errors[f"t{index}"]
            row.update({axis: {"score": None, "reason": error} for axis in _REVIEW_DIMENSIONS})
            row.update({"pass": None, "reason": error, "evidence_scene_ids": [],
                        "evaluated": False})
        else:
            row.update({
                "relevance": verdict["relevance"],
                "semantics": verdict["semantics"],
                "value": verdict["value"],
                "quote": verdict["quote"],
                "scenes": verdict["scenes"],
                "confidence": verdict["confidence"],
                "reason": verdict["reason"],
                "evidence_scene_ids": verdict["scenes"],
                "pass": verdict_pass(verdict["relevance"]["score"],
                                     verdict["semantics"]["score"],
                                     verdict["value"]["score"]),
                "evaluated": True,
            })
    if not verdicts:
        raise ReviewRequestError("；".join(dict.fromkeys(errors.values())))
    return tag_rows
