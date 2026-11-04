"""标签结果入库服务：ProducedTag → asset_tag 表的 upsert 语义（Task 7）。

阶段 1 设计的核心重跑语义在这里落地：同一资产生成结果重跑时——
- 新标签 → 插入 pending 行；
- 命中审核已定状态 → 跳过什么都不动（skipped_approved+1）；
- 命中其他状态（pending/...）→ 覆盖更新为本次产出。

已定状态两 origin 统一为 {approved, rejected, merged}（阶段 3 B3 拍板
扩入 rejected；语义即"人工已定集"——审核时人工给出终态决定，重跑覆盖
会毁掉审核成果、驳回的候选不得被自动复活）：merged = 审核时人工合并到
标准标签的决定，rejected = 审核时人工驳回，与 approved 同属"人工已定"，
重跑覆盖会毁掉审核成果；计划 catalog 侧只写 approved 系计划文本缺陷。

实现注意：
- **服务内不 commit**：调用方（Task 8 runner）统一事务控制，保证
  一次任务的全部写入要么整体落库要么整体回滚（Review Focus 1 原子性）；
  本服务内的查询依赖 SQLAlchemy autoflush，同事务内可见；
- **generated 行的候选名取 tag_name**：ProducedTag 无 candidate_name
  字段（T6 审查附注），persist 时 AssetTag.candidate_name = tag_name，
  候选类型/描述取 candidate_type/candidate_description；
- catalog 行 candidate_* 一律 None（模型契约：目录挂载行只认 tag_id）；
- **run_id 参数当前不入库**（asset_tag 无对应列），仅为契约签名完整
  保留——与 Task 8 runner 的 `run_id = f"{task_id}:{uuid4().hex[:6]}"`
  对齐，未来审计列扩展时无需改签名；
- 时间戳显式赋 UTC now（时间戳列无默认值，Task 3 审查遗留注意项）。

分层红线：本模块不得 import fastapi（设计文档 §9.4）。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from tagplatform.db.models import AssetTag
from tagplatform.generation.base import ProducedTag


@dataclass
class PersistSummary:
    """入库结果摘要：新插行数 / 覆盖更新行数 / 跳过行数（审核已定，
    两 origin 统一为 approved/rejected/merged）。"""

    inserted: int
    updated: int
    skipped_approved: int


# 审核已定状态（人工已定集）：重跑一律跳过（approved/rejected/merged 同属
# 人工已定——阶段 3 B3 拍板扩入 rejected，重跑不得复活审核驳回的候选；
# 两 origin 统一，计划 catalog 侧漏写 merged 系计划文本缺陷）
FINALIZED_STATUSES = ("approved", "rejected", "merged")


def _evidence_json(tag: ProducedTag) -> str:
    """Evidence 列表 → JSON 数组字符串（asdict 逐条展开，中文不转义）。"""
    return json.dumps([asdict(e) for e in tag.evidence], ensure_ascii=False)


def persist_tags(
    session: Session,
    asset_id: str,
    task_id: str,
    tags: list[ProducedTag],
    run_id: str,
) -> PersistSummary:
    """将 Producer 产出逐条 upsert 进 asset_tag（不 commit，调用方提交）。

    逐条规则（brief 契约 + controller ruling + 阶段 3 B3 拍板）：
    - origin="catalog"：按 (asset_id, tag_id) 查——无则插入 pending；
      有且已定（approved/rejected/merged）跳过不动；其他状态覆盖更新
      （candidate_* 置 None）。
    - origin="generated"：按 (asset_id, candidate_name) 同规则（已定
      跳过集合同为 approved/rejected/merged，重跑不得复活人工已定或
      人工驳回的候选）。
    - 更新刷新 relevance/confidence/evidence_json/task_id/origin/candidate_*
      与 updated_at；插入行 created_at/updated_at 显式赋值。
    """
    now = datetime.now(timezone.utc)
    summary = PersistSummary(inserted=0, updated=0, skipped_approved=0)

    for tag in tags:
        # 契约校验前置（零副作用）：非法 origin / catalog 缺 tag_id 直接拒绝。
        # 后者的危害：按 (asset_id, tag_id=None) 查询会误命中 generated 行
        # （tag_id 为 NULL），造成跨 origin 覆盖。
        if tag.origin == "catalog":
            if not tag.tag_id:
                raise ValueError("catalog 标签缺少 tag_id（ProducedTag 契约要求必填）")
            row = (
                session.query(AssetTag)
                .filter_by(asset_id=asset_id, tag_id=tag.tag_id)
                .one_or_none()
            )
        elif tag.origin == "generated":
            row = (
                session.query(AssetTag)
                .filter_by(asset_id=asset_id, candidate_name=tag.tag_name)
                .one_or_none()
            )
        else:
            raise ValueError(
                f"未知 origin: {tag.origin!r}（合法取值 catalog|generated）"
            )

        # 已定（approved/rejected/merged，两 origin 统一）→ 跳过什么都不动
        if row is not None and row.review_status in FINALIZED_STATUSES:
            summary.skipped_approved += 1
            continue

        # generated 行候选名取 tag_name（ProducedTag 无 candidate_name 字段）；
        # catalog 行 candidate_* 一律 None
        if tag.origin == "catalog":
            tag_id_value: str | None = tag.tag_id
            candidate_name = None
            candidate_type = None
            candidate_description = None
        else:
            tag_id_value = None
            candidate_name = tag.tag_name
            candidate_type = tag.candidate_type
            candidate_description = tag.candidate_description

        if row is None:
            # 不存在 → 插入 pending（时间戳显式赋值）
            session.add(AssetTag(
                asset_id=asset_id,
                tag_id=tag_id_value,
                candidate_name=candidate_name,
                candidate_type=candidate_type,
                candidate_description=candidate_description,
                origin=tag.origin,
                relevance_score=tag.relevance_score,
                confidence=tag.confidence,
                evidence_json=_evidence_json(tag),
                review_status="pending",
                level=tag.level,
                task_id=task_id,
                created_at=now,
                updated_at=now,
            ))
            summary.inserted += 1
        else:
            # 存在且非已定 → 覆盖更新（tag_id 为查询键不动，其余按契约刷新）
            row.origin = tag.origin
            row.relevance_score = tag.relevance_score
            row.confidence = tag.confidence
            row.evidence_json = _evidence_json(tag)
            row.task_id = task_id
            row.candidate_name = candidate_name
            row.candidate_type = candidate_type
            row.candidate_description = candidate_description
            row.level = tag.level
            row.updated_at = now
            summary.updated += 1

    return summary
