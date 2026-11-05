"""标签结果入库服务：ProducedTag → pre_asset_tag 的 INSERT OR IGNORE（v2 Task 6）。

v2 重跑语义（doc §6.1）在这里收敛为最薄的一层：本服务**只做逐行
INSERT OR IGNORE**——旧行的处理（pending 删旧）是执行器 `_clear_pending`
的职责（写结果前删该资产同 origin 的全部 pending 行，closed 重跑不清
open 的待审行）；本服务对已存在行的唯一动作是"撞部分唯一索引即跳过"：

- 无旧行 → 插入 pending（审核队列）；
- 撞 approved/rejected 终态行 → 跳过什么都不动（skipped_finalized）——
  终审不被重跑覆盖、驳回的候选不得被自动复活（doc §6.1；v2 人工已定集
  收窄为两态，merge 的终态都是 approved，doc §6.2）；
- 撞 pending 行（直调未清理 / 跨 origin 残留）→ 跳过不覆盖
  （deleted_pending 计数）——删旧是 runner 的同 origin 职责。

插入真打数据库（savepoint 内 flush），冲突由 pre_asset_tag 的两个部分
唯一索引裁决（ux_pre_tag_catalog 按 tag_id、ux_pre_tag_candidate 按
candidate_name），冲突发生后才回查旧行分类——非先查后插的竞态写法。

v2 词表：origin 取值 closed|open（ProducedTag.origin 原样落列；v1 的
catalog/generated 词退役，未知值 ValueError 零副作用拒绝）。closed 意味
着命中目录内定义，tag_id 必填。列契约（doc §3.5）：target_type='content'
（本期唯一启用粒度）、target_id=asset_id 本身、candidate_name 仅 open 行
（closed 行恒空，展示名由 tag_id 解析）、candidate_description 仅 open
自由候选（归一命中行空）、candidate_dir_id 生成时不赋值（doc §6.4）、
candidate_type 不入库（v2 无此列，ProducedTag 字段保留供 mtg 审计）。

实现注意：
- **服务内不 commit**：调用方（runner）统一事务控制，保证一次任务的
  全部写入要么整体落库要么整体回滚（任务级事务原子性）；
- **run_id 参数当前不入库**（pre_asset_tag 无对应列），仅为契约签名
  完整保留——与 runner 的 `run_id = f"{task_id}:{uuid4().hex[:6]}"`
  对齐，未来审计列扩展时无需改签名；
- 时间戳显式赋 UTC now（时间戳列无默认值，Task 3 审查遗留注意项）。

分层红线：本模块不得 import fastapi（设计文档 §9.4）。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tagplatform.db.models import PreAssetTag
from tagplatform.generation.base import ProducedTag

# 审核已定状态（人工已定集）：重跑一律跳过——v2 收窄为 approved/rejected
# （merge 退役：并入既有/合入新建的终态都是 approved，doc §6.2；驳回的
# 候选不得被重跑复活，doc §6.1）
FINALIZED_STATUSES = ("approved", "rejected")

# origin 词表（v2）：closed|open；其余取值（含 v1 残留词 catalog/generated）
# ValueError 零副作用拒绝
_ORIGINS = ("closed", "open")


@dataclass
class PersistSummary:
    """入库结果摘要：
    - inserted：新插入的 pending 行数；
    - deleted_pending：撞 pending 行被索引拒入的次数——重跑替换中这些行
      的删旧由 runner 的 `_clear_pending` 负责（本服务不删行；直调未清理
      或跨 origin 残留时在此可见，runner 编排下恒为 0）；
    - skipped_finalized：撞 approved/rejected 终态行被跳过的次数。
    """

    inserted: int
    deleted_pending: int
    skipped_finalized: int


def _evidence_json(tag: ProducedTag) -> str:
    """Evidence 列表 → JSON 数组字符串（asdict 逐条展开，中文不转义）。"""
    return json.dumps([asdict(e) for e in tag.evidence], ensure_ascii=False)


def _find_conflict(session: Session, asset_id: str,
                   tag: ProducedTag) -> PreAssetTag | None:
    """定位撞唯一索引的旧行（冲突发生后回查分类，非先查后插）。

    查询作用域与部分唯一索引一致（doc §3.5）：tag_id 非空行走
    ux_pre_tag_catalog（asset×实体×tag_id）；tag_id 为空的候选行走
    ux_pre_tag_candidate（tag_id IS NULL 且 candidate_name 非空）。
    """
    q = session.query(PreAssetTag).filter(
        PreAssetTag.asset_id == asset_id,
        PreAssetTag.target_type == "content",
        PreAssetTag.target_id == asset_id,
    )
    if tag.tag_id:
        return q.filter(PreAssetTag.tag_id == tag.tag_id).one_or_none()
    return q.filter(
        PreAssetTag.tag_id.is_(None),
        PreAssetTag.candidate_name == tag.tag_name,
    ).one_or_none()


def persist_tags(
    session: Session,
    asset_id: str,
    task_id: str,
    tags: list[ProducedTag],
    run_id: str,
) -> PersistSummary:
    """将 Producer 产出逐行 INSERT OR IGNORE 进 pre_asset_tag（不 commit，
    调用方提交）。

    每行在 savepoint 内插入：撞部分唯一索引时 savepoint 回滚、该行跳过，
    回查旧行分类计数（终态跳过 / pending 占位）；非唯一性冲突（外键等）
    原样上抛——真实数据错误不能吞。
    """
    now = datetime.now(timezone.utc)
    summary = PersistSummary(inserted=0, deleted_pending=0, skipped_finalized=0)

    for tag in tags:
        # 契约校验前置（零副作用）：未知 origin / closed 缺 tag_id 直接拒绝
        if tag.origin not in _ORIGINS:
            raise ValueError(
                f"未知 origin: {tag.origin!r}（合法取值 closed|open）")
        if tag.origin == "closed" and not tag.tag_id:
            raise ValueError("closed 标签缺少 tag_id（命中目录内定义必带）")

        row = PreAssetTag(
            asset_id=asset_id,
            target_type="content",
            target_id=asset_id,
            tag_id=tag.tag_id,
            # candidate_name 仅 open 行（closed 行恒空，展示名由 tag_id 解析）
            candidate_name=(tag.tag_name if tag.origin == "open" else None),
            candidate_dir_id=None,  # 生成时不赋值（doc §6.4，合入时才赋）
            # candidate_description 仅 open 自由候选（归一命中行空，doc §3.5）
            candidate_description=(tag.candidate_description
                                   if tag.tag_id is None else None),
            origin=tag.origin,
            relevance_score=tag.relevance_score,
            confidence=tag.confidence,
            evidence_json=_evidence_json(tag),
            task_id=task_id,
            review_status="pending",
            created_at=now,
            updated_at=now,
        )
        try:
            with session.begin_nested():  # SAVEPOINT：单行插入的冲突隔离
                session.add(row)
                session.flush()
            summary.inserted += 1
        except IntegrityError:
            conflict = _find_conflict(session, asset_id, tag)
            if conflict is None:
                raise  # 非唯一索引冲突（外键等）：数据错误，不能吞
            if conflict.review_status in FINALIZED_STATUSES:
                summary.skipped_finalized += 1
            else:
                summary.deleted_pending += 1

    return summary
