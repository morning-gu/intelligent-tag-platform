"""审核操作服务测试（Task 4）。

覆盖契约（brief 四动作 + 前置 + 记录写入）：
- approve/reject：review_status → approved/rejected，写一行 review_record；
- modify：payload 字段映射到 AssetTag 现有语义字段（candidate_*，generated
  行即展示字段；catalog 行按计划"改 AssetTag 上现有语义字段"最小化——
  tag_name/tag_type/description 别名同样落 candidate_*，不触碰共享的
  tag_catalog 字典），status 回 pending；
- merge：tag_id=merge_to_tag_id、candidate_* 清空、status → merged；
- 前置：行不存在 ValueError("标签行不存在")；非 pending → ConflictError
  （冲突真触发：先置非 pending 再提交，而非依赖种子默认值）；
- 载荷：非法 action / modify 无可改字段 / merge 缺或坏 merge_to_tag_id →
  ValueError；
- 记录：record_id 前缀 rev_、before/after 快照（关键字段 + 状态前后差）、
  reviewer_id/comment 落库、服务内 commit（rollback 后数据仍在）。
"""

from datetime import datetime, timezone

import json

import pytest

from tagplatform.db.models import AssetTag, MediaAsset, ReviewRecord, TagCatalog
from tagplatform.review import ConflictError, apply_review

_NOW = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _seed_parents(session):
    """补齐外键父行（asset_tag.asset_id → media_asset、asset_tag.tag_id →
    tag_catalog），test_results.py 同款播种模式。"""
    session.add(MediaAsset(asset_id="a1", asset_type="movie", title="测试影片",
                           created_at=_NOW))
    session.add(TagCatalog(tag_id="tag_1", tag_name="悬疑", tag_type="genre",
                           description="以悬念和谜题为核心"))
    session.add(TagCatalog(tag_id="tag_2", tag_name="喜剧", tag_type="genre",
                           description="以笑点为核心"))
    session.commit()


def _seed_row(session, origin="generated", status="pending",
              tag_id=None, candidate_name="多线叙事") -> AssetTag:
    """播种一行 asset_tag 并提交（拿到行 id；时间戳显式赋值）。"""
    is_generated = origin == "generated"
    row = AssetTag(
        asset_id="a1",
        tag_id=tag_id,
        candidate_name=candidate_name if is_generated else None,
        candidate_type="narrative" if is_generated else None,
        candidate_description="候选描述" if is_generated else None,
        origin=origin,
        relevance_score=0.9 if not is_generated else None,
        confidence=0.8,
        evidence_json="[]",
        review_status=status,
        task_id=None,
        created_at=_NOW,
        updated_at=_NOW,
    )
    session.add(row)
    session.commit()
    return row


def _record(session, result_id: int) -> ReviewRecord:
    return session.query(ReviewRecord).filter_by(result_id=result_id).one()


# ---------- approve / reject ----------


def test_approve_sets_status_and_writes_record(session):
    row = _seed_row(session)
    returned = apply_review(session, row.id, "approve", "alice", None)
    assert returned.review_status == "approved"
    rec = _record(session, row.id)
    assert rec.record_id.startswith("rev_")
    assert rec.action == "approve" and rec.reviewer_id == "alice"
    assert rec.comment is None and rec.created_at is not None
    before = json.loads(rec.before_json)
    after = json.loads(rec.after_json)
    assert before["review_status"] == "pending"
    assert after["review_status"] == "approved"
    assert before["candidate_name"] == "多线叙事"  # 快照含行关键字段
    assert before["id"] == row.id and after["id"] == row.id


def test_reject_sets_status_and_records_comment(session):
    row = _seed_row(session)
    returned = apply_review(session, row.id, "reject", "bob",
                            {"comment": "证据不足"})
    assert returned.review_status == "rejected"
    rec = _record(session, row.id)
    assert rec.action == "reject" and rec.comment == "证据不足"
    assert json.loads(rec.before_json)["review_status"] == "pending"
    assert json.loads(rec.after_json)["review_status"] == "rejected"


# ---------- modify ----------


def test_modify_generated_fields_and_stays_pending(session):
    """generated 行：candidate_* 三字段可改，status 回 pending（重新进队）。"""
    row = _seed_row(session)
    returned = apply_review(session, row.id, "modify", "alice", {
        "candidate_name": "多线叙事改", "candidate_type": "mood",
        "candidate_description": "新描述", "comment": "措辞修正"})
    assert returned.candidate_name == "多线叙事改"
    assert returned.candidate_type == "mood"
    assert returned.candidate_description == "新描述"
    assert returned.review_status == "pending"
    rec = _record(session, row.id)
    assert rec.action == "modify" and rec.comment == "措辞修正"
    before = json.loads(rec.before_json)
    after = json.loads(rec.after_json)
    assert before["candidate_name"] == "多线叙事"
    assert after["candidate_name"] == "多线叙事改"
    assert after["review_status"] == "pending"


def test_modify_catalog_row_maps_display_keys(session):
    """catalog 行按"改 AssetTag 上现有语义字段"最小化：tag_name/tag_type/
    description 别名映射到 candidate_*（不触碰共享 tag_catalog 字典）。"""
    row = _seed_row(session, origin="catalog", tag_id="tag_1")
    returned = apply_review(session, row.id, "modify", "alice", {
        "tag_name": "悬疑改", "tag_type": "thriller", "description": "新定义"})
    assert returned.candidate_name == "悬疑改"
    assert returned.candidate_type == "thriller"
    assert returned.candidate_description == "新定义"
    assert returned.tag_id == "tag_1"  # 目录挂载关系不动
    assert returned.review_status == "pending"
    # 共享目录字典未被触碰
    assert session.get(TagCatalog, "tag_1").tag_name == "悬疑"


def test_modify_without_mappable_field_raises(session):
    """空 modify 值（无任何可改字段）→ ValueError（API 层 422）。"""
    row = _seed_row(session)
    with pytest.raises(ValueError):
        apply_review(session, row.id, "modify", "alice", None)
    with pytest.raises(ValueError):
        apply_review(session, row.id, "modify", "alice", {})
    with pytest.raises(ValueError):
        apply_review(session, row.id, "modify", "alice", {"comment": "仅备注"})
    # 零副作用：行与记录均未动
    assert session.query(ReviewRecord).count() == 0


def test_modify_unknown_keys_only_raises(session):
    row = _seed_row(session)
    with pytest.raises(ValueError):
        apply_review(session, row.id, "modify", "alice",
                     {"irrelevant": "x", "comment": "c"})


# ---------- merge ----------


def test_merge_moves_to_catalog_tag(session):
    """merge：tag_id=merge_to、candidate_* 清空、status → merged。"""
    row = _seed_row(session)
    returned = apply_review(session, row.id, "merge", "alice",
                            {"merge_to_tag_id": "tag_2", "comment": "并入目录"})
    assert returned.tag_id == "tag_2"
    assert returned.candidate_name is None
    assert returned.candidate_type is None
    assert returned.candidate_description is None
    assert returned.review_status == "merged"
    rec = _record(session, row.id)
    assert rec.action == "merge" and rec.comment == "并入目录"
    before = json.loads(rec.before_json)
    after = json.loads(rec.after_json)
    assert before["candidate_name"] == "多线叙事" and before["tag_id"] is None
    assert after["tag_id"] == "tag_2" and after["candidate_name"] is None
    assert after["review_status"] == "merged"


def test_merge_without_merge_to_raises(session):
    row = _seed_row(session)
    with pytest.raises(ValueError):
        apply_review(session, row.id, "merge", "alice", None)
    with pytest.raises(ValueError):
        apply_review(session, row.id, "merge", "alice", {"comment": "缺目标"})
    assert session.query(ReviewRecord).count() == 0  # 零副作用


def test_merge_unknown_target_raises(session):
    row = _seed_row(session)
    with pytest.raises(ValueError):
        apply_review(session, row.id, "merge", "alice",
                     {"merge_to_tag_id": "tag_none"})
    assert session.query(ReviewRecord).count() == 0


# ---------- 前置与冲突 ----------


def test_missing_row_raises_value_error(session):
    with pytest.raises(ValueError, match="标签行不存在"):
        apply_review(session, 999, "approve", "alice", None)


def test_conflict_when_not_pending(session):
    """冲突真触发：先把行置 approved 并提交，再审核 → ConflictError。"""
    row = _seed_row(session)
    row.review_status = "approved"
    session.commit()
    with pytest.raises(ConflictError, match="该标签已被处理"):
        apply_review(session, row.id, "approve", "alice", None)
    assert session.query(ReviewRecord).count() == 0  # 冲突不写记录


def test_unknown_action_raises(session):
    row = _seed_row(session)
    with pytest.raises(ValueError):
        apply_review(session, row.id, "alien", "alice", None)
    assert session.query(ReviewRecord).count() == 0


def test_service_commits_immediately(session):
    """服务内提交：审核后 rollback，行状态与审核记录仍存活（已落库）。"""
    row = _seed_row(session)
    apply_review(session, row.id, "approve", "alice", {"comment": "ok"})
    session.rollback()  # 撤销一切未提交变更
    assert session.get(AssetTag, row.id).review_status == "approved"
    assert session.query(ReviewRecord).filter_by(result_id=row.id).count() == 1
