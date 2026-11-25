"""closed 产出标签的置信度计算（阶段 2.5，2026-12）。

公式（用户口径，权重/参数走 Settings）：
- Craw = w1·Ccons + w2·Cver + w3·Cmod（默认 0.45/0.30/0.25，和归一化）
- Ccons（评分一致性）：对每个最终标签**再评 2 次**（与原判定共 3 次），
  分数口径 = Sevi 融合分（0–1，三模态 evidence_strength 加权归一化）；
  As = 三次分数的总体方差（÷3，ddof=0）；Ccons = exp(−As/b)，b 默认 0.2。
- Cver（证据可验性）：LLM 按五级核验表（1=无法核验 … 5=完全可核验）
  对"标签 + 证据条目"打 1–5 分，Cver = (S−1)/4。
- Cmod（跨模态一致性）：s_m = (evidence_strength_m − 1)/4（归一化标尺），
  dij = |si − sj|，Cs = 1 − (d12 + d13 + d23)/3，Cc = max(dij)，
  Cmod = j·Cs + (1−j)·(1−Cc)，j 默认 0.5。本地计算。

三个分量的取材（与 producer 判定口径一一对应）：
- Ccons 重评：复用 closed 生成契约——`spec.prompt_text + "\n\n##
  候选标签分组\n" + 组 JSON`（单标签成组），同 run_id 调 2 次，输出经
  parse_closed_output 解析，每次按 evidence_scores 算 Sevi。Mock 端哈希
  键含 run_id，同 run_id 重调 = 相同输出 → As = 0 → Ccons = 1.0。
- Cver 核验：`spec.prompt_text + "\n\n" + CLOSED_VERIFICATION_PROMPT`
  （五级表在模板常量里），Cver 分支要求模型输出小 JSON {"score": N}；
  有帧时核验消息带与生成时相同的帧 parts（多模态对照）。
- Cmod：读 ProducedTag.evidence_scores（producer v8 带出的三模态原始
  强度 1–5），纯本地算术。

失败语义（用户拍板：降级，不落任务 failed）：
- Ccons 重评失败（LLM/解析）→ 用原判定的 Sevi（tag.confidence）补位
  视为"无波动"，报告标注 partial；
- Cver 失败 → 占位 0.5 + partial；
- Cmod 缺 evidence_scores → 占位 0.5 + partial；
- 报告写盘失败 → log warning，不阻断任务；
- 本模块入口 `apply_confidence()` 永不抛错（内部兜底 try/except），
  Craw 算不出时保持 producer 原值不动。

报告：`closed_confidence_report_dir`（默认 data/audit/confidence）下
`{资产名清洗}_{时间戳}.md`，每个标签一节：Ccons 三次分数 + 方差表、
Cver 证据 + 评分表、Cmod 中间计算表（s1/s2/s3、d12/d13/d23、Cs/Cc），
partial 环节显式标注。资产名取 asset.title。

分层红线：本模块不得 import fastapi（设计文档 §9.4）。
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from tagplatform.capabilities.prompts.templates import (
    CLOSED_VERIFICATION_PROMPT,
)
from tagplatform.services.generation.base import Evidence, GenerationSpec, ProducedTag
from tagplatform.services.generation.closed.parse import (
    LLMParseError,
    parse_closed_output,
)
from tagplatform.settings import get_settings

logger = logging.getLogger(__name__)

# Cver 小 JSON 的解析宽容度：容忍 ```json 围栏与键名漂移（"score"/"分数"/
# "rating"），分值钳到 1–5。与 parse_closed_output 的围栏剥离口径一致。
_VERIFY_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")
_VERIFY_KEYS = ("score", "分数", "rating", "分值")


@dataclass
class TagConfidence:
    """单个标签的置信度计算全量中间结果（报告的数据源）。"""

    tag_name: str
    tag_id: str | None = None
    # Ccons：三次 Sevi 融合分（第 1 次为原判定，第 2/3 次为重评）+ 方差
    sevi_scores: list[float] = field(default_factory=list)
    variance: float | None = None
    c_cons: float | None = None
    cons_partial: bool = False   # 重评失败 → 原分补位（视为无波动）
    # Cver：LLM 核验分（1–5 原始 + 归一化）
    verify_score: int | None = None
    c_ver: float | None = None
    ver_partial: bool = False    # 核验失败 → 0.5 占位
    # Cmod：三模态原始强度与中间量
    modality_scores: dict[str, int] = field(default_factory=dict)
    s_values: dict[str, float] = field(default_factory=dict)
    pairwise_d: dict[str, float] = field(default_factory=dict)
    c_s: float | None = None
    c_c: float | None = None
    c_mod: float | None = None
    mod_partial: bool = False    # 缺 evidence_scores → 0.5 占位
    # 总分
    c_raw: float | None = None


def _sanitize_name(name: str) -> str:
    """资产名 → 可作文件名的安全片段（去路径分隔符与 Windows 保留字符）。"""
    cleaned = re.sub(r'[\\/:*?"<>|\s]+', "_", (name or "asset").strip())
    return cleaned[:80] or "asset"


def _sevi_from_scores(scores: dict[str, int], weights: tuple[float, float, float]) -> float:
    """三模态 1–5 强度 → Sevi 融合分（0–1），与 producer 口径一致。"""
    s_content = (scores.get("content", 1) - 1) / 4
    s_scene = (scores.get("scene", 1) - 1) / 4
    s_frame = (scores.get("frame", 1) - 1) / 4
    return (weights[0] * s_content + weights[1] * s_scene + weights[2] * s_frame)


def _parse_verify_score(raw: str) -> int:
    """Cver 输出 → 1–5 分。剥围栏、找对象、容忍键名漂移。"""
    text = raw.strip()
    if text.startswith("```"):
        text = _VERIFY_FENCE_RE.sub("", text).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise LLMParseError(f"核验输出不是 JSON 对象: {raw[:200]!r}")
    try:
        obj = json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        raise LLMParseError(f"核验输出解析失败: {exc}") from exc
    if not isinstance(obj, dict):
        raise LLMParseError(f"核验输出顶层不是对象: {raw[:200]!r}")
    for key in _VERIFY_KEYS:
        if key in obj:
            try:
                value = int(obj[key])
            except (TypeError, ValueError) as exc:
                raise LLMParseError(
                    f"核验分值不是整数: {obj.get(key)!r}") from exc
            return min(max(value, 1), 5)
    raise LLMParseError(f"核验输出缺少 score 键: {raw[:200]!r}")


def _build_group_payload(spec: GenerationSpec, tag: ProducedTag,
                         ) -> dict:
    """单标签重建 closed 组 JSON（与 producer 拼法同构）。

    组对象：{"dir_id", "dir_name", "dir_description", "tags": [tag 定义]}；
    目录与标签定义从 spec.library 反查（producer 同款访问器）。
    """
    library = spec.library
    directory = library.directory(tag.dir_id) if (library and tag.dir_id) else None
    definition = library.tag(tag.tag_id) if (library and tag.tag_id) else None
    tag_entry = {
        "tag_id": tag.tag_id or "",
        "tag_name": tag.tag_name,
        "description": (definition.description if definition
                        else (tag.candidate_description or "")),
    }
    return {
        "dir_id": tag.dir_id or "",
        "dir_name": directory.dir_name if directory else "",
        "dir_description": directory.description if directory else "",
        "tags": [tag_entry],
    }


def _reconsistency_scores(
    spec: GenerationSpec,
    tag: ProducedTag,
    llm,
    evi_weights: tuple[float, float, float],
) -> list[float]:
    """Ccons 重评：同 run_id 再调 2 次单标签组判定，取每次的 Sevi。

    复用 closed 生成契约（组 JSON 拼法 + parse_closed_output），失败抛错
    由上层降级（原分补位）。返回 [Sevi_2, Sevi_3]（第 1 次为原判定，不
    在此重调——原判定分数直接用 tag.confidence）。
    """
    group_payload = _build_group_payload(spec, tag)
    prompt = (spec.prompt_text + "\n\n## 候选标签分组\n"
              + json.dumps(group_payload, ensure_ascii=False, indent=2))
    scores: list[float] = []
    for round_index in (2, 3):
        resp = llm.complete(
            [{"role": "user", "content": prompt}], spec.run_id)
        if not resp.ok:
            raise RuntimeError(f"重评第 {round_index} 次 LLM 调用失败: "
                               f"{resp.raw.get('error')}")
        parsed = parse_closed_output(resp.content)
        matched = None
        for item in parsed:
            if item.get("tag_id") == tag.tag_id:
                matched = item
        if matched is None:
            raise LLMParseError(
                f"重评第 {round_index} 次输出未包含标签 {tag.tag_id}")
        scores.append(_sevi_from_scores({
            "content": matched["evidence"]["content"]["evidence_strength"],
            "scene": matched["evidence"]["scene"]["evidence_strength"],
            "frame": matched["evidence"]["frame"]["evidence_strength"],
        }, evi_weights))
    return scores


def _verify_evidence(
    spec: GenerationSpec,
    tag: ProducedTag,
    llm,
    frame_parts: list[dict],
) -> int:
    """Cver 核验：标签 + 证据条目 + 五级表 → 1–5 分。

    材料段复用 spec.prompt_text（已含渲染后正文 + 场景行），追加模板常量
    与该标签的证据条目；有帧时带多模态 parts（与生成时同一组帧）。失败
    抛错由上层降级（0.5 占位）。
    """
    lines = [CLOSED_VERIFICATION_PROMPT, ""]
    lines.append(f"### 待核验标签：{tag.tag_name}")
    if tag.evidence:
        lines.append("证据条目：")
        for ev in tag.evidence:
            lines.append(f"- 场景 {ev.scene_id}：{ev.description}")
    else:
        lines.append("（该标签无证据条目）")
    prompt = spec.prompt_text + "\n\n" + "\n".join(lines)
    request = ([{"role": "user", "content": prompt}] if not frame_parts else
               [{"role": "user", "content": [
                   {"type": "text", "text": prompt}, *frame_parts]}])
    resp = llm.complete(request, spec.run_id)
    if not resp.ok:
        raise RuntimeError(f"核验 LLM 调用失败: {resp.raw.get('error')}")
    return _parse_verify_score(resp.content)


def _compute_c_mod(scores: dict[str, int], j: float) -> TagConfidence:
    """Cmod 本地算术：s_m 归一化 → dij → Cs/Cc → Cmod。填 TagConfidence。"""
    result = TagConfidence(tag_name="", modality_scores=dict(scores))
    s_values = {m: (v - 1) / 4 for m, v in scores.items()}
    result.s_values = s_values
    keys = ("content", "scene", "frame")
    d12 = abs(s_values[keys[0]] - s_values[keys[1]])
    d13 = abs(s_values[keys[0]] - s_values[keys[2]])
    d23 = abs(s_values[keys[1]] - s_values[keys[2]])
    result.pairwise_d = {"d12": d12, "d13": d13, "d23": d23}
    result.c_s = 1.0 - (d12 + d13 + d23) / 3.0
    result.c_c = max(d12, d13, d23)
    result.c_mod = j * result.c_s + (1.0 - j) * (1.0 - result.c_c)
    return result


def _format_cell(value: float | None, digits: int = 4) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def _render_report(
    asset_title: str,
    task_id: str,
    run_id: str,
    weights: tuple[float, float, float],
    variance_tolerance: float,
    fusion_j: float,
    results: list[TagConfidence],
) -> str:
    """置信度全量中间数据 → Markdown 文本。"""
    w_cons, w_ver, w_mod = weights
    lines: list[str] = []
    lines.append(f"# 置信度计算报告：{asset_title}")
    lines.append("")
    lines.append(f"- 任务：{task_id}（run {run_id}）")
    lines.append(f"- 生成时间：{datetime.now(timezone.utc).isoformat()}")
    lines.append(
        f"- 公式：Craw = {w_cons:.2f}·Ccons + {w_ver:.2f}·Cver "
        f"+ {w_mod:.2f}·Cmod（Ccons = exp(−As/{variance_tolerance})，"
        f"Cmod = {fusion_j:.2f}·Cs + {1 - fusion_j:.2f}·(1−Cc)）")
    lines.append(f"- 标签数：{len(results)}")
    lines.append("")
    # 总览表
    lines.append("## 总览")
    lines.append("")
    lines.append("| 标签 | Ccons | Cver | Cmod | Craw |")
    lines.append("|----|----|----|----|----|")
    for r in results:
        lines.append(
            f"| {r.tag_name} | {_format_cell(r.c_cons)} | "
            f"{_format_cell(r.c_ver)} | {_format_cell(r.c_mod)} | "
            f"{_format_cell(r.c_raw)} |")
    lines.append("")
    # 每标签细节
    for index, r in enumerate(results, start=1):
        lines.append(f"## {index}. {r.tag_name}")
        lines.append("")
        # Ccons
        lines.append("### Ccons 评分一致性（三次 Sevi 融合分）")
        partial_mark = " ⚠️ 重评失败，原分补位（partial）" if r.cons_partial else ""
        lines.append("")
        lines.append("| 轮次 | 分数 |")
        lines.append("|----|----|")
        for round_index, score in enumerate(r.sevi_scores, start=1):
            label = "原判定" if round_index == 1 else f"重评 {round_index - 1}"
            lines.append(f"| {label} | {score:.4f} |")
        lines.append(f"| 方差 As | {_format_cell(r.variance)} |")
        lines.append(f"| Ccons = exp(−As/{variance_tolerance}) | "
                     f"{_format_cell(r.c_cons)} |{partial_mark}")
        lines.append("")
        # Cver
        lines.append("### Cver 证据可验性（五级核验表）")
        if r.ver_partial:
            lines.append("")
            lines.append("⚠️ 核验失败，Cver 占位 0.5（partial）。")
        else:
            lines.append("")
            lines.append("| 核验分 S | Cver = (S−1)/4 |")
            lines.append("|----|----|")
            lines.append(f"| {r.verify_score} | {_format_cell(r.c_ver)} |")
        lines.append("")
        # Cmod
        lines.append("### Cmod 跨模态一致性")
        if r.mod_partial:
            lines.append("")
            lines.append("⚠️ 缺三模态强度读数，Cmod 占位 0.5（partial）。")
        else:
            lines.append("")
            lines.append("| 模态 | 原始强度 (1–5) | 归一化 s_m |")
            lines.append("|----|----|----|")
            for modality in ("content", "scene", "frame"):
                lines.append(
                    f"| {modality} | {r.modality_scores.get(modality, '—')} | "
                    f"{_format_cell(r.s_values.get(modality))} |")
            lines.append("")
            lines.append("| 中间量 | 值 |")
            lines.append("|----|----|")
            for key in ("d12", "d13", "d23"):
                lines.append(f"| {key} | {_format_cell(r.pairwise_d.get(key))} |")
            lines.append(f"| Cs = 1−(d12+d13+d23)/3 | {_format_cell(r.c_s)} |")
            lines.append(f"| Cc = max(dij) | {_format_cell(r.c_c)} |")
            lines.append(
                f"| Cmod = {fusion_j:.2f}·Cs + {1 - fusion_j:.2f}·(1−Cc) | "
                f"{_format_cell(r.c_mod)} |")
        lines.append("")
        # Craw
        lines.append(
            f"**Craw = {w_cons:.2f}·{_format_cell(r.c_cons)} + "
            f"{w_ver:.2f}·{_format_cell(r.c_ver)} + "
            f"{w_mod:.2f}·{_format_cell(r.c_mod)} = "
            f"{_format_cell(r.c_raw)}**")
        lines.append("")
    return "\n".join(lines)


def _write_report(asset_title: str, report_dir: str, content: str) -> Path:
    """报告落盘：{资产名清洗}_{UTC 时间戳}.md（目录不存在自动建）。"""
    root = Path(report_dir)
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = root / f"{_sanitize_name(asset_title)}_{stamp}.md"
    path.write_text(content, encoding="utf-8")
    return path


def apply_confidence(
    tags: list[ProducedTag],
    spec: GenerationSpec,
    asset_title: str,
    session: Session,
    llm,
) -> list[TagConfidence]:
    """置信度主入口：算 Craw、覆写 tags[*].confidence、写 MD 报告。

    挂在 execute 的 run_filter_chain 之后、persist_tags 之前（closed-only）。
    永不抛错：任何内部异常只 log warning，保持 producer 原 confidence 不动
    （降级语义——置信度环节绝不把任务落 failed）。session 参数留作未来
    扩展（当前计算不落库，Craw 经 persist_tags 随行写入）。
    """
    if not tags:
        return []
    try:
        settings = get_settings()
        weights = settings.closed_confidence_weight_values
        b = settings.closed_confidence_variance_tolerance
        j = settings.closed_confidence_fusion_j
        evi_weights = settings.closed_evi_weight_values
        # Cver 核验消息复用生成时的帧 parts（多模态对照，与 producer 口径
        # 一致；_resolve_frame_parts 幂等，磁盘 IO 已有缓存则秒回）
        from tagplatform.services.generation.closed.producer import (
            _resolve_frame_parts,
        )
        frame_parts = _resolve_frame_parts(spec)
        results: list[TagConfidence] = []
        for tag in tags:
            results.append(_compute_for_tag(
                tag, spec, llm, weights, b, j, evi_weights, frame_parts))
        _finalize(results, weights)
        for tag, result in zip(tags, results):
            if result.c_raw is not None:
                tag.confidence = result.c_raw
        report_path = None
        try:
            report_path = _write_report(
                asset_title, settings.closed_confidence_report_dir,
                _render_report(asset_title, spec.task_id, spec.run_id,
                               weights, b, j, results))
            logger.info("置信度报告已写入：%s（标签 %d 条）",
                        report_path, len(results))
        except OSError as exc:
            logger.warning("置信度报告写盘失败：%s", exc)
        return results
    except Exception as exc:  # noqa: BLE001 —— 降级红线：不阻断任务
        logger.warning("置信度计算整体失败（保持原 confidence）：%s", exc)
        return []


def _compute_for_tag(
    tag: ProducedTag,
    spec: GenerationSpec,
    llm,
    weights: tuple[float, float, float],
    b: float,
    j: float,
    evi_weights: tuple[float, float, float],
    frame_parts: list[dict],
) -> TagConfidence:
    """单标签三分量计算（内部各分量独立降级）。"""
    result = TagConfidence(
        tag_name=tag.tag_name, tag_id=tag.tag_id)
    # ---- Ccons ----
    base_sevi = tag.confidence if tag.confidence is not None else 0.5
    try:
        re_scores = _reconsistency_scores(spec, tag, llm, evi_weights)
        result.sevi_scores = [base_sevi] + re_scores
    except Exception as exc:  # noqa: BLE001 —— 重评失败 → 原分补位
        logger.warning("标签 %s Ccons 重评失败（原分补位）：%s",
                       tag.tag_name, exc)
        result.sevi_scores = [base_sevi, base_sevi, base_sevi]
        result.cons_partial = True
    mean = sum(result.sevi_scores) / len(result.sevi_scores)
    variance = sum((s - mean) ** 2 for s in result.sevi_scores) / len(
        result.sevi_scores)
    result.variance = variance
    result.c_cons = math.exp(-variance / b)
    # ---- Cver ----
    try:
        score = _verify_evidence(spec, tag, llm, frame_parts)
        result.verify_score = score
        result.c_ver = (score - 1) / 4
    except Exception as exc:  # noqa: BLE001 —— 核验失败 → 0.5 占位
        logger.warning("标签 %s Cver 核验失败（0.5 占位）：%s",
                       tag.tag_name, exc)
        result.c_ver = 0.5
        result.ver_partial = True
    # ---- Cmod ----
    if tag.evidence_scores and len(tag.evidence_scores) >= 3:
        mod = _compute_c_mod(tag.evidence_scores, j)
        result.modality_scores = mod.modality_scores
        result.s_values = mod.s_values
        result.pairwise_d = mod.pairwise_d
        result.c_s = mod.c_s
        result.c_c = mod.c_c
        result.c_mod = mod.c_mod
    else:
        logger.warning("标签 %s 缺三模态强度读数（Cmod 占位 0.5）",
                       tag.tag_name)
        result.c_mod = 0.5
        result.mod_partial = True
    return result


def _finalize(results: list[TagConfidence],
              weights: tuple[float, float, float]) -> None:
    """Craw 加权收口（三权重已在 Settings 层归一化）。"""
    w_cons, w_ver, w_mod = weights
    for r in results:
        if r.c_cons is not None and r.c_ver is not None and r.c_mod is not None:
            r.c_raw = (w_cons * r.c_cons + w_ver * r.c_ver
                       + w_mod * r.c_mod)
