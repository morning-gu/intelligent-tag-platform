"""人工审核服务：pre_asset_tag 行的四动作审核（v2 行即日志，doc §6.2）。

动作 → 行操作映射（doc §6.2 逐字执行）：
- approve：pre 行标 approved + reviewer_id/review_comment → 复制挂载到
  asset_tag（tag_name 取 tag_define.tag_name 现值；target_type/target_id
  沿 pre 行；candidate_* 只留在 pre 行可溯）；
- reject：pre 行标 rejected + reviewer/comment（无挂载——驳回即终审）；
- modify（改候选后通过）：更新候选字段（candidate_name/candidate_description，
  payload 键同名；comment 记录修改内容）→ 直接走 approve 流程（一步到位，
  不回 pending——v1"重置 pending 再审"语义退役）；
- merge 两支：
  - 并入既有（payload merge_to_tag_id）：审核员认定候选 = 库内已有标签
    ——pre 行赋 tag_id，candidate_name 原文保留（可溯）→ approve 流程，
    不建新标签；
  - 合入新建（payload new_tag_name + new_dir_id [+ new_description]）：
    库内确无对应——建 TagDefine 行（val_fixed，tag_change_log 记
    action='merge'，metrics 的 merge 计数源）→ pre 行赋 tag_id/
    candidate_dir_id（doc §6.4 赋值时序）→ approve 流程；同目录同名
    （撞 ux_tag_define_dir_name）→ ValueError 提示改走并入既有
    （API 层 422——Review Focus 3：同名候选并入既有是唯一可用路径）。

前置与错误（API 层映射 404/409/422）：
- 行不存在 → ValueError("标签行不存在")（API 层 404）；
- review_status != "pending" → ConflictError("该标签已被处理")（API 层
  409——approved/rejected 终态不被覆盖，doc §6.1；重复提交/并发抢先）；
- 复制挂载/并入落库撞唯一索引 → ConflictError("该资产已挂此标签")
  （API 层 409，Ruling 3）：approve/modify/merge 的挂载行撞 ux_asset_tag
  （同资产同标签已挂）或 merge 并入撞 ux_pre_tag_catalog（同资产已有同
  tag_id 行）——commit 抛 IntegrityError 时回滚，行变更与新建定义一并
  丢弃，行保持 pending；
- open 自由候选行（tag_id 为空）直接 approve/modify → ValueError（API 层
  422——asset_tag.tag_id 非空约束的前置化：候选名无法直接挂载，须先经
  merge 赋 tag_id）；
- 非法 action / modify 无可改字段或字段空/超长/类型错 / merge 载荷缺失、
  两支混用或目标不存在 → ValueError（API 层 422）；modify 各字符串字段
  strip 后校验非空且 ≤ 200 字符，strip 值落库（v1 Review Focus 5 风格）。

**服务内 commit**：审核是用户操作，即时落库——与 persist（调用方统一
事务控制）不同；批量审核依赖"逐条独立提交"，单条失败不影响已成功条目。
载荷校验全部前置（零副作用）：任何失败都不留下行变更。

分层红线：本模块不得 import fastapi（设计文档 §9.4）。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tagplatform.db.models import (
    AssetTag,
    PreAssetTag,
    TagCatalog,
    TagChangeLog,
    TagDefine,
)


class ConflictError(Exception):
    """审核冲突：目标行已被处理（review_status 非 pending——重复提交或并发
    抢先），或落库撞唯一索引（挂载/并入的 asset×tag 唯一位已被占）。"""


_LEGAL_ACTIONS = ("approve", "reject", "modify", "merge")

# modify 动作 payload 键 → pre 行候选字段（键同名，doc §6.2：更新
# candidate_name/description 后直接走 approve 流程）
_MODIFY_FIELDS = ("candidate_name", "candidate_description")

# modify/合并新建字符串字段长度上限（strip 后按字符数计，超长 → ValueError）
_MODIFY_MAX_LEN = 200


def _clean_text(value: object, label: str) -> str:
    """合并新建字段的字符串校验（沿 modify 的 v1 风格）：必须 str、strip 后
    非空且不超上限，返回 strip 值。"""
    if not isinstance(value, str):
        raise ValueError(f"{label}必须为字符串")
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{label}不能为空")
    if len(stripped) > _MODIFY_MAX_LEN:
        raise ValueError(f"{label}超长（上限 {_MODIFY_MAX_LEN} 字符）")
    return stripped


def _validate_merge_payload(
    session: Session, payload: dict
) -> tuple[str | None, tuple[str, str, str] | None]:
    """merge 两支载荷校验（前置零副作用）→ (merge_to_tag_id, 新建规格)。

    两支互斥：merge_to_tag_id（并入既有，目标必须存在于 tag_define）或
    new_tag_name + new_dir_id（合入新建，目录必须存在且同目录下无同名
    标签——ux_tag_define_dir_name 冲突前置拦截并提示改走并入既有）。
    返回值二者恰一非空。
    """
    merge_to = payload.get("merge_to_tag_id")
    new_name = payload.get("new_tag_name")
    if merge_to and new_name:
        raise ValueError(
            "merge 动作只能提供 merge_to_tag_id（并入既有）或"
            " new_tag_name+new_dir_id（合入新建）其一")
    if merge_to:
        if session.get(TagDefine, merge_to) is None:
            raise ValueError(f"目标标签不存在：{merge_to}")
        return merge_to, None
    if new_name:
        name = _clean_text(new_name, "新标签名")
        dir_id = payload.get("new_dir_id")
        if not dir_id:
            raise ValueError("合入新建必须提供 new_dir_id")
        if session.get(TagCatalog, dir_id) is None:
            raise ValueError(f"目标目录不存在：{dir_id}")
        # Review Focus 3：同目录同名会撞 ux_tag_define_dir_name——前置拦截，
        # 提示唯一可用路径（并入既有），不让 IntegrityError 变裸 500
        duplicate = (
            session.query(TagDefine)
            .filter(TagDefine.dir_id == dir_id, TagDefine.tag_name == name)
            .first()
        )
        if duplicate is not None:
            raise ValueError(
                f"目录下已存在同名标签：{name}，请改用并入既有"
                "（merge_to_tag_id）")
        description = ""
        if payload.get("new_description") is not None:
            description = _clean_text(payload["new_description"], "新标签描述")
        return None, (name, dir_id, description)
    raise ValueError(
        "merge 动作必须提供 merge_to_tag_id（并入既有）或"
        " new_tag_name+new_dir_id（合入新建）")


def _create_merged_define(session: Session, spec: tuple[str, str, str],
                          operator: str, now: datetime) -> TagDefine:
    """合入新建：建 TagDefine（val_fixed，doc §3.3）并写 tag_change_log
    （action='merge'，doc §6.2 指标口径），flush 确保定义行先于挂载与
    pre 行更新落库。返回新定义行。"""
    name, dir_id, description = spec
    define = TagDefine(
        tag_id=f"tag_{uuid.uuid4().hex[:8]}",
        tag_name=name,
        tag_type="val_fixed",
        description=description,
        dir_id=dir_id,
        status="active",
        created_at=now,
        updated_at=now,
    )
    session.add(define)
    session.add(TagChangeLog(
        tag_id=define.tag_id,
        action="merge",
        before_json=None,
        after_json=json.dumps(
            {"kind": "tag", "tag_id": define.tag_id, "tag_name": name,
             "dir_id": dir_id, "description": description},
            ensure_ascii=False),
        operator=operator,
        created_at=now,
    ))
    session.flush()  # 跨表插入分步 flush：定义行先落库再挂载（外键时序）
    return define


def apply_review(session: Session, pre_tag_id: int, action: str,
                 reviewer: str, payload: dict | None) -> PreAssetTag:
    """对 pre_asset_tag 行执行审核动作（doc §6.2 映射表），服务内提交后返回
    该行。reviewer_id/review_comment/review_status 落行即完整审核事实（行
    即日志，无 ReviewRecord）。

    前置不满足/载荷非法按模块 docstring 抛错（ValueError/ConflictError，
    零副作用——校验全部先于任何行变更）。
    """
    row = session.get(PreAssetTag, pre_tag_id)
    if row is None:
        raise ValueError("标签行不存在")
    if row.review_status != "pending":
        # 终态不被覆盖（doc §6.1）——重复提交/并发抢先
        raise ConflictError("该标签已被处理")
    if action not in _LEGAL_ACTIONS:
        raise ValueError(
            f"未知 action: {action!r}（合法取值 approve|reject|modify|merge）")

    # ---- 载荷校验（全部前置：失败时零副作用）----
    payload = payload or {}
    comment = payload.get("comment")

    new_fields: dict[str, str] = {}
    if action == "modify":
        for key in _MODIFY_FIELDS:
            value = payload.get(key)
            if value is None:
                continue
            # 字符串字段逐字段校验（Review Focus 5 风格）：strip 后非空且
            # 不超上限，strip 值直接落库（顺带消灭纯空白名）
            if not isinstance(value, str):
                raise ValueError(f"修改字段必须为字符串：{key}")
            stripped = value.strip()
            if not stripped:
                raise ValueError("修改字段不能为空")
            if len(stripped) > _MODIFY_MAX_LEN:
                raise ValueError(
                    f"修改字段超长：{key}（上限 {_MODIFY_MAX_LEN} 字符）")
            new_fields[key] = stripped
        if not new_fields:
            raise ValueError(
                "modify 动作至少提供一个可修改字段"
                "（candidate_name/candidate_description）")

    merge_to: str | None = None
    new_define: tuple[str, str, str] | None = None
    if action == "merge":
        merge_to, new_define = _validate_merge_payload(session, payload)

    # approve 流程前置：行须已关联标签定义（open 自由候选须先合并入库——
    # asset_tag.tag_id 非空，候选名无法直接挂载）
    define: TagDefine | None = None
    if action in ("approve", "modify"):
        if row.tag_id is None:
            raise ValueError(
                "该候选行未关联标签定义（open 自由候选须先合并入库 merge）")
        define = session.get(TagDefine, row.tag_id)
        if define is None:
            raise ValueError(f"标签定义不存在：{row.tag_id}")

    # ---- 执行动作（前置已全部通过）----
    # 整段执行包在 IntegrityError 转译内：执行期任何 flush（合入新建的
    # 定义落库、行更新 autoflush）或提交撞唯一索引（ux_asset_tag：同资产
    # 同标签已挂载 / ux_pre_tag_catalog：同资产已有同 tag_id 行 /
    # ux_tag_define_dir_name 并发窗口）→ 回滚丢弃本次全部变更（行保持
    # pending、新建定义与变更日志一并丢弃），按冲突语义上抛而非裸 500
    # （API 层映射 409）
    now = datetime.now(timezone.utc)
    try:
        if action == "reject":
            row.review_status = "rejected"
            row.reviewer_id = reviewer
            row.review_comment = comment
            row.updated_at = now
        else:  # approve / modify / merge 共用 approve 流程（approved + 复制挂载）
            if action == "modify":
                for field, value in new_fields.items():
                    setattr(row, field, value)
            elif action == "merge":
                if new_define is not None:
                    # 合入新建：建定义 + change_log，pre 行赋
                    # tag_id/candidate_dir_id（doc §6.4 赋值时序）
                    define = _create_merged_define(
                        session, new_define, reviewer, now)
                    row.candidate_dir_id = define.dir_id
                    row.tag_id = define.tag_id
                else:
                    # 并入既有：pre 行赋 tag_id，candidate_name 原文保留
                    # （可溯）——定义行先取（get 触发 autoflush，须在行变更
                    # 前完成，避免撞索引时异常点偏离提交期）
                    define = session.get(TagDefine, merge_to)
                    row.tag_id = merge_to
            row.review_status = "approved"
            row.reviewer_id = reviewer
            row.review_comment = comment
            row.updated_at = now
            session.add(AssetTag(
                asset_id=row.asset_id,
                target_type=row.target_type,
                target_id=row.target_id,
                tag_id=row.tag_id,
                # 冗余拷贝取 define 现值（doc §3.6/§6.3——candidate_* 不参与）
                tag_name=define.tag_name,
                created_at=now,
                updated_at=now,
            ))
        session.commit()  # 审核是用户操作，服务内即时落库
    except IntegrityError:
        session.rollback()
        raise ConflictError("该资产已挂此标签")
    return row


# ---------- tag_name 冗余拷贝刷新（doc §6.3 契约助手） ----------

def refresh_mounted_tag_names(session: Session, tag_id: str) -> int:
    """把某标签定义的最新 tag_name 同步到其全部 asset_tag 挂载行，返回更新行数。

    doc §6.3：asset_tag.tag_name 是 tag_define.tag_name 的冗余拷贝
    （val_fixed），定义侧改名/合并后调用本助手消除两处漂移。调用契约：
    调用方先改 TagDefine.tag_name（并自行写 tag_change_log 留证、提交
    事务）——本助手只做挂载侧批量 UPDATE，flush 不 commit，与调用方的
    行变更同事务原子落库；本期无 rename 端点（助手 + 测试即交付，Task 8
    rename 流程消费）。定义不存在 → ValueError（API 层映射 422）。
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
