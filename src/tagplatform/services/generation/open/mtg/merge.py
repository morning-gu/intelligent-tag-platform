"""开放生成候选数据结构（内部流转用）。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MergedCandidate:
    """去重后的候选：名称、来源场景、阶段作用域、目标目录与唯一候选 ID（内部用）。

    `scope`（film|scene）在流水线内部用于"全片级要两个场景支撑"、
    一阶段聚类和阶段配额管理。
    """

    tag: str
    dimension: str | None
    scenes: tuple[str, ...]
    runs: int
    aliases: tuple[str, ...]
    reason: str
    # 模型给的通用释义（"这个词是什么"）；空串表示这一轮没给，由流水线
    # 用 descriptions.fill_descriptions 批量补写。
    desc: str = ""
    scope: str | None = None
    # 目标目录 ID（逐目录生成时直接绑定请求的目录）。
    dir_raw: str | None = None
    candidate_id: str | None = None
