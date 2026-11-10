"""质量评估环节（当前为占位实现：原样透传，不过滤任何标签）。

规划职责（本期不实现，接口先立）：
- 校验标签与证据的一致性（evidence 覆盖的场景是否真实存在、时间区间
  是否落在内容 JSON 内）；
- 置信度/相关性双门槛的复核（producer 侧的 policy 截断之外的语义层
  判断，如证据为空的高置信标签降权）；
- 与库内目录的一致性检查（closed 行 tag_id 必须存在且 active）。

占位语义：返回 `FilterOutcome(tags=list(tags))`，`note` 标记"未评估"，
方便在日志/测试里区分"没丢东西"与"根本没评估"。未来落地时只改本模块
的 `filter`，runner 与责任链骨架零改动。
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from tagplatform.filtering.base import FilterContext, FilterOutcome
from tagplatform.generation.base import ProducedTag

logger = logging.getLogger(__name__)


class QualityFilter:
    """质量评估环节：占位实现，原样透传。"""

    name = "quality"

    def filter(
        self,
        tags: list[ProducedTag],
        context: FilterContext,
        session: Session,
    ) -> FilterOutcome:
        # 占位：评估逻辑未接入，全部放行（不丢弃、不改写）
        logger.info(
            "质量评估：任务 %s 资产 %s 模式 %s，收到 %d 条候选 %s"
            "，占位实现未评估，全部放行",
            context.task_id, context.asset_id, context.mode, len(tags),
            [tag.tag_name for tag in tags],
        )
        return FilterOutcome(tags=list(tags), note="quality: 占位实现，未评估")