"""标签结果入库：ProducedTag → `pre_asset_tag` 的「删旧插新 + 自动判定」。

对应设计文档 §6.1（重跑与幂等）与 v2 的"重复不入审"口径：

- 任务写结果前，先删除该资产**同 origin** 的全部 `pending` 行（closed 重跑
  不清 open 的待审行，反之亦然）；
- **命中库内标签**（`ProducedTag.origin == "catalog"`）的行不再进人工队列：
  直接落 `review_status="approved"`、`reviewer_id="system"`。完全同名记
  "自动通过"，相似并入（带 `alias_name`）记"相似并入，原名记为别名候选"；
- **库外新词**（`origin == "generated"`）落 `pending`，带候选目录
  （`candidate_dir_id`）与挂载点（`target_type/target_id`），等人工审核；
- 已定稿行（approved / rejected）不动——重跑不得复活人工已定或已驳回的标签；
- 去重边界是两个部分唯一索引：`ux_pre_tag_catalog`（asset×target×tag_id）
  与 `ux_pre_tag_candidate`（asset×target×candidate_name）。

分层红线：本模块不 import fastapi（设计文档 §9.4）。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from sqlalchemy import delete
from sqlalchemy.orm import Session

from tagplatform.db.models import AssetTag, PreAssetTag
from tagplatform.generation.base import ProducedTag

# 人工已定稿状态：重跑一律跳过（不覆盖、不复活）
FINALIZED_STATUSES = ("approved", "rejected")
# 系统自动判定的审核人标识
SYSTEM_REVIEWER = "system"


@dataclass
class PersistSummary:
    """入库结果：清掉的旧待审行 / 新插入行 / 系统自动通过 / 跳过已定稿。"""

    cleared: int
    inserted: int
    skipped_finalized: int
    auto_approved: int = 0
    # 自动通过同时写进 asset_tag 的挂载行数（人工审核那条路在 review 服务里写）
    mounted: int = 0


def _mount_asset_tag(session: Session, *, asset_id: str, target_type: str,
                     target_id: str, tag_id: str, tag_name: str,
                     now: datetime) -> bool:
    """自动通过的行同步写正式挂载（幂等：同键已挂则跳过）。

    `asset_tag` 才是"资产有哪些标签"的权威口径；审核人工那条路在
    review 服务里写，系统自动通过（同名命中/相似并入）也必须写，否则
    资产详情看不到这些标签。
    """
    exists = (
        session.query(AssetTag.id)
        .filter(
            AssetTag.asset_id == asset_id,
            AssetTag.target_type == target_type,
            AssetTag.target_id == target_id,
            AssetTag.tag_id == tag_id,
        )
        .first()
    )
    if exists is not None:
        return False
    session.add(AssetTag(
        asset_id=asset_id,
        target_type=target_type,
        target_id=target_id,
        tag_id=tag_id,
        tag_name=tag_name,
        created_at=now,
        updated_at=now,
    ))
    return True


def _evidence_json(tag: ProducedTag) -> str:
    """Evidence 列表 → JSON 数组字符串（中文不转义）。"""
    return json.dumps([asdict(e) for e in tag.evidence], ensure_ascii=False)


def _existing(session: Session, asset_id: str, target_type: str, target_id: str,
              field, value) -> PreAssetTag | None:
    """按 (asset, target, tag_id|candidate_name) 查既有行（跨 origin）。"""
    return (
        session.query(PreAssetTag)
        .filter(
            PreAssetTag.asset_id == asset_id,
            PreAssetTag.target_type == target_type,
            PreAssetTag.target_id == target_id,
            field == value,
        )
        .one_or_none()
    )


def _auto_comment(tag: ProducedTag) -> str:
    """系统自动通过的留痕：同名与相似并入分开写，便于事后审计。"""
    if tag.alias_name:
        return f"系统自动并入库内标签「{tag.tag_name}」，原名「{tag.alias_name}」记为别名候选"
    return f"系统自动通过：与库内标签「{tag.tag_name}」同名"


def persist_tags(
    session: Session,
    asset_id: str,
    task_id: str,
    tags: list[ProducedTag],
    origin: str,
) -> PersistSummary:
    """把 Producer 产出写进 `pre_asset_tag`（不 commit，调用方统一提交）。

    origin：任务模式（closed / open），即行的产出来源，落库后不可变。
    """
    if origin not in ("closed", "open"):
        raise ValueError(f"未知 origin: {origin!r}（合法取值 closed|open）")

    now = datetime.now(timezone.utc)
    cleared = session.execute(
        delete(PreAssetTag).where(
            PreAssetTag.asset_id == asset_id,
            PreAssetTag.origin == origin,
            PreAssetTag.review_status == "pending",
        )
    ).rowcount or 0

    summary = PersistSummary(cleared=cleared, inserted=0, skipped_finalized=0)
    for tag in tags:
        target_type = tag.target_type or "content"
        target_id = tag.target_id or asset_id
        if tag.origin == "catalog":
            if not tag.tag_id:
                raise ValueError(
                    "catalog 标签缺少 tag_id（ProducedTag 契约要求必填）")
            field, value = PreAssetTag.tag_id, tag.tag_id
            values: dict[str, object] = {
                "tag_id": tag.tag_id,
                "candidate_name": None,
                "candidate_description": None,
                "candidate_alias": tag.alias_name,
            }
            status = "approved"
            comment = _auto_comment(tag)
        elif tag.origin == "generated":
            if not tag.dir_id:
                raise ValueError(
                    f"新词缺少候选目录: {tag.tag_name!r}（生成侧必须给出 dir_id）")
            field, value = PreAssetTag.candidate_name, tag.tag_name
            values = {
                "tag_id": None,
                "candidate_name": tag.tag_name,
                "candidate_description": tag.candidate_description,
                "candidate_dir_id": tag.dir_id,
                "candidate_alias": None,
            }
            status = "pending"
            comment = None
        else:
            raise ValueError(
                f"未知产出类型: {tag.origin!r}（合法取值 catalog|generated）")

        existing = _existing(session, asset_id, target_type, target_id,
                             field, value)
        if existing is not None:
            # pending 行已被上一步清掉，这里命中的只可能是已定稿行或其他
            # origin 的待审行：都不覆盖（不复活、不抢行）
            summary.skipped_finalized += 1
            continue

        session.add(PreAssetTag(
            asset_id=asset_id,
            target_type=target_type,
            target_id=target_id,
            origin=origin,
            relevance_score=tag.relevance_score,
            confidence=tag.confidence,
            evidence_json=_evidence_json(tag),
            review_status=status,
            reviewer_id=SYSTEM_REVIEWER if status == "approved" else None,
            review_comment=comment,
            task_id=task_id,
            created_at=now,
            updated_at=now,
            **values,
        ))
        summary.inserted += 1
        if status == "approved":
            summary.auto_approved += 1
            if _mount_asset_tag(
                session, asset_id=asset_id, target_type=target_type,
                target_id=target_id, tag_id=str(values["tag_id"]),
                tag_name=tag.tag_name, now=now,
            ):
                summary.mounted += 1

    return summary
