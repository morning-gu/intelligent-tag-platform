"""judge 审核：逐候选判定内容支持度（移植自 MovieTagGen 06 的 audit/judge）。

一次调用判完全部候选（控制成本），输出逐条 verdict。语义边界：
- 只判"内容是否支持这个标签"，不判标签好不好听；
- 拿不到证据就判 insufficient（保守），不自动判 supported；
- 解析失败重试一次（追加一句更强的格式要求），仍失败抛 JudgeError ——
  由上层把任务置 failed，绝不"审核失败就当全过"。

判决与候选的对齐是这里最容易出错的一环：真实调用里 judge 常把整行候选
（`标签｜维度｜依据`）原样抄进 `tag` 字段。因此 `parse` 先按分隔符截断，
本模块再做两级匹配：归一化精确匹配 → 唯一前缀匹配（防止近义标签错配）。
一个候选只认一条判决，漏判的由上层淘汰。

judge 客户端与生成客户端分离：生成用 qwen3.7-plus，judge 用
`settings.mtg_judge_model`（deepseek-v4.1-flash），temperature=0。
"""
from __future__ import annotations

from tagplatform.capabilities.llm.usage import accumulate_usage
from tagplatform.capabilities.prompts.mtg import JUDGE_RETRY_HINT
from tagplatform.services.generation.open.mtg.normalization import normalize_key
from tagplatform.services.generation.open.mtg.parse import MTGParseError, parse_judge_items
from tagplatform.services.generation.open.mtg.prompts import (
    build_judge_messages, candidate_line, render_judge_messages,
)
from tagplatform.schemas import ContentJSON


def build_judge_template() -> dict:
    line = candidate_line("{{候选标签}}", "{{维度}}", "{{生成侧的简短理由}}")
    return {
        "messages": render_judge_messages("{{影片资料与按场景组织的内容文本}}", line),
        "retry_suffix": JUDGE_RETRY_HINT,
    }


class JudgeError(RuntimeError):
    """judge 调用或解析失败（任务应置 failed，不得静默放行）。"""


def _map_items(items: list[dict], candidates: list[dict]) -> dict[str, dict]:
    """判决项 → {候选归一化标签: 判决}：精确优先，其次唯一前缀匹配。"""
    tags_by_key = {normalize_key(c["tag"]): c["tag"]
                   for c in candidates if normalize_key(c["tag"])}
    mapped: dict[str, dict] = {}
    leftovers: list[tuple[str, dict]] = []
    for item in items:
        key = normalize_key(item["tag"])
        if key in tags_by_key and key not in mapped:
            mapped[key] = item
        else:
            leftovers.append((key, item))
    for key, item in leftovers:
        if not key:
            continue
        hits = [candidate_key for candidate_key in tags_by_key
                if key.startswith(candidate_key) or candidate_key.startswith(key)]
        if len(hits) == 1 and hits[0] not in mapped:
            mapped[hits[0]] = item
    for key, item in mapped.items():
        item["tag"] = tags_by_key[key]
    return mapped


def judge_candidates(client, content: ContentJSON, candidates: list[dict],
                     *, run_id: str, retries: int = 1,
                     include_dialogue: bool = True,
                     usage: dict | None = None) -> dict[str, dict]:
    """候选 → {归一化标签名: 判决 dict}（未返回的候选由上层按未判处理）。

    usage 传入 dict 时累计本阶段的调用数与 tokens（审计报告用）。
    """
    if not candidates:
        return {}
    messages = build_judge_messages(content, candidates, include_dialogue=include_dialogue)
    last_error: Exception | None = None
    last_raw = ""
    for attempt in range(retries + 1):
        request = messages if attempt == 0 else [
            {"role": "user",
             "content": messages[0]["content"] + JUDGE_RETRY_HINT}]
        response = client.complete(request, f"{run_id}:judge{'' if attempt == 0 else attempt}")
        if usage is not None:
            accumulate_usage(usage, response.usage)
        if not response.ok:
            raise JudgeError(f"judge 调用失败: {response.raw.get('error')}")
        try:
            items = parse_judge_items(response.content)
        except MTGParseError as exc:
            last_error = exc
            last_raw = (response.content or "")[:400]
            continue
        return _map_items(items, candidates)
    raise JudgeError(f"judge 输出无法解析: {last_error}；原始输出前 400 字: {last_raw!r}")
