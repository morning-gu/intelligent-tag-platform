"""closed 产出标签的置信度计算（阶段 2.5，2026-12）。

公式（用户口径，权重/参数走 Settings）：
- Craw = w1·Ccons + w2·Cver + w3·Cmod（默认 0.45/0.30/0.25，和归一化）
- Ccons（评分一致性）：对每个最终标签**再评 2 次**（与原判定共 3 次），
  分数口径 = Sevi 融合分（0–1，三模态 evidence_strength 加权归一化）；
  As = 三次分数的总体方差（÷3，ddof=0）；Ccons = exp(−As/b)，b 默认 0.2。
- Cver（证据核验，v2 模态两维）：LLM 按 content/scene/frame 三模态各判
  Rn（0/1 评价资格）+ 充分度三档 + T 真实性（1–5）+ V 可核验性（1–5），
  数值计算本地做：T/V 各按 (x−1)/4 归一，模态分 Cver,n = 0.6T + 0.4V，
  权重 Qn 由充分度组合查固定菜单（同级并列时 V 高者拿大权重，再平 T，
  再平按 content→scene→frame 顺序），Cver = Σ Rn·Qn·Cver,n（0–1）。
- Cmod（跨模态一致性）：s_m = (evidence_strength_m − 1)/4（归一化标尺），
  dij = |si − sj|，Cs = 1 − (d12 + d13 + d23)/3，Cc = max(dij)，
  Cmod = j·Cs + (1−j)·(1−Cc)，j 默认 0.5。本地计算。

三个分量的取材（与 producer 判定口径一一对应）：
- Ccons 重评：复用 closed 生成契约——单标签「## 候选标签」块插入
  spec.prompt_text 的「## 影视内容」节之前（tag_block 拼法，与生成时
  同构），同 run_id 调 2 次，输出经
  parse_closed_output 解析，每次按 evidence_scores 算 Sevi。消息与
  生成时同构：有帧带多模态 parts（与 Cver 核验同一组帧，用户口径
  2026-11：三次判定同条件测量）。Mock 端哈希键含 run_id，同 run_id
  重调 = 相同输出 → As = 0 → Ccons = 1.0。
- Cver 核验：自拼材料（不复用 spec.prompt_text——生成契约正文含候选
  清单与输出指令残留，长上下文下证据条目易被淹没）：常量
  CLOSED_VERIFICATION_PROMPT（角色定位 + Rn/充分度/T/V 判定表）→
  「## 影视内容」节（_render_content_text(spec.content) 渲染的原始资产
  文本）→「### 待核验标签」（含标签描述与别名，经 spec.library 反查，
  缺则省行）→ 证据条目（带时间区间）→ 输出行（模态 JSON 契约，置尾）；
  Cver 分支要求模型输出按模态小 JSON（Rn/充分度/T/V）；有帧时核验消息
  带与生成时相同的帧 parts（多模态对照，帧即 frame 模态的核验材料）。
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
from tagplatform.services.generation.assemble import _render_content_text
from tagplatform.services.generation.base import Evidence, GenerationSpec, ProducedTag
from tagplatform.services.generation.closed.parse import (
    LLMParseError,
    parse_closed_output,
)
from tagplatform.services.generation.closed.tag_block import (
    _one_line,
    render_candidate_block,
    splice_candidate_block,
)
from tagplatform.settings import get_settings

logger = logging.getLogger(__name__)

# Cver 小 JSON 的解析宽容度：容忍 ```json 围栏与键名漂移（"modalities"/
# "模态"），分值钳到 1–5。与 parse_closed_output 的围栏剥离口径一致。
_VERIFY_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")
_VERIFY_MODALITY_KEYS = ("modalities", "模态")
_SUFFICIENCY_LEVELS = ("充分", "支持", "辅助")
_MODALITY_ORDER = ("content", "scene", "frame")


@dataclass
class ModalityVerdict:
    """单模态核验判定（LLM 原始输出解析结果）。"""

    r: int = 0                      # 模态标签相关性：1=具备评价资格
    sufficiency: str = ""           # 充分度三档：充分/支持/辅助
    t: int | None = None            # 真实性 1–5（Rn=0 时 None）
    v: int | None = None            # 可核验性 1–5（Rn=0 时 None）


def _clamp_score(value) -> int:
    """宽容转整数并钳 1–5（复刻旧 _parse_verify_score 的钳位口径）。"""
    return min(max(int(value), 1), 5)


def _parse_sufficiency(value) -> str:
    """充分度归一到三档枚举；异常值按「辅助」处理（最保守档）。"""
    text = str(value or "").strip()
    for level in _SUFFICIENCY_LEVELS:
        if level in text:
            return level
    return "辅助"


def _sufficiency_rank(level: str) -> int:
    """充分度档位 → 排序秩：充分=2 > 支持=1 > 辅助=0（异常档按辅助）。"""
    if level not in _SUFFICIENCY_LEVELS:
        return 0
    return len(_SUFFICIENCY_LEVELS) - 1 - _SUFFICIENCY_LEVELS.index(level)


def _parse_modality_verdicts(raw: str) -> dict[str, ModalityVerdict]:
    """Cver 输出 → 三模态判定。剥围栏、找对象、容忍键名漂移。

    缺失模态按 Rn=0（无评价资格）补齐；r 非法值按 0 处理。Rn=0 时
    t/v 忽略（保持 None）。
    """
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
    modalities = None
    for key in _VERIFY_MODALITY_KEYS:
        if key in obj and isinstance(obj[key], dict):
            modalities = obj[key]
            break
    if modalities is None:
        raise LLMParseError(f"核验输出缺少 modalities 键: {raw[:200]!r}")
    verdicts: dict[str, ModalityVerdict] = {}
    for modality in _MODALITY_ORDER:
        entry = modalities.get(modality)
        if not isinstance(entry, dict):
            verdicts[modality] = ModalityVerdict(r=0)
            continue
        try:
            r = int(entry.get("r", 0))
        except (TypeError, ValueError):
            r = 0
        r = 1 if r else 0
        verdict = ModalityVerdict(r=r, sufficiency=_parse_sufficiency(
            entry.get("sufficiency")))
        if r:
            try:
                verdict.t = _clamp_score(entry.get("t"))
                verdict.v = _clamp_score(entry.get("v"))
            except (TypeError, ValueError) as exc:
                raise LLMParseError(
                    f"核验输出 {modality} 模态 t/v 不是整数: "
                    f"{entry.get('t')!r}/{entry.get('v')!r}") from exc
        verdicts[modality] = verdict
    return verdicts


# ---- 权重菜单（用户口径 2026-12）----
# 充分度三档组合 → 固定菜单行。同级并列时大权重给 V 分更高的模态
# （再平 T，再平按 content→scene→frame 顺序）。

# 2 个相关模态（相对原表 0.1:0.9 / 0.3:0.7 / 0.5:0.5）
_WEIGHT_MENU_TWO = {
    ("充分", "充分"): (0.5, 0.5),
    ("充分", "支持"): (0.7, 0.3),
    ("充分", "辅助"): (0.9, 0.1),
    ("支持", "支持"): (0.5, 0.5),
    ("支持", "辅助"): (0.7, 0.3),
    ("辅助", "辅助"): (0.5, 0.5),
}

# 3 个相关模态（相对原表 0.1:0.2:0.7 / 0.1:0.4:0.5 / 0.3:0.3:0.4）：
# 组合归并为四类——三个都有效（充分×3 / 充分×2+支持 / 充分+支持×2 /
# 支持×3）→ 0.3:0.3:0.4；两个有效 + 一个辅助 → 0.5:0.4:0.1；一个有效
# + 两个辅助 → 0.7:0.2:0.1；辅助×3 → 0.3:0.3:0.4。
def _map_three_modality_weights(
    verdicts: dict[str, ModalityVerdict],
) -> dict[str, float]:
    """3 相关模态组合 → 每模态权重（菜单行内排列由并列破序规则定）。"""
    levels = {m: verdicts[m].sufficiency for m in _MODALITY_ORDER}
    full = ("充分", "支持")
    if all(levels[m] in full for m in _MODALITY_ORDER):
        base = (0.3, 0.3, 0.4)
    elif sum(1 for m in _MODALITY_ORDER if levels[m] in full) == 2:
        base = (0.5, 0.4, 0.1)
    elif sum(1 for m in _MODALITY_ORDER if levels[m] in full) == 1:
        base = (0.7, 0.2, 0.1)
    else:
        base = (0.3, 0.3, 0.4)
    # 菜单行内排列：大权重配更充分的模态。破序键：充分度秩（充分=2 >
    # 支持=1 > 辅助=0）→ V → T → 固定顺序；降序排列 → 秩大者在前，
    # zip 时按序拿 sorted(base, reverse=True) 的大值。
    order = sorted(
        _MODALITY_ORDER,
        key=lambda m: (
            _sufficiency_rank(levels[m]),
            verdicts[m].v or 0, verdicts[m].t or 0,
            -_MODALITY_ORDER.index(m),
        ),
        reverse=True,
    )
    weights = dict(zip(order, sorted(base, reverse=True)))
    return weights


def _assign_modality_weights(
    verdicts: dict[str, ModalityVerdict],
) -> dict[str, float]:
    """全部模态的 Qn（本地权重分配，Rn=0 强制 0）。

    规则（用户口径）：两模态 Rn=0 → 剩余模态 1；一 Rn=0 → 二模态菜单；
    全 Rn=1 → 三模态菜单；全 Rn=0 → 调用方降级（视为输出异常）。
    """
    relevant = [m for m in _MODALITY_ORDER if verdicts[m].r]
    weights = {m: 0.0 for m in _MODALITY_ORDER}
    if not relevant:
        return weights
    if len(relevant) == 1:
        weights[relevant[0]] = 1.0
        return weights
    if len(relevant) == 2:
        a, b = relevant
        pair = _WEIGHT_MENU_TWO.get(
            (verdicts[a].sufficiency, verdicts[b].sufficiency))
        if pair is None:  # 反序组合（如 辅助+充分）→ 菜单对称重查
            pair = _WEIGHT_MENU_TWO.get(
                (verdicts[b].sufficiency, verdicts[a].sufficiency))
        if pair is None:  # 异常档位兜底：对称五五开
            pair = (0.5, 0.5)
        # pair 为 (大, 小)：大权重给更充分模态（档位秩→V→T→固定序破平）
        rank = {
            m: (_sufficiency_rank(verdicts[m].sufficiency),
                verdicts[m].v or 0, verdicts[m].t or 0)
            for m in (a, b)
        }
        first, second = (a, b) if (
            rank[a] > rank[b] or
            (rank[a] == rank[b] and _MODALITY_ORDER.index(a)
             < _MODALITY_ORDER.index(b))
        ) else (b, a)
        weights[first], weights[second] = max(pair), min(pair)
        return weights
    return _map_three_modality_weights(verdicts)


def _compute_c_ver(
    verdicts: dict[str, ModalityVerdict],
) -> tuple[float, dict[str, float]]:
    """本地数值计算：Cver = Σ Rn·Qn·(0.6T + 0.4V)（T/V 已归一 0–1）。

    返回 (Cver, 各模态 Qn)。全 Rn=0 抛 ValueError（调用方降级）。
    """
    weights = _assign_modality_weights(verdicts)
    if not any(verdicts[m].r for m in _MODALITY_ORDER):
        raise ValueError("三模态 Rn 全为 0，无法计算 Cver")
    total = 0.0
    for m in _MODALITY_ORDER:
        verdict = verdicts[m]
        if not verdict.r:
            continue
        t_norm = (verdict.t - 1) / 4
        v_norm = (verdict.v - 1) / 4
        total += weights[m] * (0.6 * t_norm + 0.4 * v_norm)
    return total, weights


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
    # Cver：三模态判定（Rn/sufficiency/T/V）与本地计算的 Qn/Cver
    verify_score: dict[str, ModalityVerdict] = field(default_factory=dict)
    ver_weights: dict[str, float] = field(default_factory=dict)
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


def _parse_verify_score(raw: str) -> dict[str, ModalityVerdict]:
    """Cver 输出 → 三模态判定（旧名单一 score 契约退役）。

    兼容别名：老式 {"score": N} 会被 _parse_modality_verdicts 以
    缺 modalities 键拒绝；mock 与测试均已切到新契约。
    """
    return _parse_modality_verdicts(raw)


def _tag_definition(spec: GenerationSpec, tag: ProducedTag):
    """经 spec.library 反查标签定义（无库/无 tag_id 时 None）。"""
    return spec.library.tag(tag.tag_id) if (
        spec.library and tag.tag_id) else None


def _build_candidate_block(spec: GenerationSpec, tag: ProducedTag,
                           ) -> str:
    """单标签重建 closed 候选块（与 producer 拼法同构）。

    「## 候选标签」节：目录头（名称/描述）+ 单标签行组（tag_id/名称/
    描述/别名）；目录与标签定义从 spec.library 反查（producer 同款
    访问器）。
    """
    library = spec.library
    directory = library.directory(tag.dir_id) if (library and tag.dir_id) else None
    definition = _tag_definition(spec, tag)
    description = (definition.description if definition
                   else (tag.candidate_description or ""))
    return render_candidate_block(
        dir_name=directory.dir_name if directory else "未分类",
        dir_description=directory.description if directory else "",
        tags=[(tag.tag_id or "", tag.tag_name, description,
               definition.aliases if definition else ())],
    )


def _reconsistency_scores(
    spec: GenerationSpec,
    tag: ProducedTag,
    llm,
    evi_weights: tuple[float, float, float],
    frame_parts: list[dict] | None = None,
) -> list[float]:
    """Ccons 重评：同 run_id 再调 2 次单标签组判定，取每次的 Sevi。

    复用 closed 生成契约（候选块拼法 + parse_closed_output），失败抛错
    由上层降级（原分补位）。返回 [Sevi_2, Sevi_3]（第 1 次为原判定，不
    在此重调——原判定分数直接用 tag.confidence）。消息与生成/Cver 同构：
    有帧带多模态 parts（三次判定同条件，帧评分口径才可比）。
    """
    prompt = splice_candidate_block(
        spec.prompt_text, _build_candidate_block(spec, tag))
    parts = frame_parts or []
    request = ([{"role": "user", "content": prompt}] if not parts else
               [{"role": "user", "content": [
                   {"type": "text", "text": prompt}, *parts]}])
    scores: list[float] = []
    for round_index in (2, 3):
        resp = llm.complete(request, spec.run_id)
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


_VERIFY_OUTPUT_LINE = (
    '只输出 JSON 对象（Rn 判 0 的模态省略 t/v 字段，scene/frame 模态'
    '省略时整体省略该键），例如 '
    '{"modalities": {"content": {"r": 1, "sufficiency": "充分", "t": 4, "v": 5}}}'
    '，不要输出其他文字。'
)


def _verify_evidence(
    spec: GenerationSpec,
    tag: ProducedTag,
    llm,
    frame_parts: list[dict],
) -> dict[str, ModalityVerdict]:
    """Cver 核验：标签 + 证据条目 → 三模态判定（Rn/充分度/T/V）。

    自拼材料（不复用 spec.prompt_text——生成契约正文含候选清单与输出
    指令残留）：核验常量（角色定位 + 判定表）→「## 影视内容」节
    （_render_content_text 渲染的原始资产文本）→ 待核验标签（含描述/
    别名，经 spec.library 反查，缺则省行）→ 证据条目（带时间区间）→
    输出行置尾。三模态的核验材料即三个输入通道：content=资产文本整体、
    scene=其中的逐场景行、frame=随消息附带的关键帧图片（与生成时同一
    组）。证据条目按场景锚定平铺（Evidence 无模态归属，同一条证据可
    在多个通道中定位）。数值计算在 _compute_c_ver 本地做。失败抛错由
    上层降级（0.5 占位）。
    """
    definition = _tag_definition(spec, tag)
    description = (definition.description if definition
                    else (tag.candidate_description or ""))
    aliases = definition.aliases if definition else ()
    lines = [CLOSED_VERIFICATION_PROMPT, "",
             "## 影视内容", _render_content_text(spec.content), "",
             f"### 待核验标签：{tag.tag_name}"]
    description = _one_line(description)
    if description:
        lines.append(f"标签描述：{description}")
    aliases = tuple(_one_line(alias) for alias in aliases if _one_line(alias))
    if aliases:
        lines.append(f"标签别名：{'、'.join(aliases)}")
    if tag.evidence:
        lines.append("证据条目：")
        for ev in tag.evidence:
            lines.append(f"- 场景 {ev.scene_id} "
                         f"[{ev.start_ms}-{ev.end_ms}]：{ev.description}")
    else:
        lines.append("（该标签无证据条目）")
    lines.append("")
    lines.append(_VERIFY_OUTPUT_LINE)
    prompt = "\n".join(lines)
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
        lines.append("### Cver 证据核验（模态两维：Rn/充分度/T/V）")
        if r.ver_partial:
            lines.append("")
            lines.append("⚠️ 核验失败，Cver 占位 0.5（partial）。")
        else:
            lines.append("")
            lines.append("| 模态 | Rn | 充分度 | T | V | Qn | Cver,n = 0.6·T′+0.4·V′ |")
            lines.append("|----|----|----|----|----|----|----|")
            for modality in _MODALITY_ORDER:
                verdict = r.verify_score.get(modality)
                if verdict is None:
                    lines.append(f"| {modality} | — | — | — | — | — | — |")
                    continue
                t_norm = (verdict.t - 1) / 4 if verdict.t else None
                v_norm = (verdict.v - 1) / 4 if verdict.v else None
                modality_score = (None if t_norm is None or v_norm is None
                                  else 0.6 * t_norm + 0.4 * v_norm)
                lines.append(
                    f"| {modality} | {verdict.r} | "
                    f"{verdict.sufficiency or '—'} | "
                    f"{verdict.t if verdict.t else '—'} | "
                    f"{verdict.v if verdict.v else '—'} | "
                    f"{_format_cell(r.ver_weights.get(modality))} | "
                    f"{_format_cell(modality_score)} |")
            lines.append("")
            lines.append(f"| Cver = Σ Rn·Qn·Cver,n | {_format_cell(r.c_ver)} |")
            lines.append("|----|----|")
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
        re_scores = _reconsistency_scores(spec, tag, llm, evi_weights,
                                          frame_parts)
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
        verdicts = _verify_evidence(spec, tag, llm, frame_parts)
        c_ver, ver_weights = _compute_c_ver(verdicts)
        result.verify_score = verdicts
        result.ver_weights = ver_weights
        result.c_ver = c_ver
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
