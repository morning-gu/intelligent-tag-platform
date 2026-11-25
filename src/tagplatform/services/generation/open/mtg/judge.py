"""judge 审核：逐候选做三维结果导向评分（relevance/semantics/value，0-4）。

候选分批送审，逐条返回三维分。语义边界：
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

_MAX_BATCH_SIZE = 20


def build_judge_template(*, with_candidate_ids: bool = True) -> dict:
    marks = "目录 {{目录 ID}}"
    if with_candidate_ids:
        marks = "excerpt｜依据场景 {{来源场景 ID}}｜" + marks + "｜候选 ID {{候选 ID}}"
    line = candidate_line("{{候选标签}}", "{{维度}}",
                          f"〔{marks}〕{{{{生成侧的简短理由}}}}")
    dirs = [{"dir_id": "{{目录 ID}}", "path": "{{目录全路径}}",
             "description": "{{目录定义}}"}]
    return {
        "messages": render_judge_messages(
            "{{影片资料与按场景组织的内容文本}}", line, dir_block=judge_dir_block(dirs),
            with_candidate_ids=with_candidate_ids, with_excerpts=with_candidate_ids),
        "retry_suffix": JUDGE_RETRY_HINT,
    }


class JudgeError(RuntimeError):
    """judge 调用或解析失败（任务应置 failed，不得静默放行）。"""


def _map_items(items: list[dict], candidates: list[dict]) -> dict[str, dict]:
    """判决项 → {候选归一化标签: 判决}：精确优先，其次唯一前缀匹配。"""
    tags_by_id = {c["candidate_id"]: c["tag"] for c in candidates if c.get("candidate_id")}
    tags_by_key = {normalize_key(c["tag"]): c["tag"] for c in candidates
                   if not c.get("candidate_id") and normalize_key(c["tag"])}
    mapped: dict[str, dict] = {}
    leftovers: list[tuple[str, dict]] = []
    for item in items:
        candidate_id = item.get("candidate_id")
        if candidate_id:
            if candidate_id in tags_by_id:
                item["tag"] = tags_by_id[candidate_id]
                mapped.setdefault(candidate_id, item)
            continue
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
        if key in tags_by_key:
            item["tag"] = tags_by_key[key]
    return mapped


def judge_candidates(client, content: ContentJSON, candidates: list[dict],
                     *, run_id: str, retries: int = 1,
                     include_dialogue: bool = True,
                     usage: dict | None = None,
                     trace: dict | None = None,
                     dirs: list[dict] | None = None) -> dict[str, dict]:
    """候选 → {候选 ID 或归一化标签名: 判决 dict}。

    usage 传入 dict 时累计本阶段的调用数与 tokens（审计报告用）；
    trace 传入 dict 时记录逐次原始响应（`raw`）、解析错误（`errors`）与
    末轮兜底保住的判决条数（`salvaged`）——审计据此还原异常形态。
    目录契合按候选及其目标目录校验。目录漏判时仍保留已有的三维评分。
    """
    verdicts: dict[str, dict] = {}
    errors: list[JudgeError] = []
    for start in range(0, len(candidates), _MAX_BATCH_SIZE):
        batch = candidates[start:start + _MAX_BATCH_SIZE]
        batch_run_id = (run_id if len(candidates) <= _MAX_BATCH_SIZE
                        else f"{run_id}:batch-{start // _MAX_BATCH_SIZE + 1:03d}")
        try:
            verdicts.update(_judge_batch(
                client, content, batch, run_id=batch_run_id, retries=retries,
                include_dialogue=include_dialogue, usage=usage, trace=trace, dirs=dirs))
        except JudgeError as exc:
            errors.append(exc)
            logger.warning("judge 批次 %s 未取得有效判决：%s", batch_run_id, exc)
    if errors and not verdicts:
        raise errors[0]
    return verdicts


def _judge_batch(client, content, candidates, *, run_id, retries, include_dialogue,
                 usage, trace, dirs) -> dict[str, dict]:
    pending = {row.get("candidate_id") or normalize_key(row["tag"]): row
               for row in candidates}
    trace_offset = len(trace.get("raw", [])) if trace is not None else 0
    verdicts: dict[str, dict] = {}
    dir_index = {entry["dir_id"]: entry for entry in (dirs or [])}
    last_error = ""
    last_raw = ""
    for attempt in range(retries + 1):
        dir_ids = dict.fromkeys(dir_id for row in pending.values()
                                for dir_id in row.get("dir_ids", []))
        request = build_judge_messages(
            content, list(pending.values()), include_dialogue=include_dialogue,
            dirs=[dir_index[dir_id] for dir_id in dir_ids])
        if attempt:
            request[0]["content"] += JUDGE_RETRY_HINT
        response = client.complete(request, f"{run_id}:judge{'' if attempt == 0 else attempt}")
        if usage is not None:
            accumulate_usage(usage, response.usage)
        raw_text = response.content or ""
        last_raw = raw_text[:400]
        if trace is not None:
            trace.setdefault("raw", []).append(
                {"attempt": trace_offset + attempt, "content": raw_text})
        problems = []
        if response.ok:
            try:
                items = parse_judge_items(raw_text)
            except MTGParseError as exc:
                items = exc.items
                problems.append(str(exc))
            for key, item in _map_items(items, candidates).items():
                if key not in pending:
                    continue
                expected = pending[key].get("dir_ids", [])
                reported = item.pop("dir_fits")
                verdict = verdicts.setdefault(key, item)
                fits = verdict.setdefault("dir_fits", {})
                if isinstance(reported, dict):
                    for dir_id in expected:
                        decision = reported.get(dir_id)
                        if isinstance(decision, bool):
                            fits.setdefault(dir_id, decision)
                missing_dirs = [dir_id for dir_id in expected if dir_id not in fits]
                if missing_dirs:
                    problems.append(f"{item['tag']}：缺少有效目录判决 " + "、".join(missing_dirs))
                    pending[key] = {**pending[key], "dir_ids": missing_dirs}
                else:
                    pending.pop(key)
        else:
            problems.append(f"judge 调用失败: {response.raw.get('error')}")
        missing_tags = [row["tag"] for key, row in pending.items() if key not in verdicts]
        if missing_tags:
            problems.append("缺少有效判决：" + "、".join(missing_tags))
        if problems:
            last_error = "；".join(problems)
            if trace is not None:
                trace.setdefault("errors", []).append(
                    {"attempt": trace_offset + attempt, "error": last_error})
        if not pending or not response.ok:
            break
    if pending:
        if not verdicts:
            raise JudgeError(f"{last_error}；原始输出前 400 字: {last_raw!r}")
        if trace is not None:
            trace["salvaged"] = trace.get("salvaged", 0) + len(verdicts)
        logger.warning("judge 部分候选未判，保留 %d 条合格判决：%s", len(verdicts), last_error)
    for verdict in verdicts.values():
        if not verdict["dir_fits"]:
            verdict.pop("dir_fits")
    return verdicts
