"""确定性 Mock LLM 客户端：测试与冒烟用，零网络、零随机源。

与阶段 1 的 MockEmbedder 对应：无 random 模块、跨进程确定，全部行为由
sha256 哈希驱动。按 prompt 内容识别两种模式（与任务 8 runner 的组装咬合）：
- 模式 A（封闭提取）：prompt 含 `## 候选标签` 节 → 解析候选行
  `- {tag_id} | {tag_name} | {tag_type} | {description}`，按
  sha256(run_id + tag_id) 以 ~40% 概率选中，输出 LLM 契约的五键
  （tag_id/tag_name/relevance/confidence/evidence）；
- 模式 B（开放生成）：固定返回 3 个候选（多线叙事/情感张力/视听风格），
  不含 tag_id（模式 B 的 tag_id 由匹配层赋，LLM 不产出）。

场景证据（Task 6）：两模式均解析 prompt 中的场景行（runner
_render_content_text 组装的 `- {scene_id} [{start_ms}-{end_ms}] {description}`），
每个输出标签按 sha256(evidence|{run_id}|{tag_id 或 tag_name}) 确定性选一条
场景行附为一元素证据卡（scene_id/start_ms/end_ms/description 取该行值）——
演示/冒烟模式下证据链完整、evidence_coverage 非零；prompt 无场景行
（如 title-only 空内容测试）时兜底为空列表。

哈希风格与 MockEmbedder 一致（前 8 hex / 0x100000000）；选中、打分与证据
选取用不同哈希键——若复用选中哈希，被选中的标签分数恒 < 0.7，会与任务 5
的 min_confidence=0.7 过滤耦合退化为空结果。
"""
from __future__ import annotations

import hashlib
import json
import re

from tagplatform.llm.client import LLMResponse

# 候选行格式：`- {tag_id} | {tag_name} | {tag_type} | {description}`
_CANDIDATE_LINE_RE = re.compile(r"^- (.+?) \| (.+?) \| (.+?) \| (.+)$", re.MULTILINE)
# 场景行格式（runner._render_content_text 组装，逐字对齐）：
# `- {scene_id} [{start_ms}-{end_ms}] {description}`；scene_id 取 \S+（候选行
# 首词后跟 " |" 而非 " ["，两类行格式天然互斥，互不误配）
_SCENE_LINE_RE = re.compile(r"^- (\S+) \[(\d+)-(\d+)\] (.+)$", re.MULTILINE)
# 模式 A 标志节（任务 8 runner 组装 candidates 时的标题）
_CLOSED_MARKER = "## 候选标签"
# 开放式生成（mtg 流水线）的标志串：生成 / 审核两种请求靠它分流
_MTG_GEN_MARKER = "## 标签生成任务"
_MTG_JUDGE_MARKER = "## 审核任务"
# 相似度复核请求（generation/mtg/verify.py）：行格式同为全角竖线三段
_MTG_VERIFY_MARKER = "## 相似度复核"
# 审核候选行格式：`- {tag}｜{dimension}｜{reason}`（全角竖线，与场景行互斥）
_JUDGE_LINE_RE = re.compile(r"^- (.+?)｜(.+?)｜(.*)$", re.MULTILINE)
# 选中概率阈值（~40%）
_SELECT_PROB = 0.4

# 开放生成固定三候选：名称 / 维度（中文五维名）
_OPEN_SEEDS = [
    ("多线叙事", "叙事"),
    ("情感张力", "情绪"),
    ("视听风格", "风格"),
]


def _extract_text(messages: list[dict]) -> str:
    """拼接消息中的全部文本（多模态 content parts；图像 part 无 text 键自然跳过）。"""
    parts = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, list):
            parts.extend(p.get("text", "") for p in c if isinstance(p, dict))
    return "\n".join(parts)


def _hash_unit(key: str) -> float:
    """哈希 → 确定性 [0,1) 均匀值（sha256 前 8 hex，与 MockEmbedder 同风格）。"""
    h = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return int(h[:8], 16) / 0x1_0000_0000


def _score(key: str) -> float:
    """哈希 → [0.5, 1.0) 分值（relevance/confidence 共用映射）。"""
    return 0.5 + _hash_unit(key) * 0.5


def _parse_scenes(text: str) -> list[dict]:
    """解析 prompt 中的全部场景行（无场景行 → []，title-only 兜底的前置）。

    行格式由 runner._render_content_text 组装（逐字对齐），时间戳还原为 int。"""
    return [{"scene_id": sid, "start_ms": int(start), "end_ms": int(end),
             "description": desc}
            for sid, start, end, desc in _SCENE_LINE_RE.findall(text)]


def _pick_evidence(scenes: list[dict], hash_key: str) -> list[dict]:
    """按 sha256(哈希键) 确定性选一条场景行作一元素证据卡（无场景行 → []）。

    哈希键 `evidence|{run_id}|{tag_id 或 tag_name}`（与选中/打分哈希键区分，
    互不耦合）；前 8 hex → [0,1) 均匀值按场景数分桶定位（[0,1) 严格小于 1，
    桶下标恒落在合法区间）。description 定为场景描述（Task 6 拍板）。
    """
    if not scenes:
        return []
    scene = scenes[int(_hash_unit(hash_key) * len(scenes))]
    return [{"scene_id": scene["scene_id"], "start_ms": scene["start_ms"],
             "end_ms": scene["end_ms"], "description": scene["description"]}]


def _closed_output(text: str, run_id: str) -> list[dict]:
    """模式 A：解析候选行，哈希采样选中项（确定性伪判定），逐标签附场景证据。"""
    tail = text.split(_CLOSED_MARKER, 1)[1]
    # 场景行在 content 节（候选标志节之前），对全文解析即可
    scenes = _parse_scenes(text)
    items = []
    for tag_id, tag_name, _tag_type, _desc in _CANDIDATE_LINE_RE.findall(tail):
        if _hash_unit(run_id + tag_id) >= _SELECT_PROB:
            continue
        items.append({
            "tag_id": tag_id,
            "tag_name": tag_name,
            "relevance": _score(f"relevance|{run_id}|{tag_id}"),
            "confidence": _score(f"confidence|{run_id}|{tag_id}"),
            "evidence": _pick_evidence(scenes, f"evidence|{run_id}|{tag_id}"),
        })
    return items


def _open_output(text: str, run_id: str) -> list[dict]:
    """开放式生成（mtg）：固定三候选，按场景数轮转给一条场景证据。

    三轮返回同样的三个候选（真实模型也会重复），用于覆盖跨轮合并路径：
    归一化去重后 runs=3。
    """
    scenes = _parse_scenes(text)
    items = []
    for index, (name, dimension) in enumerate(_OPEN_SEEDS):
        scene = scenes[index % len(scenes)] if scenes else None
        items.append({
            "tag": name,
            "dimension": dimension,
            "scenes": [scene["scene_id"]] if scene else [],
            "reason": f"模拟依据 #{index + 1}",
        })
    return items


def _verify_output(text: str) -> dict:
    """相似度复核：候选与库标签字面相同才算同一概念（确定性伪判定）。

    真实模型的判据是语义；mock 只按字面相等给 true，保证测试里
    "只有同名才替换"这条路径可预期。
    """
    results = [
        {"candidate": candidate.strip(),
         "same": candidate.strip() == library.strip()}
        for candidate, library, _cos in _JUDGE_LINE_RE.findall(text)
    ]
    return {"results": results}


def _judge_output(text: str, run_id: str) -> list[dict]:
    """judge：解析候选行，全部判 supported（confidence 0.9，引用首场景并摘原文）。

    必须给 quote：流水线要求"判决引用内容原文"，模拟客户端从首场景描述里
    截一段，口径与真实 judge 一致。
    """
    scenes = _parse_scenes(text)
    scene_id = scenes[0]["scene_id"] if scenes else None
    quote = scenes[0]["description"][:12] if scenes else ""
    items = []
    for tag, _dimension, _reason in _JUDGE_LINE_RE.findall(text):
        items.append({
            "tag": tag.strip(),
            "verdict": "supported",
            "importance": "core",
            "confidence": 0.9,
            "scenes": [scene_id] if scene_id else [],
            "quote": quote,
            "reason": "模拟判定：所引原文支撑标签",
        })
    return items


class MockLLMClient:
    """确定性伪 LLM：哈希即输出，测试与 mock 配置专用。"""

    def __init__(self, model: str = "mock-1"):
        self.model = model

    def complete(self, messages: list[dict], run_id: str) -> LLMResponse:
        text = _extract_text(messages)
        if _MTG_JUDGE_MARKER in text:
            out = _judge_output(text, run_id)
        elif _MTG_VERIFY_MARKER in text:
            out = _verify_output(text)
        elif _CLOSED_MARKER in text:
            out = _closed_output(text, run_id)
        else:
            out = _open_output(text, run_id)
        content = json.dumps(out, ensure_ascii=False)
        usage = {"prompt_tokens": 100, "completion_tokens": 50}
        # raw 保留 OpenAI 信封形状（choices[0].message.content），与真实
        # 客户端成功响应结构一致，便于上层统一处理与调试对账
        return LLMResponse(content=content, usage=usage,
                           raw={"choices": [{"message": {"content": content},
                                             "finish_reason": "stop"}],
                                "usage": usage, "mock": True},
                           ok=True)
