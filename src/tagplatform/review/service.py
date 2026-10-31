"""人工审核服务：asset_tag 结果行的四动作审核（Task 4）。

动作语义（brief 契约）：
- approve/reject：review_status → approved/rejected（人工已定终态，
  重跑保护集成员——persist 侧 FINALIZED_STATUSES 跳过）；
- modify：payload 字段映射到 AssetTag 行上现有语义字段（名称/类型/描述
  → candidate_name/candidate_type/candidate_description；generated 行的
  展示字段即 candidate_*，catalog 行按计划"改 AssetTag 上现有语义字段"
  最小化——tag_name/tag_type/description 别名同样落 candidate_*，不触碰
  共享的 tag_catalog 字典），status 回 pending（修改后重新进队）；
- merge：行并入既有目录标签——tag_id=merge_to_tag_id、candidate_* 清空、
  status → merged（此后重跑按 (asset_id, tag_id) 命中已定行而跳过）。

前置与错误（API 层映射 404/409/422）：
- 行不存在 → ValueError("标签行不存在")（API 层 404）；
- review_status != "pending" → ConflictError("该标签已被处理")
  （API 层 409——并发保护：同一行只有一个审核动作能生效）；
- 非法 action / modify 无可改字段 / merge 缺 merge_to_tag_id 或目标不在
  tag_catalog → ValueError（API 层 422）。

每次动作写一行 review_record（record_id=`rev_`+uuid4().hex[:8]，
before_json/after_json 为行关键字段快照，comment 取 payload）。

**服务内 commit**：审核是用户操作，即时落库——与 persist（调用方统一
事务控制）不同；批量审核依赖"逐条独立提交"，单条失败不影响已成功条目。
载荷校验全部前置（零副作用）：任何失败都不留下行变更或审核记录。

分层红线：本模块不得 import fastapi（设计文档 §9.4）。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from tagplatform.db.models import AssetTag, ReviewRecord, TagCatalog


class ConflictError(Exception):
    """审核冲突：目标行已被处理（review_status 非 pending——重复提交或并发抢先）。"""


# review_record 快照键集（before_json/after_json：行关键字段前后对照）
_SNAPSHOT_FIELDS = (
    "id", "asset_id", "tag_id", "candidate_name", "candidate_type",
    "candidate_description", "origin", "review_status",
)

# modify 动作 payload 键 → AssetTag 语义字段映射（canonical 键在前，
# 展示字段别名在后；一律落 AssetTag 行内字段，不动共享 tag_catalog）
_MODIFY_FIELDS = (
    ("candidate_name", "tag_name"),
    ("candidate_type", "tag_type"),
    ("candidate_description", "description"),
)

_LEGAL_ACTIONS = ("approve", "reject", "modify", "merge")


def _snapshot(row: AssetTag) -> str:
    """行关键字段快照 → JSON 字符串（review_record before/after 用）。"""
    return json.dumps(
        {field: getattr(row, field) for field in _SNAPSHOT_FIELDS},
        ensure_ascii=False,
    )


def apply_review(session: Session, asset_tag_id: int, action: str,
                 reviewer: str, payload: dict | None) -> AssetTag:
    """对 asset_tag 行执行审核动作并写审核记录，服务内提交后返回该行。

    前置不满足/载荷非法按模块 docstring 抛错（ValueError/ConflictError，
    零副作用——校验全部先于任何行变更）。
    """
    row = session.get(AssetTag, asset_tag_id)
    if row is None:
        raise ValueError("标签行不存在")
    if row.review_status != "pending":
        raise ConflictError("该标签已被处理")

    # ---- 载荷校验（全部前置：失败时零副作用）----
    if action not in _LEGAL_ACTIONS:
        raise ValueError(
            f"未知 action: {action!r}（合法取值 approve|reject|modify|merge）")

    merge_to: str | None = None
    if action == "merge":
        merge_to = (payload or {}).get("merge_to_tag_id")
        if not merge_to:
            raise ValueError("merge 动作必须提供 merge_to_tag_id")
        if session.get(TagCatalog, merge_to) is None:
            raise ValueError(f"目标标签不存在于标签目录：{merge_to}")

    new_fields: dict[str, object] = {}
    if action == "modify":
        for canonical, alias in _MODIFY_FIELDS:
            value = (payload or {}).get(canonical)
            if value is None:
                value = (payload or {}).get(alias)
            if value is not None:
                new_fields[canonical] = value
        if not new_fields:
            raise ValueError(
                "modify 动作至少提供一个可修改字段（candidate_name/tag_name、"
                "candidate_type/tag_type、candidate_description/description）")

    # ---- 执行动作（前置已全部通过）----
    now = datetime.now(timezone.utc)
    before = _snapshot(row)
    comment = (payload or {}).get("comment")

    if action == "approve":
        row.review_status = "approved"
    elif action == "reject":
        row.review_status = "rejected"
    elif action == "modify":
        for field, value in new_fields.items():
            setattr(row, field, value)
        row.review_status = "pending"  # 修改后重新进队（契约显式要求）
    else:  # merge（merge_to 已前置校验存在于 tag_catalog）
        row.tag_id = merge_to
        row.candidate_name = None
        row.candidate_type = None
        row.candidate_description = None
        row.review_status = "merged"
    row.updated_at = now

    session.add(ReviewRecord(
        record_id=f"rev_{uuid.uuid4().hex[:8]}",
        result_id=row.id,
        action=action,
        reviewer_id=reviewer,
        before_json=before,
        after_json=_snapshot(row),
        comment=comment,
        created_at=now,
    ))
    session.commit()  # 审核是用户操作，服务内即时落库
    return row
