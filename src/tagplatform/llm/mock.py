"""确定性 Mock LLM 客户端：测试与冒烟用，零网络、零随机源。

与阶段 1 的 MockEmbedder 对应：无 random 模块、跨进程确定，全部行为由
sha256 哈希驱动。按 prompt 内容识别两种模式（与任务 8 runner 的组装咬合）：
- 模式 A（封闭提取）：prompt 含 `## 候选标签` 节 → 解析候选行
  `- {tag_id} | {tag_name} | {tag_type} | {description}`，按
  sha256(run_id + tag_id) 以 ~40% 概率选中，输出 LLM 契约的五键
  （tag_id/tag_name/relevance/confidence/evidence）；
- 模式 B（开放生成）：固定返回 3 个候选（多线叙事/情感张力/视听风格），
  不含 tag_id（模式 B 的 tag_id 由匹配层赋，LLM 不产出）。

哈希风格与 MockEmbedder 一致（前 8 hex / 0x100000000）；选中与打分用不同
哈希键——若复用选中哈希，被选中的标签分数恒 < 0.7，会与任务 5 的
min_confidence=0.7 过滤耦合退化为空结果。
"""
from __future__ import annotations

import hashlib
import json
import re

from tagplatform.llm.client import LLMResponse

# 候选行格式：`- {tag_id} | {tag_name} | {tag_type} | {description}`
_CANDIDATE_LINE_RE = re.compile(r"^- (.+?) \| (.+?) \| (.+?) \| (.+)$", re.MULTILINE)
# 模式 A 标志节（任务 8 runner 组装 candidates 时的标题）
_CLOSED_MARKER = "## 候选标签"
# 选中概率阈值（~40%）
_SELECT_PROB = 0.4

# 模式 B 固定三候选：名称 / 分类 / 一句话中文定义
_OPEN_SEEDS = [
    ("多线叙事", "narrative", "叙事在多条线索或时间线之间交替推进。"),
    ("情感张力", "mood", "情绪在铺垫与释放之间保持持续的紧张感。"),
    ("视听风格", "style", "画面构图与声音设计共同呈现鲜明的风格化表达。"),
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


def _closed_output(text: str, run_id: str) -> list[dict]:
    """模式 A：解析候选行，哈希采样选中项（确定性伪判定）。"""
    tail = text.split(_CLOSED_MARKER, 1)[1]
    items = []
    for tag_id, tag_name, _tag_type, _desc in _CANDIDATE_LINE_RE.findall(tail):
        if _hash_unit(run_id + tag_id) >= _SELECT_PROB:
            continue
        items.append({
            "tag_id": tag_id,
            "tag_name": tag_name,
            "relevance": _score(f"relevance|{run_id}|{tag_id}"),
            "confidence": _score(f"confidence|{run_id}|{tag_id}"),
            "evidence": [],
        })
    return items


def _open_output(run_id: str) -> list[dict]:
    """模式 B：固定三候选，confidence 由哈希映射 [0.5,1.0)（轮次噪声）。"""
    return [{
        "tag_name": name,
        "tag_type": tag_type,
        "description": desc,
        "confidence": _score(f"confidence|{run_id}|{name}"),
        "evidence": [],
    } for name, tag_type, desc in _OPEN_SEEDS]


class MockLLMClient:
    """确定性伪 LLM：哈希即输出，测试与 mock 配置专用。"""

    def __init__(self, model: str = "mock-1"):
        self.model = model

    def complete(self, messages: list[dict], run_id: str) -> LLMResponse:
        text = _extract_text(messages)
        if _CLOSED_MARKER in text:
            out = _closed_output(text, run_id)
        else:
            out = _open_output(run_id)
        content = json.dumps(out, ensure_ascii=False)
        usage = {"prompt_tokens": 100, "completion_tokens": 50}
        # raw 必须是 OpenAI 信封形状：下游缓存命中路径从 choices[0].message.content
        # 还原文本，信封契约在 mock 路径下同样生效
        return LLMResponse(content=content, usage=usage,
                           raw={"choices": [{"message": {"content": content},
                                             "finish_reason": "stop"}],
                                "usage": usage, "mock": True},
                           ok=True)
