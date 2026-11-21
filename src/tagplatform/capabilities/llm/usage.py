"""LLM 用量审计的统一累加（audit 报告 calls/prompt_tokens/completion_tokens 三键口径）。

原为五处三行式各自维护（mtg 流水线 pipeline/judge/verify/descriptions 各一份
就地累加 + 资产构建 builder 一份返回新 dict），本模块收成两个具名语义：

- `accumulate_usage`：就地累加，含 calls 计数——mtg 流水线审计口径；
- `combined_usage`：合并两次调用的用量为**新 dict**，不计 calls——资产构建的
  重试合并口径（逐场景回传用量，不改入参；两语义刻意分开而不是一个函数加
  开关，调用点读起来才知道改的是谁）。

tokens 键取整数（响应侧可能给浮点/字符串），None 按空处理。
"""
from __future__ import annotations

_TOKEN_KEYS = ("prompt_tokens", "completion_tokens")


def accumulate_usage(total: dict, usage: dict | None = None, *, calls: int = 1) -> None:
    """一次 LLM 调用的用量累加进 total（就地修改）。

    total 约定含 calls/prompt_tokens/completion_tokens 三键（缺了按 0 起步）；
    usage 为响应侧用量 dict，None 按空处理。calls=0 用于"只合并 tokens 不计
    调用数"的场合。
    """
    usage = usage or {}
    total["calls"] = total.get("calls", 0) + calls
    for key in _TOKEN_KEYS:
        total[key] = total.get(key, 0) + int(usage.get(key, 0) or 0)


def combined_usage(a: dict, b: dict) -> dict:
    """两次调用的 usage 合并成新 dict（不修改入参，不计 calls）。"""
    return {**a, **{key: int(a.get(key, 0) or 0) + int(b.get(key, 0) or 0)
                    for key in _TOKEN_KEYS}}
