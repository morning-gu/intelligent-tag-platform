"""ProduceTrace：closed 生成管线漏斗追踪的被动采集器（管线追踪 Task 1）。

closed producer 在既有流程点上顺路上报（可选参数 `trace`，None 时走与
旧代码逐字节相同的路径——采集器纯旁观，不参与任何判定、不影响任何
丢弃/选择结果），本模块只负责收集与快照，是 generation 包的叶子：

- 候选：recall 后按批入册（身份/目录/召回分/批次序），结局由 `resolve`
  在各丢弃点回填。outcome 五值枚举 `kept | unmentioned | gate |
  unmountable | m_cap`（dir_asset 目录剔除属召回口径——候选根本入不了
  册，不入 trace）；`unmentioned` 无显式上报点，快照时对结局为 None 的
  候选归一补齐；
- 批次：每批解析成功后记一次（目录/批序/候选数/是否重试），原始输出
  只存最终成功那份（首败原文仅存留于 producer 的 warning 日志）；
- 编造 id：按判定出现次序累积，不去重；
- max_tags：全局输出上限 M（策略原值）。

消费侧（后续任务）：`snapshot()` 供评测 run 目录落盘与 API/前端透出，
`raw_rows()` 专供 llm_raw 文件落盘——原始输出不进 snapshot（即不进
raw_results 正典行、不进 API 载荷）。

分层红线：本模块不得 import generation 包内任何模块（避免与
base/closed/open 成环），也不得 import fastapi（设计文档 §9.4）。
"""

from __future__ import annotations

from dataclasses import dataclass

# 快照对"没有结局上报"的候选的归一值（召回在册但模型未提及）
OUTCOME_UNMENTIONED = "unmentioned"


@dataclass
class TraceCandidate:
    """单条召回候选的追踪行。

    身份四字段（tag_id/tag_name/dir_id/recall）与批次序在入册时定死；
    结局三字段（outcome/applicability/sf）由 `resolve` 回填，末次为准。
    """

    tag_id: str
    tag_name: str
    dir_id: str
    recall: float
    batch: int
    outcome: str | None = None
    applicability: int | None = None
    sf: float | None = None


class ProduceTrace:
    """closed produce 全流程漏斗的被动收集器。

    采集纪律（与 producer 的接口约定）：
    - `add_candidates` 按批入册，dict 按 add 序保序（快照候选行序 =
      召回批次序）；recall 按 tag_id 去重，正常一次 produce 内不重复；
    - `resolve` 同 tag_id 重复调用末次为准（先中间态后终局的覆盖合法，
      三个结局字段整行覆盖）；未注册 tag_id 为 no-op（编造 id 不经
      resolve，走 `note_fabricated`）；
    - `snapshot()` 返回深拷贝 dict（消费方改不动内部状态），批次行不带
      原始输出。
    """

    def __init__(self) -> None:
        self._candidates: dict[str, TraceCandidate] = {}
        self._batches: list[dict] = []
        self._fabricated: list[str] = []
        # 未上报时为 0；producer 流程恒在候选入册前 set_max_tags
        self._max_tags: int = 0

    def set_max_tags(self, m: int) -> None:
        """记录全局输出上限 M（策略原值；选择阶段的钳负兜底不改追踪值）。"""
        self._max_tags = m

    def add_candidates(self, rows: list[TraceCandidate]) -> None:
        """按批入册候选行。"""
        for row in rows:
            self._candidates[row.tag_id] = row

    def note_batch(self, dir_id: str, batch: int, n: int,
                   parse_retried: bool, raw: str) -> None:
        """记录一个解析成功的批次；raw = 最终成功那次输出的全文。"""
        self._batches.append({
            "dir_id": dir_id,
            "batch": batch,
            "n": n,
            "parse_retried": parse_retried,
            "content": raw,
        })

    def note_fabricated(self, tag_id: str) -> None:
        """记录一个编造/越组 id（按判定出现次序累积，不去重）。"""
        self._fabricated.append(tag_id)

    def resolve(self, tag_id: str, outcome: str, *,
                applicability: int | None = None,
                sf: float | None = None) -> None:
        """回填候选结局；同 tag_id 末次为准，未注册 tag_id 静默 no-op。"""
        row = self._candidates.get(tag_id)
        if row is None:
            return
        row.outcome = outcome
        row.applicability = applicability
        row.sf = sf

    def snapshot(self) -> dict:
        """深拷贝快照；outcome None 归一 "unmentioned"，批次行不带 content。"""
        return {
            "max_tags": self._max_tags,
            "candidates": [
                {
                    "tag_id": row.tag_id,
                    "tag_name": row.tag_name,
                    "dir_id": row.dir_id,
                    "recall": row.recall,
                    "batch": row.batch,
                    "outcome": (row.outcome if row.outcome is not None
                                else OUTCOME_UNMENTIONED),
                    "applicability": row.applicability,
                    "sf": row.sf,
                }
                for row in self._candidates.values()
            ],
            "batches": [
                {key: b[key]
                 for key in ("dir_id", "batch", "n", "parse_retried")}
                for b in self._batches
            ],
            "fabricated": list(self._fabricated),
        }

    def raw_rows(self) -> list[dict]:
        """LLM 原始输出行（含 content=成功份全文），专供 llm_raw 落盘。"""
        return [dict(b) for b in self._batches]
