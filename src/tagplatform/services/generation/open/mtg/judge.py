"""judge 审核：逐候选做三维结果导向评分（relevance/semantics/value，0-4）。

一次调用判完全部候选（控制成本），输出逐条三维分。语义边界：
- 评标签的实际效果——内容支持多准确、名称是否清晰、对检索浏览有多大
  作用，不评标签好不好听；释义此时尚未补写，semantics 只评名称本身；
- 内容证据不足时 relevance 可填 null；semantics 和 value 评名称，给 0-4 整数。
- 对齐候选后检查判决覆盖，重试未判候选。保留各轮已对齐的合法判决，全部
  未判才抛 JudgeError。

每次调用的原始响应都写进 `trace`（上层落审计 JSON）：整批归零时能分清
到底是模型显式 null、漏字段，还是响应格式漂移。

判决与候选的对齐是这里最容易出错的一环：真实调用里 judge 常把整行候选
（`标签｜维度｜依据`）原样抄进 `tag` 字段。因此 `parse` 先按分隔符截断，
本模块再做两级匹配：归一化精确匹配 → 唯一前缀匹配（防止近义标签错配）。
一个候选只认一条判决，漏判的由上层淘汰。

judge 客户端与生成客户端分离：生成用 qwen3.7-plus，judge 用
`settings.mtg_judge_model`（deepseek-v4.1-flash），temperature=0。
"""
from __future__ import annotations

import logging

from tagplatform.capabilities.llm.usage import accumulate_usage
from tagplatform.capabilities.prompts.mtg import JUDGE_RETRY_HINT
from tagplatform.services.generation.open.mtg.normalization import normalize_key
from tagplatform.services.generation.open.mtg.parse import MTGParseError, parse_judge_items
from tagplatform.services.generation.open.mtg.prompts import (
    build_judge_messages, candidate_line, judge_dir_block, render_judge_messages,
)
from tagplatform.schemas import ContentJSON


logger = logging.getLogger(__name__)


def build_judge_template() -> dict:
    line = candidate_line("{{候选标签}}", "{{维度}}", "{{生成侧的简短理由}}")
    dirs = [{"dir_id": "{{目录 ID}}", "path": "{{目录全路径}}",
             "description": "{{目录定义}}"}]
    return {
        "messages": render_judge_messages("{{影片资料与按场景组织的内容文本}}", line,
                                          dir_block=judge_dir_block(dirs)),
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
        if key in tags_by_key:
            mapped.setdefault(key, item)
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
                     usage: dict | None = None,
                     trace: dict | None = None,
                     dirs: list[dict] | None = None) -> dict[str, dict]:
    """候选 → {归一化标签名: 判决 dict}（未返回的候选由上层按未判处理）。

    usage 传入 dict 时累计本阶段的调用数与 tokens（审计报告用）；
    trace 传入 dict 时记录逐次原始响应（`raw`）、解析错误（`errors`）与
    末轮兜底保住的判决条数（`salvaged`）——审计据此还原异常形态。
    dirs 非空时要求逐条判目录契合（in_dir），漏给按协议错误重试。
    """
    if not candidates:
        return {}
    pending = {normalize_key(row["tag"]): row for row in candidates}
    verdicts: dict[str, dict] = {}
    last_error = ""
    last_raw = ""
    for attempt in range(retries + 1):
        request = build_judge_messages(
            content, list(pending.values()), include_dialogue=include_dialogue, dirs=dirs)
        if attempt:
            request[0]["content"] += JUDGE_RETRY_HINT
        response = client.complete(request, f"{run_id}:judge{'' if attempt == 0 else attempt}")
        if usage is not None:
            accumulate_usage(usage, response.usage)
        raw_text = response.content or ""
        last_raw = raw_text[:400]
        if trace is not None:
            trace.setdefault("raw", []).append({"attempt": attempt, "content": raw_text})
        problems = []
        if response.ok:
            try:
                items = parse_judge_items(raw_text, require_dir_fit=bool(dirs))
            except MTGParseError as exc:
                items = exc.items
                problems.append(str(exc))
            for key, item in _map_items(items, candidates).items():
                if key in pending:
                    verdicts[key] = item
                    pending.pop(key)
        else:
            problems.append(f"judge 调用失败: {response.raw.get('error')}")
        if pending:
            problems.append("缺少有效判决：" + "、".join(row["tag"] for row in pending.values()))
        if problems:
            last_error = "；".join(problems)
            if trace is not None:
                trace.setdefault("errors", []).append({"attempt": attempt, "error": last_error})
        if not pending or not response.ok:
            break
    if pending:
        if not verdicts:
            raise JudgeError(f"{last_error}；原始输出前 400 字: {last_raw!r}")
        if trace is not None:
            trace["salvaged"] = len(verdicts)
        logger.warning("judge 部分候选未判，保留 %d 条合格判决：%s", len(verdicts), last_error)
    return verdicts
