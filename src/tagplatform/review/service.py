"""人工审核服务：`pre_asset_tag` 待审行的审核动作。

动作语义（v2 设计文档 §6.2）：
- approve：`review_status → approved`，审核人/备注落在行内；
- reject：`review_status → rejected`；
- modify：payload 字段写回行内（名称/描述/目录），随后**直接按通过处理**
  ——v2 把“修改”定义为“改完即可入库”，不再回 pending 重排（修改内容以
  `review_comment` 留痕）；
- merge：并入库内既有标签——`tag_id` 赋目标标签、候选字段清空、状态
  approved（终态与 approve 一致，区别只是标签由人工指定）。本期不支持
  “合入新建标签”（标签库写入口径未定），传入即 422。

前置与错误（API 层映射 404/409/422）：
- 行不存在 → ValueError("标签行不存在")（API 层 404）；
- review_status != "pending" → ConflictError("该标签已被处理")（API 层 409，
  并发保护：同一行只有一个审核动作能生效）；
- 落库撞部分唯一索引 → ConflictError("该资产已挂此标签")（API 层 409）：
  merge 目标标签同资产已有行，或 modify 改名与同资产已有候选重名；
- 非法 action / modify 无可改字段 / 字段为空或超长 / merge 缺目标或目标
  不在标签表 → ValueError（API 层 422）。

**服务内 commit**：审核是用户操作，即时落库；批量审核依赖“逐条独立提交”，
单条失败不影响已成功条目。载荷校验全部前置（零副作用）。

分层红线：本模块不得 import fastapi（设计文档 §9.4）。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tagplatform.db.models import PreAssetTag, TagCatalog


class ConflictError(Exception):
    """审核冲突：目标行已被处理（review_status 非 pending），或落库撞部分
    唯一索引（同资产同实体已挂该标签/已有同名候选）。"""


# modify 动作 payload 键 → 行内字段映射（canonical 键在前，展示别名在后）
_MODIFY_FIELDS = (
    ("candidate_name", "tag_name"),
    ("candidate_description", "description"),
    ("candidate_dir_id", "dir_id"),
)

_LEGAL_ACTIONS = ("approve", "reject", "modify", "merge")

# modify 字符串字段长度上限（strip 后按字符数计，超长 → ValueError → 422）
_MODIFY_MAX_LEN = 200


def apply_review(session: Session, row_id: int, action: str,
                 reviewer: str, payload: dict | None) -> PreAssetTag:
    """对 pre_asset_tag 行执行审核动作，服务内提交后返回该行。

    前置不满足/载荷非法按模块 docstring 抛错（ValueError/ConflictError，
    零副作用——校验全部先于任何行变更）。
    """
    row = session.get(PreAssetTag, row_id)
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
            raise ValueError(f"目标标签不存在于标签库：{merge_to}")

    new_fields: dict[str, str] = {}
    if action == "modify":
        for canonical, alias in _MODIFY_FIELDS:
            value = (payload or {}).get(canonical)
            if value is None:
                value = (payload or {}).get(alias)
            if value is not None:
                if not isinstance(value, str):
                    raise ValueError(f"修改字段必须为字符串：{canonical}")
                stripped = value.strip()
                if not stripped:
                    raise ValueError("修改字段不能为空")
                if len(stripped) > _MODIFY_MAX_LEN:
                    raise ValueError(
                        f"修改字段超长：{canonical}"
                        f"（上限 {_MODIFY_MAX_LEN} 字符）")
                new_fields[canonical] = stripped
        if not new_fields:
            raise ValueError(
                "modify 动作至少提供一个可修改字段（candidate_name/tag_name、"
                "candidate_description/description、candidate_dir_id/dir_id）")

    # ---- 执行动作（前置已全部通过）----
    now = datetime.now(timezone.utc)
    comment = (payload or {}).get("comment")

    if action == "approve":
        row.review_status = "approved"
    elif action == "reject":
        row.review_status = "rejected"
    elif action == "modify":
        for field, value in new_fields.items():
            setattr(row, field, value)
        # v2：修改即“改后通过”，终态 approved
        row.review_status = "approved"
    else:  # merge：并入既有标签（目标已前置校验）
        row.tag_id = merge_to
        row.candidate_name = None
        row.candidate_description = None
        row.review_status = "approved"

    row.reviewer_id = reviewer
    if isinstance(comment, str) and comment.strip():
        row.review_comment = comment.strip()
    row.updated_at = now

    try:
        session.commit()  # 审核是用户操作，服务内即时落库
    except IntegrityError:
        # 撞部分唯一索引（ux_pre_tag_catalog / ux_pre_tag_candidate）：
        # merge 到同资产同实体已挂的标签，或 modify 改名与已有候选重名。
        # 回滚丢弃本次行变更（行保持 pending），按冲突语义上抛（API 层 409）
        session.rollback()
        raise ConflictError("该资产已挂此标签")
    return row
