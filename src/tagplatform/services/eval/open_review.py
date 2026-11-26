"""开放评测的整片盲评、证据核验与通过判定。"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from contextlib import nullcontext

from tagplatform.capabilities.prompts.open_review import (
    REVIEW_PROMPT, REVIEW_PROMPT_VERSION, REVIEW_RETRY_HINT,
)
from tagplatform.schemas import ContentJSON
from tagplatform.services.generation.open.mtg.prompts import render_content
from tagplatform.services.tags.value_scoring import (
    VALUE_DIRECTIONS,
    VALUE_DIRECTION_ALIASES,
    VALUE_DIRECTION_LABELS,
    aggregate_value_score,
    normalize_tag_kind,
    weights_dict_for_kind,
)
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
        "pass_mode": "model",
        "batch_mode": "movie",
    }


def review_movie_tags(
    client, content: ContentJSON, tag_rows: list[dict], *,
    run_id: str, modalities=("script", "dialogue"),
    request_slot=None, trace: dict | None = None,
    value_weights: Mapping[str, tuple[float, float, float]] | None = None,
) -> list[dict]:
    """整片评审，结构错误或截断时拆批，保留同片其余标签作上下文。

    整批格式失败可重试一次。拆分后的子批先各试一次，单条补试排在所有
    子批之后。最多 2 * 标签数 + 2 次调用，传输失败直接记录。
    标签价值由模型分别对内容呈现/内容搜索/内容推荐打分，再按 target_type
    对应的权重聚合为保留两位小数的 value.score，同时保留三方向明细分。
    """
    if not tag_rows:
        return []
    haystack = render_content(content, modalities=modalities)
    if not haystack.strip():
        raise ReviewRequestError("评审材料为空（modalities 未含 script）")
    scenes = {scene.scene_id for scene in content.content.scenes}
    labels = [{
        "id": f"t{index}",
        "name": row.get("name"),
        "desc": row.get("desc"),
        "level": row.get("level"),
        "target_type": normalize_tag_kind(
            row.get("target_type")
            or ("scene" if row.get("level") == "scene" else "content")
        ),
        "dir_path": row.get("dir_path") or row.get("dir_name"),
        "dir_description": row.get("dir_description"),
        "scene_ids": row.get("scene_ids") or [],
    } for index, row in enumerate(tag_rows)]
    results: dict[int, dict] = {}
    errors: dict[int, str] = {}
    budget = 2 * len(labels) + 2
    single_retries: list[dict] = []
    budget_error = "评审调用预算耗尽，未发出恢复请求"

    def batch(targets, *, attempt=0):
        nonlocal budget
        context = ""
        if len(targets) < len(labels):
            other_labels = [label for label in labels if label not in targets]
            context = "\n\n## 该片其他标签（上下文）\n" + json.dumps(other_labels, ensure_ascii=False)
        user = haystack + context + "\n\n## 待评估标签\n" + json.dumps(targets, ensure_ascii=False)
        target_ids = {int(label["id"][1:]) for label in targets}
        batch_id = f"{run_id}:{targets[0]['id']}-{targets[-1]['id']}"
        if budget <= 0:
            for index in target_ids:
                errors[index] = budget_error
            return

        budget -= 1
        # 恢复批次也带格式提醒，避免重复整批请求只为追加同一句话。
        messages = [{"role": "system", "content": REVIEW_PROMPT},
                    {"role": "user", "content": user + (
                        REVIEW_RETRY_HINT if attempt or targets is not labels else "")}]
        record = {"batch": batch_id, "attempt": attempt,
                  "targets": [label["id"] for label in targets], "raw": ""}
        if trace is not None:
            trace.setdefault("batches", []).append(record)
        accepted = {}
        recovery = False
        retry_format = False
        try:
            with request_slot if request_slot is not None else nullcontext():
                response = client.complete(messages, batch_id)
        except Exception as exc:  # noqa: BLE001
            error = f"评审调用失败：{exc}"
        else:
            response_text, ok, transport_error = _unwrap(response)
            record["raw"] = response_text
            if not ok:
                error = (f"影片评审材料超出模型上下文：{transport_error}"
                         if _input_limit(transport_error) else f"评审调用失败：{transport_error}")
            elif _truncated(response):
                error = "评审输出超出长度限制"
                recovery = len(targets) > 1
            else:
                try:
                    parsed = _parse_reviews(response_text, len(labels))
                except ValueError as exc:
                    error = f"评审响应无法解析：{exc}"
                else:
                    accepted = {i: verdict for i, verdict in parsed.items() if i in target_ids}
                    if len(accepted) == len(targets):
                        results.update(accepted)
                        for index in accepted:
                            errors.pop(index, None)
                        return
                    error = ("评审响应缺少目标判决" if accepted
                             else "评审响应未包含有效判决")
                recovery = retry_format = True
        missing = [label for label in targets if int(label["id"][1:]) not in accepted]
        record.update(error=error, missing=[label["id"] for label in missing])
        results.update(accepted)
        if recovery:
            if not accepted and targets is labels and attempt == 0 and retry_format:
                batch(targets, attempt=1)
                return
            if accepted:
                batch(missing)
                return
            if len(missing) > 1:
                middle = len(missing) // 2
                batch(missing[:middle])
                batch(missing[middle:])
                return
            if attempt == 0:
                single_retries.append(missing[0])
        for label in missing:
            errors[int(label["id"][1:])] = error

    batch(labels)
    for label in single_retries:
        batch([label], attempt=1)
    if trace is not None and errors:
        trace["errors"] = {f"t{index}": message for index, message in errors.items()}
    for index, row in enumerate(tag_rows):
        verdict = results.get(index)
        if verdict is None:
            error = errors[index]
            row.update({axis: {"score": None, "reason": error} for axis in _REVIEW_DIMENSIONS})
            row.update({"pass": None, "reason": error, "evidence_scene_ids": [], "evaluated": False})
        else:
            label = labels[index]
            allowed = scenes
            if label["level"] == "scene":
                allowed = scenes.intersection(label["scene_ids"])
            cited = allowed.intersection(verdict["evidence_scene_ids"])
            relevance = verdict["relevance"]
            minimum = {"scene": 1, "film": 2}.get(label["level"], 1)
            if relevance["score"] in (3, 4) and len(cited) < minimum:
                # Some responses cite the actual scene only in the written rationale.
                rationale = f"{relevance['reason']}\n{verdict['reason']}"
                cited.update(_scene_mentions(rationale, allowed))
                if len(cited) < minimum:
                    reason = "有效引用场景不足，无法核验所给相关性高分"
                    if relevance["reason"]:
                        reason += "。模型理由：" + relevance["reason"]
                    verdict["relevance"] = {"score": None, "reason": reason}
            verdict["evidence_scene_ids"] = sorted(cited)
            if not (label["desc"] or "").strip() and verdict["semantics"]["score"] not in (0, 1):
                verdict["semantics"] = {"score": None, "reason": "缺少释义"}
            raw_value = verdict["value"]
            tag_kind = label["target_type"]
            dims = raw_value["dimensions"]
            agg_score = aggregate_value_score(
                {d: dims[d]["score"] for d in VALUE_DIRECTIONS},
                tag_kind, weights=value_weights,
            )
            reason_text = raw_value.get("reason") or _summarize_value_reasons(dims)
            verdict["value"] = {
                "score": agg_score,
                "reason": reason_text,
                "tag_kind": tag_kind,
                "weights": weights_dict_for_kind(tag_kind, value_weights),
                "dimensions": dims,
            }
            row.update(verdict, evaluated=True)
    if not results:
        raise ReviewRequestError("；".join(dict.fromkeys(errors.values())))
    return tag_rows


def _scene_mentions(text: str, scene_ids: set[str]) -> set[str]:
    """Find complete, known scene IDs in a rationale without matching substrings."""
    if not scene_ids:
        return set()
    ids = "|".join(re.escape(scene_id) for scene_id in sorted(scene_ids, key=len, reverse=True))
    return set(re.findall(rf"(?<![A-Za-z0-9_-])(?:{ids})(?![A-Za-z0-9_-])", text))


def _parse_reviews(raw: str, total: int) -> dict[int, dict]:
    """按影片全局行序号解析三维分数、总评和引用场景。

    畸形家族容错：首尾整圈 ``` 围栏剥除（tag_quality 同款）、直读失败按
    首尾大括号抠对象（verify._loads 同款）、reviews 为按 id 索引的单
    对象时展平、裸列表容忍。维度对象缺 score 键 / 非法分值 / id 出界 /
    重复 id（镜像 tag_quality：重复即不可靠）的条目按漏判处理——不进
    结果，不抛错。显式 null 分有效（无法判断），与漏判不同。
    """
    text = raw.strip()
    first, separator, rest = text.partition("\n")
    if separator and first.strip().lower() in ("```json", "```") \
            and rest.rstrip().endswith("```"):
        text = rest.rstrip()[:-3].strip()
    data = _loads_with_prose_fallback(text)
    if isinstance(data, dict) and isinstance(data.get("reviews"), dict):
        items = [{"id": key, **value} for key, value in data["reviews"].items()
                 if isinstance(value, dict)]
    elif isinstance(data, dict) and isinstance(data.get("reviews"), list):
        items = [item for item in data["reviews"] if isinstance(item, dict)]
    elif isinstance(data, dict) and isinstance(data.get("tags"), list):
        # rubric 原输出形状（{"tags":[...]}）容忍：补充说明已要求 reviews，
        # 但模型照 rubric 末尾形状回也能解析。
        items = [item for item in data["tags"] if isinstance(item, dict)]
    elif isinstance(data, list):
        items = [item for item in data if isinstance(item, dict)]
    else:
        raise ValueError("评审响应缺少 reviews 列表")
    results: dict[int, dict] = {}
    seen: set[int] = set()
    for item in items:
        index = _row_index(item.get("id"), total)
        if index is None:
            continue
        if index in seen:
            results.pop(index, None)
            continue
        seen.add(index)
        dimensions: dict[str, dict] = {}
        for axis in ("relevance", "semantics"):
            if axis not in item:  # 缺维度键 = 未完成（显式 null 才是无法判断）
                break
            score, reason = _clean_dimension(item[axis])
            if score is _MISSING:
                break
            dimensions[axis] = {"score": score, "reason": reason}
        if len(dimensions) != 2:
            continue
        value_payload = _clean_value_block(item)
        if value_payload is _MISSING:
            continue
        dimensions["value"] = value_payload
        decision = item.get("pass")
        reason = item.get("reason")
        if ("pass" not in item or (decision is not None and not isinstance(decision, bool))
                or not isinstance(reason, str) or not reason.strip()):
            continue
        evidence = item.get("evidence_scene_ids")
        results[index] = {
            **dimensions,
            "pass": decision,
            "reason": reason.strip(),
            "evidence_scene_ids": ([scene_id for scene_id in evidence if isinstance(scene_id, str)]
                                   if isinstance(evidence, list) else []),
        }
    return results


# 维度形状非法的哨兵（与有效 null 分区分：null 是"无法判断"，非法是漏判）
_MISSING = object()


def _clean_dimension(value) -> tuple:
    """维度值 → (score, reason)；形状非法返回 (_MISSING, "")（整行按漏判）。

    容忍 {"score","reason"} 对象与裸分值两种形状；score 只认 0-4 的整数
    （含整数值浮点）或显式 null，布尔/越界/缺 score 键都算未完成。
    """
    if isinstance(value, dict):
        if "score" not in value:
            return _MISSING, ""
        score = value["score"]
        reason = value.get("reason")
        reason = reason.strip() if isinstance(reason, str) else ""
    else:
        score, reason = value, ""
    if score is None:
        return None, reason
    if isinstance(score, bool):
        return _MISSING, ""
    if isinstance(score, float) and score.is_integer():
        score = int(score)
    if isinstance(score, int) and 0 <= score <= 4:
        return score, reason
    return _MISSING, ""


def _clean_value_block(item: dict):
    """解析 value 三方向（presentation / search / recommendation）评分对象。

    接受 `item["value"]` 内含三方向对象（或 `item["value_scores"]` 兜底），
    也容忍中文方向键名；任一方向缺失或分值非法返回 `_MISSING`（按漏判触发重试）。
    """
    raw = item.get("value")
    if not isinstance(raw, dict):
        raw = item.get("value_scores")
    if not isinstance(raw, dict):
        return _MISSING
    normalized: dict[str, object] = {}
    for key, val in raw.items():
        canonical = (
            VALUE_DIRECTION_ALIASES.get(str(key).strip().lower())
            or VALUE_DIRECTION_ALIASES.get(str(key).strip())
        )
        if canonical and canonical not in normalized:
            normalized[canonical] = val
    dims: dict[str, dict] = {}
    for direction in VALUE_DIRECTIONS:
        if direction not in normalized:
            return _MISSING
        score, reason = _clean_dimension(normalized[direction])
        if score is _MISSING:
            return _MISSING
        dims[direction] = {"score": score, "reason": reason}
    summary_reason = raw.get("reason")
    summary_reason = summary_reason.strip() if isinstance(summary_reason, str) else ""
    return {"dimensions": dims, "reason": summary_reason}


def _summarize_value_reasons(dims: dict[str, dict]) -> str:
    """将三个价值方向的得分与理由汇总为单句可读文案。"""
    parts = []
    for direction in VALUE_DIRECTIONS:
        entry = dims[direction]
        label = VALUE_DIRECTION_LABELS[direction]
        score = "null" if entry["score"] is None else str(entry["score"])
        reason = entry.get("reason") or ""
        parts.append(f"{label}({score})：{reason}" if reason else f"{label}({score})")
    return "；".join(parts)


def _loads_with_prose_fallback(text: str):
    """json.loads 直读失败时按首尾大括号抠对象（verify._loads 同款容错）。"""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("评审响应中没有 JSON 对象")
    return json.loads(text[start:end + 1])


def _row_index(value, total: int) -> int | None:
    """响应 id → 批次内行序号：容忍 "t3" / 3 / "3"；出界或不可解析 → None。"""
    text = str(value).strip().lower()
    if text.startswith("t"):
        text = text[1:]
    if not text.isascii() or not text.isdecimal() or len(text) > len(str(total)):
        return None
    index = int(text)
    return index if index < total else None


def _unwrap(response) -> tuple[str, bool, str]:
    """评审客户端响应归一：真客户端 / mock 回 LLMResponse，简化替身回文本。

    返回 (text, ok, error)：纯文本替身按成功处理（测试替身契约的最小
    形状 .complete(messages, run_id) -> str）；LLMResponse 失败时给
    raw.error 错误串。
    """
    if isinstance(response, str):
        return response, True, ""
    ok = bool(getattr(response, "ok", True))
    if not ok:
        raw = getattr(response, "raw", None) or {}
        return "", False, str(raw.get("error") or "模型未返回成功响应")
    return getattr(response, "content", None) or "", True, ""


def _truncated(response) -> bool:
    """finish_reason == "length" 判定；输出截断同样走切分（镜像 tag_quality）。

    ok=False 信封不判截断（失败请求的 raw 里即便带着 length 也要走
    「评审调用失败」口径，tag_quality 同款 `response.ok and ...` 门）。
    纯文本替身没有 raw 信封，恒 False。
    """
    if not getattr(response, "ok", True):
        return False
    raw = getattr(response, "raw", None) or {}
    choices = raw.get("choices") or [{}]
    return choices[0].get("finish_reason") == "length"


def _input_limit(error: str) -> bool:
    """输入超长判别式（照抄 tag_quality 的判别式，本地实现——不跨模块
    import 私有函数）。"""
    return any(term in error.lower() for term in (
        "context_length_exceeded", "maximum context length", "context window",
        "input is too long", "input length", "payload too large", "request too large",
        "too many images", "413 request", "上下文长度", "输入过长",
    ))
