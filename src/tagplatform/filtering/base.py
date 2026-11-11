"""生成结果过滤责任链：环节协议、过滤上下文与顺序注册表。

定位：生成器（producer）产出 `ProducedTag` 列表后，**不直接进审核队列**
——先按顺序过责任链（默认：质量评估 → 信息围栏），通过的标签才由
`persist_tags` 落 `pre_asset_tag`，被丢弃的标签不入库、不展示。

责任链放在 producer 与 persist 之间（runner 第 7 步）而不是审核侧：
- 过滤属于"生成流水线"的业务判断，与人工审核（approve/reject）是两回事
  ——被过滤的行不应占用审核队列，也不该以 `rejected` 落库（`rejected`
  语义是"人工驳即终审、重跑跳过"，见 doc §6.1）；
- 环节需要随任务事务：环节抛错 → 任务 failed、回滚零半截写入，与
  persist 的失败语义一致；
- 环节可以丢（drop）也可以改写（重算 confidence / 抹敏感描述），契约
  一律是"返回新的 ProducedTag 列表"，扩展时无需改 runner。

扩展承诺（同 `generation/base.py` 的反失败承诺）：新增一个环节 = 新实现
类 + `register_filter` 注册，调度与审核/前端自动兼容。

注册表 `_CHAIN` 是模块级有序 list（顺序即语义，不能改成 dict）；默认
两环节在包 `__init__` 导入时按"质量评估 → 信息围栏"注册。

分层红线：本模块不得 import fastapi（设计文档 §9.4）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.orm import Session

from tagplatform.generation.base import ProducedTag
from tagplatform.schemas import ContentJSON

logger = logging.getLogger(__name__)


@dataclass
class FilterContext:
    """一次过滤的只读上下文：任务标识 + 内容（环节按需取用）。

    仅放"环节判断确实需要"的字段；未来环节需要策略/召回结果时按需扩列
    （dataclass 加字段对既有环节向后兼容）。
    """

    task_id: str
    asset_id: str
    mode: str
    content: ContentJSON


@dataclass
class FilterOutcome:
    """单环节输出：通过本环节的标签列表 + 环节备注。

    `tags` 允许是原列表的子集（丢弃）或被改写后的新列表（重算分数等）；
    本环节新增的标签不计入统计（责任链是过滤器，不是生成器）。
    """

    tags: list[ProducedTag]
    note: str = ""


class TagFilter(Protocol):
    """过滤环节协议。

    约定：
    - `name` 唯一标识环节（同一 name 不可重复注册）；
    - `filter` 返回 `FilterOutcome`；允许返回空列表（全部丢弃，任务仍算
      成功——同"生成不出来不算失败"）；
    - 环节内异常直接上抛：由 runner 的任务级事务统一落 failed 并回滚。
    """

    name: str

    def filter(
        self,
        tags: list[ProducedTag],
        context: FilterContext,
        session: Session,
    ) -> FilterOutcome:
        """过滤一批候选标签。"""
        ...


@dataclass
class StageReport:
    """单环节执行报告（runner 记日志用，不落库）。

    dropped = 进环节条数 − 出环节条数（可为负：环节改写后增加了条目，
    但责任链不鼓励这种用法）。
    """

    name: str
    kept: int
    dropped: int
    note: str


# 模块级有序注册表：环节按注册顺序执行（顺序即语义）
_CHAIN: list[TagFilter] = []


def register_filter(tag_filter: TagFilter) -> None:
    """注册环节（追加到链尾）；name 重复抛 ValueError。"""
    if any(stage.name == tag_filter.name for stage in _CHAIN):
        raise ValueError(
            f"过滤环节 name 已注册: {tag_filter.name}"
            f"（当前链: {list_filters()}）"
        )
    _CHAIN.append(tag_filter)


def list_filters() -> list[str]:
    """列出责任链环节名（按执行顺序，非排序）。"""
    return [stage.name for stage in _CHAIN]


def run_filter_chain(
    tags: list[ProducedTag],
    context: FilterContext,
    session: Session,
) -> tuple[list[ProducedTag], list[StageReport]]:
    """按注册顺序把候选标签过一遍责任链。

    返回 (通过的标签, 每环节报告)。空链（测试或显式清空）原样透传。
    不吞异常：任一环节抛错即向上传播，由任务级事务落 failed。
    """
    reports: list[StageReport] = []
    current = list(tags)
    logger.info(
        "过滤责任链开始：任务 %s 资产 %s 模式 %s，输入 %d 条候选，环节 %s",
        context.task_id, context.asset_id, context.mode,
        len(current), list_filters() or "（空链）",
    )
    for index, stage in enumerate(_CHAIN, start=1):
        incoming_tags = list(current)
        incoming = len(incoming_tags)
        # 进环节前打出传入标签名，便于对照哪一批被环节吃掉
        logger.debug(
            "过滤环节 [%d/%d] %s 开始：输入 %d 条 %s",
            index, len(_CHAIN), stage.name, incoming,
            [tag.tag_name for tag in incoming_tags],
        )
        outcome = stage.filter(current, context, session)
        current = list(outcome.tags)
        dropped = incoming - len(current)
        reports.append(StageReport(
            name=stage.name,
            kept=len(current),
            dropped=dropped,
            note=outcome.note,
        ))
        logger.info(
            "过滤环节 [%d/%d] %s 完成：输入 %d 条 → 保留 %d 条、丢弃 %d 条"
            "（%s）",
            index, len(_CHAIN), stage.name, incoming,
            len(current), dropped, outcome.note or "无备注",
        )
        if dropped > 0:
            # 丢弃明细只记日志：被过滤的行不入库（doc §6.1 审计走 mtg 文件）
            kept_names = [tag.tag_name for tag in current]
            dropped_names = [tag.tag_name for tag in incoming_tags
                             if tag.tag_name not in kept_names]
            logger.info(
                "过滤环节 %s 丢弃明细：%s",
                stage.name,
                dropped_names or "（同名条目，仅计数变化）",
            )
    logger.info(
        "过滤责任链结束：任务 %s 资产 %s，%d 条候选 → %d 条通过",
        context.task_id, context.asset_id, len(tags), len(current),
    )
    return current, reports