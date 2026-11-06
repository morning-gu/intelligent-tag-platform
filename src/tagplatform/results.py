"""标签结果入库：ProducedTag → `pre_asset_tag` 的「删旧插新 + 去重」语义。

对应设计文档 §6.1（重跑与幂等）：

- 任务写结果前，先删除该资产**同 origin** 的全部 `pending` 行（closed 重跑
  不清 open 的待审行，反之亦然）；
- 再逐条插入本次产出；已定稿行（approved / rejected）不动——重跑不得复活
  人工已定或已驳回的标签；
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

from tagplatform.db.models import PreAssetTag
from tagplatform.generation.base import ProducedTag

# 人工已定稿状态：重跑一律跳过（不覆盖、不复活）
FINALIZED_STATUSES = ("approved", "rejected")


@dataclass
class PersistSummary:
    """入库结果：清掉的旧待审行 / 新插入行 / 因已定稿而跳过的行。"""

    cleared: int
    inserted: int
    skipped_finalized: int


def _evidence_json(tag: ProducedTag) -> str:
    """Evidence 列表 → JSON 数组字符串（中文不转义）。"""
    return json.dumps([asdict(e) for e in tag.evidence], ensure_ascii=False)


def _existing(session: Session, asset_id: str, field, value) -> PreAssetTag | None:
    """按 (asset, target=content, tag_id|candidate_name) 查既有行。"""
    return (
        session.query(PreAssetTag)
        .filter(
            PreAssetTag.asset_id == asset_id,
            PreAssetTag.target_type == "content",
            PreAssetTag.target_id == asset_id,
            field == value,
        )
        .one_or_none()
    )


def persist_tags(
    session: Session,
    asset_id: str,
    task_id: str,
    tags: list[ProducedTag],
    origin: str,
) -> PersistSummary:
    """把 Producer 产出写进 `pre_asset_tag`（不 commit，调用方统一提交）。

    origin：任务模式（closed / open），即行的产出来源，落库后不可变。

    逐条规则：
    - 命中库内标签（ProducedTag.origin="catalog"）→ 写 `tag_id`，候选字段置空；
    - 库外新词（ProducedTag.origin="generated"）→ 写 `candidate_name` /
      `candidate_description`，`tag_id` 留空，等审核合入时补；
    - 同一资产同一实体已有该标签的已定稿行 → 跳过；
    - 同一批产出内部不重复插入（前者已插入后查得到）。
    """
    if origin not in ("closed", "open"):
        raise ValueError(f"未知 origin: {origin!r}（合法取值 closed|open）")

    now = datetime.now(timezone.utc)
    cleared = session.execute(
        delete(PreAssetTag).where(
            PreAssetTag.asset_id == asset_id,
            PreAssetTag.target_type == "content",
            PreAssetTag.origin == origin,
            PreAssetTag.review_status == "pending",
        )
    ).rowcount or 0

    summary = PersistSummary(cleared=cleared, inserted=0, skipped_finalized=0)
    for tag in tags:
        if tag.origin == "catalog":
            if not tag.tag_id:
                raise ValueError(
                    "catalog 标签缺少 tag_id（ProducedTag 契约要求必填）")
            field, value = PreAssetTag.tag_id, tag.tag_id
            values: dict[str, object] = {
                "tag_id": tag.tag_id,
                "candidate_name": None,
                "candidate_description": None,
            }
        elif tag.origin == "generated":
            field, value = PreAssetTag.candidate_name, tag.tag_name
            values = {
                "tag_id": None,
                "candidate_name": tag.tag_name,
                "candidate_description": tag.candidate_description,
            }
        else:
            raise ValueError(
                f"未知产出类型: {tag.origin!r}（合法取值 catalog|generated）")

        if _existing(session, asset_id, field, value) is not None:
            # pending 行已被上一步清掉，这里命中的只可能是已定稿行
            summary.skipped_finalized += 1
            continue

        session.add(PreAssetTag(
            asset_id=asset_id,
            target_type="content",   # 本期只启用 content 级挂载
            target_id=asset_id,
            origin=origin,
            relevance_score=tag.relevance_score,
            confidence=tag.confidence,
            evidence_json=_evidence_json(tag),
            review_status="pending",
            task_id=task_id,
            created_at=now,
            updated_at=now,
            **values,
        ))
        summary.inserted += 1

    return summary
