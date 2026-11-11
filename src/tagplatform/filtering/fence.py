"""信息围栏环节（当前为占位实现：原样透传，不过滤任何标签）。

规划职责（本期不实现，接口先立）：
- 敏感信息拦截：候选标签名/描述/证据文本命中敏感词、剧透口径、未授权
  版权实体时丢弃或改写降级；
- 范围围栏：标签是否允许挂在该资产的粒度/授权范围内（如仅内部可见的
  内容不得产出对外可见标签）；
- 合规留痕：被围栏丢弃的明细应落 mtg 审计文件，而不是 pre_asset_tag
  （doc §6.1：跨轮次质量分析查审计文件，不查待审表）。

占位语义：返回 `FilterOutcome(tags=list(tags))`，`note` 标记"未围栏"。
未来落地时只改本模块的 `filter`，runner 与责任链骨架零改动。
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from tagplatform.filtering.base import FilterContext, FilterOutcome
from tagplatform.generation.base import ProducedTag

logger = logging.getLogger(__name__)


class InfoFenceFilter:
    """信息围栏环节：占位实现，原样透传。"""

    name = "fence"

    def filter(
        self,
        tags: list[ProducedTag],
        context: FilterContext,
        session: Session,
    ) -> FilterOutcome:
        # 占位：围栏规则未接入，全部放行（不丢弃、不改写）
        logger.info(
            "信息围栏：任务 %s 资产 %s 模式 %s，收到 %d 条候选 %s"
            "，占位实现未围栏，全部放行",
            context.task_id, context.asset_id, context.mode, len(tags),
            [tag.tag_name for tag in tags],
        )
        return FilterOutcome(tags=list(tags), note="fence: 占位实现，未围栏")