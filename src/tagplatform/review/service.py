"""人工审核服务：`pre_asset_tag` 待审行的审核动作。

动作语义（v2 设计文档 §6.2 + 本期目录归属口径）：
- approve：`review_status → approved`；若该行是库外新词，按候选目录登记一条
  "待写 tag_define"的入库预览行（`tagplatform.ingest`，本地 JSON）；
- reject：`review_status → rejected`；
- modify：payload 字段写回行内（名称/描述/目录），随后**直接按通过处理**
  ——v2 把“修改”定义为“改完即可入库”，不再回 pending 重排；
- merge：并入库内既有标签——`tag_id` 赋目标标签、候选字段清空、状态
  approved；原候选名登记为目标标签的**别名预览**（将来写
  `tag_define.aliases`）。目标标签必须存在于标签库（JSON，不读 DB 表）。

前置与错误（API 层映射 404/409/422）：
- 行不存在 → ValueError("标签行不存在")（API 层 404）；
- review_status != "pending" → ConflictError("该标签已被处理")（API 层 409）；
- 落库撞部分唯一索引 → ConflictError("该资产已挂此标签")（API 层 409）；
- 非法 action / modify 无可改字段 / 字段为空或超长 / merge 缺目标或目标
  不在标签库 / 新词没有归属目录 → ValueError（API 层 422）。

**服务内 commit**：审核是用户操作，即时落库；批量审核依赖“逐条独立提交”。
入库预览写在提交之后：即使 JSON 写失败，审核结果已生效，可重放补齐。

分层红线：本模块不得 import fastapi（设计文档 §9.4）。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tagplatform.db.models import AssetTag, PreAssetTag, TagDefine
from tagplatform.ingest import record_alias, record_tag_definition
from tagplatform.library import TagLibrary, try_load_library

logger = logging.getLogger(__name__)


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


def _library_or_raise(session: Session) -> TagLibrary:
    library = try_load_library(session)
    if library is None:
        raise ValueError("标签库未就绪，无法执行入库动作")
    return library


def _ingest_new_tag(session: Session, row: PreAssetTag,
                    library: TagLibrary, reviewer: str) -> dict | None:
    """把一条已通过的新词写进 `tag_define`（无新词语义时返回 None）。"""
    if row.tag_id or not row.candidate_name:
        return None
    if not row.candidate_dir_id:
        raise ValueError(
            f"新标签「{row.candidate_name}」没有归属目录，无法入库——"
            "请先用 modify 指定 dir_id")
    directory = library.directory(row.candidate_dir_id)
    if directory is None:
        raise ValueError(f"目录不存在于标签库：{row.candidate_dir_id}")
    # 目录校验（生成侧建候选时已挡过一遍，入库侧再挡一次：目录可能在两轮之间
    # 被改成静态目录，或人工 modify 换了目录）。两条规则：
    # 1) 只有动态目录允许审核后扩充新标签，静态目录的取值是固定的；
    # 2) 候选行的挂载粒度必须落在目录的 applicable_scope 里（目录可能挂多个
    #    粒度，逗号分隔），否则入的是挂不上资产的标签。
    if not directory.allows_new_tags:
        raise ValueError(
            f"目录「{directory.dir_name}」是静态目录（dir_static），不允许扩充新标签")
    scope = library.scope_of(row.candidate_dir_id)
    allowed = {s.strip() for s in (scope or "").split(",") if s.strip()}
    if row.target_type not in allowed:
        raise ValueError(
            f"目录「{directory.dir_name}」的挂载粒度是 {scope}，"
            f"与候选行的 {row.target_type} 不一致")
    source = {
        "asset_id": row.asset_id,
        "pre_asset_tag_id": row.id,
        "task_id": row.task_id,
        "reviewer": reviewer,
        "target_type": row.target_type,
        "target_id": row.target_id,
    }
    return record_tag_definition(
        session, library,
        tag_name=row.candidate_name,
        dir_id=row.candidate_dir_id,
        description=row.candidate_description or "",
        aliases=[row.candidate_alias] if row.candidate_alias else [],
        source=source,
    )


def _mount_asset_tag(session: Session, row: PreAssetTag, *,
                     tag_id: str, tag_name: str, now: datetime) -> bool:
    """审核通过后写正式挂载行 `asset_tag`（已挂则跳过，返回是否新写）。

    这是"审核生效 → 正式挂载"的第二步：`pre_asset_tag` 留候选与证据，
    `asset_tag` 才是资产详情/标签库读的挂载关系。同资产同标签已存在时
    幂等跳过（重复审核、或同片两条候选指向同一库内标签）。
    """
    if not tag_id:
        raise ValueError("挂载需要 tag_id")
    exists = (session.query(AssetTag.id)
              .filter(AssetTag.asset_id == row.asset_id,
                      AssetTag.target_type == row.target_type,
                      AssetTag.target_id == row.target_id,
                      AssetTag.tag_id == tag_id)
              .first())
    if exists is not None:
        return False
    session.add(AssetTag(
        asset_id=row.asset_id,
        target_type=row.target_type,
        target_id=row.target_id,
        tag_id=tag_id,
        # 冗余拷贝取 define 现值（doc §3.6/§6.3——candidate_* 不参与）
        tag_name=tag_name,
        created_at=now,
        updated_at=now,
    ))
    return True


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

    library = _library_or_raise(session)
    merge_to: str | None = None
    if action == "merge":
        merge_to = (payload or {}).get("merge_to_tag_id")
        if not merge_to:
            raise ValueError("merge 动作必须提供 merge_to_tag_id")
        if library.tag(merge_to) is None:
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
        dir_id = new_fields.get("candidate_dir_id")
        if dir_id is not None and library.directory(dir_id) is None:
            raise ValueError(f"目录不存在于标签库：{dir_id}")

    # 新词入库必须有归属目录：通过/修改前就挡住（返回 422），不留半成品
    if action in ("approve", "modify") and row.tag_id is None:
        final_name = new_fields.get("candidate_name", row.candidate_name)
        final_dir = new_fields.get("candidate_dir_id", row.candidate_dir_id)
        if final_name and not final_dir:
            raise ValueError(
                f"新标签「{final_name}」没有归属目录，无法入库——"
                "请先用 modify 指定 dir_id")
        if final_dir and final_name:
            directory = library.directory(final_dir)
            if directory is None:
                raise ValueError(f"目录不存在于标签库：{final_dir}")
            # 目录校验（与生成侧同口径，入库前再挡一次：目录可能在这两轮之间
            # 被改成静态，或人工 modify 换了目录/粒度）：
            # 1) 只有动态目录能扩充新标签，静态目录取值固定；
            # 2) 候选行的挂载粒度必须在目录的 applicable_scope 里。
            if not directory.allows_new_tags:
                raise ValueError(
                    f"目录「{directory.dir_name}」是静态目录（dir_static），"
                    "不允许扩充新标签")
            scope = library.scope_of(final_dir)
            allowed = {s.strip() for s in (scope or "").split(",") if s.strip()}
            if row.target_type not in allowed:
                raise ValueError(
                    f"目录「{directory.dir_name}」的挂载粒度是 {scope}，"
                    f"与候选行的 {row.target_type} 不一致")

    # ---- 执行动作（前置已全部通过）----
    now = datetime.now(timezone.utc)
    comment = (payload or {}).get("comment")
    merged_alias: str | None = None

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
        merged_alias = row.candidate_name
        row.tag_id = merge_to
        row.candidate_name = None
        row.candidate_description = None
        row.candidate_dir_id = None
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
        session.rollback()
        raise ConflictError("该资产已挂此标签")

    # ---- 入库（审核已生效，单独提交；写失败只记日志，可人工重放）----
    source = {"asset_id": row.asset_id, "pre_asset_tag_id": row.id,
              "task_id": row.task_id, "reviewer": reviewer}
    try:
        if merged_alias and merge_to:
            record_alias(session, tag_id=merge_to, alias=merged_alias,
                         source=source)
            define = session.get(TagDefine, merge_to)
            if define is not None:
                _mount_asset_tag(session, row, tag_id=define.tag_id,
                                 tag_name=define.tag_name, now=now)
        elif row.review_status == "approved" and not row.tag_id:
            ingested = _ingest_new_tag(session, row, library, reviewer)
            if ingested:
                _mount_asset_tag(session, row, tag_id=str(ingested["tag_id"]),
                                 tag_name=str(ingested["tag_name"]), now=now)
        elif row.review_status == "approved" and row.tag_id:
            # 命中库内标签的候选（judge 判重后替换成库内名 / 人工 modify 到库内名）：
            # 原候选名作为别名落进 `tag_define.aliases`，再写正式挂载行。
            define = session.get(TagDefine, row.tag_id)
            if define is None:
                raise ValueError(f"标签定义不存在：{row.tag_id}")
            alias = (row.candidate_alias or "").strip()
            if alias and alias != define.tag_name:
                record_alias(session, tag_id=define.tag_id, alias=alias,
                             source=source)
            _mount_asset_tag(session, row, tag_id=define.tag_id,
                             tag_name=define.tag_name, now=now)
        else:
            return row
        session.commit()
    except (OSError, ValueError, IntegrityError) as exc:
        session.rollback()
        logger.warning("入库写入失败（审核已生效）：%s", exc)
    return row


# ---------- tag_name 冗余拷贝刷新（doc §6.3 契约助手） ----------

def refresh_mounted_tag_names(session: Session, tag_id: str) -> int:
    """把某标签定义的最新 tag_name 同步到其全部 `asset_tag` 挂载行。

    doc §6.3：`asset_tag.tag_name` 是 `tag_define.tag_name` 的冗余拷贝
    （val_fixed），定义侧改名/合并后调用本助手消除两处漂移。调用方先改
    `TagDefine.tag_name`（自行写 tag_change_log 留证）并提交；本助手只做
    挂载侧批量 UPDATE，flush 不 commit，与调用方同事务原子落库。
    定义不存在 → ValueError（API 层映射 422）。
    """
    define = session.get(TagDefine, tag_id)
    if define is None:
        raise ValueError(f"标签定义不存在：{tag_id}")
    rowcount = session.query(AssetTag).filter(AssetTag.tag_id == tag_id).update(
        {
            AssetTag.tag_name: define.tag_name,
            AssetTag.updated_at: datetime.now(timezone.utc),
        },
        synchronize_session=False,
    )
    session.flush()
    return rowcount
