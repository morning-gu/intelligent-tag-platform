"""标签结果入库服务测试（v2 pre_asset_tag 重写，Task 6）。

覆盖契约（brief + doc §6.1 + 三条语义红线）：
- 首次写入：新 (asset, 标识) → pending 行，summary (inserted=N,
  deleted_pending=0, skipped_finalized=0)；origin/target_type/target_id
  按 v2 契约（origin 取 ProducedTag.origin，target_id=asset_id 本身，
  candidate_type 不入库——v2 无此列，字段仅为 mtg 审计保留）；
- 重跑替换（旧 pending）：runner 先 `_clear_pending` 删旧 → persist 插新，
  分数/证据/task_id 为新值——删 pending 的责任在 runner，persist 只做
  INSERT OR IGNORE；
- persist 单独重跑（未清理）：pending 行占位撞部分唯一索引 → 旧行原样
  不被覆盖（deleted_pending 计数）——锁死"persist 不先查后插、不更新"；
- 终态保留：approved/rejected 行 → skipped_finalized 且逐字段不动
  （驳回的候选不得被重跑复活，doc §6.1）；
- 跨 origin 隔离：closed 插入撞 open 的 pending 行（同 tag_id 归一命中）
  → 跳过且 open 行原样（persist 不删任何 pending 行）；
- open 自由候选：candidate_name=tag_name、tag_id 空、candidate_description
  入库；closed 缺 tag_id / 未知 origin（含 v1 残留词 catalog/generated）
  → ValueError 零副作用。

实现注意：asset_id/task_id/tag_id 均带外键——autouse fixture 先补齐父行
（media_asset / tag_catalog+tag_define / tag_generation_task），目录行先
flush 再插定义（跨表插入分步 flush，否则 FK 先于父行校验）。
"""

import json
from datetime import datetime, timezone

import pytest

from tagplatform.db.models import (
    MediaAsset,
    PreAssetTag,
    TagCatalog,
    TagDefine,
    TagGenerationTask,
)
from tagplatform.generation import Evidence, ProducedTag
from tagplatform.results import FINALIZED_STATUSES, PersistSummary, persist_tags
from tagplatform.tasks.runner import _clear_pending


def _ptag(tag_id=None, name="悬疑", origin="closed", conf=0.9, rel=0.9,
          evidence=None, candidate_description=None):
    """ProducedTag 构造：candidate_type 恒非空——v2 不入库，仅 mtg 审计。"""
    return ProducedTag(
        origin=origin, tag_id=tag_id, tag_name=name,
        candidate_type="genre" if tag_id else "叙事",
        candidate_description=candidate_description,
        relevance_score=rel, confidence=conf,
        evidence=evidence if evidence is not None
        else [Evidence("scene_001", 1000, 2000, "证据")],
    )


@pytest.fixture(autouse=True)
def _seed_fk_parents(session):
    """补齐裸值（a1/t1/t2/tag_1/tag_2/dir_1）的外键父行（v2 双表：tag_define
    经 dir_id 挂 tag_catalog，目录行先 flush 再插定义）。"""
    session.add(MediaAsset(asset_id="a1", asset_type="movie", title="测试影片"))
    session.flush()
    session.add(TagCatalog(dir_id="dir_1", dir_name="剧情目录",
                           dir_type="dir_static", description="叙事类目"))
    session.flush()
    session.add_all([
        TagDefine(tag_id="tag_1", tag_name="悬疑", tag_type="val_fixed",
                  description="以悬念和谜题为核心", dir_id="dir_1"),
        TagDefine(tag_id="tag_2", tag_name="喜剧", tag_type="val_fixed",
                  description="以笑点为核心", dir_id="dir_1"),
    ])
    session.add_all([
        TagGenerationTask(task_id="t1", asset_id="a1", mode="closed",
                          status="succeeded"),
        TagGenerationTask(task_id="t2", asset_id="a1", mode="closed",
                          status="succeeded"),
    ])
    session.commit()


def _row(session, tag_id=None, candidate_name=None) -> PreAssetTag:
    q = session.query(PreAssetTag).filter(PreAssetTag.asset_id == "a1")
    if tag_id is not None:
        q = q.filter(PreAssetTag.tag_id == tag_id)
    if candidate_name is not None:
        q = q.filter(PreAssetTag.candidate_name == candidate_name)
    return q.one()


_SNAPSHOT_ATTRS = ("id", "asset_id", "target_type", "target_id", "tag_id",
                   "candidate_name", "candidate_dir_id", "candidate_description",
                   "origin", "relevance_score", "confidence", "evidence_json",
                   "task_id", "review_status", "reviewer_id", "review_comment",
                   "created_at", "updated_at")


def _snapshot(row) -> dict:
    return {attr: getattr(row, attr) for attr in _SNAPSHOT_ATTRS}


# ---- 首次写入 ----


def test_persist_insert_pending(session):
    s = persist_tags(session, "a1", "t1",
                     [_ptag("tag_1"), _ptag("tag_2", name="喜剧")], "r1")
    assert s == PersistSummary(inserted=2, deleted_pending=0, skipped_finalized=0)
    row = _row(session, tag_id="tag_1")
    assert row.review_status == "pending" and row.task_id == "t1"
    assert row.origin == "closed" and row.target_type == "content"
    assert row.target_id == "a1"  # content 级：target_id = asset_id 本身
    assert row.candidate_name is None and row.candidate_dir_id is None
    assert row.created_at is not None and row.updated_at is not None


def test_pre_table_has_no_candidate_type_column(session):
    """v2 列契约：candidate_type 不入库（doc §3.5——生成端分类留在 mtg 审计
    文件），persist 接受带 candidate_type 的 ProducedTag 而不报错。"""
    assert not hasattr(PreAssetTag, "candidate_type")
    persist_tags(session, "a1", "t1", [_ptag("tag_1")], "r1")
    assert session.query(PreAssetTag).count() == 1


def test_persist_writes_evidence_json(session):
    persist_tags(session, "a1", "t1", [_ptag("tag_1")], "r1")
    row = _row(session, tag_id="tag_1")
    assert json.loads(row.evidence_json) == [
        {"scene_id": "scene_001", "start_ms": 1000, "end_ms": 2000,
         "description": "证据"}
    ]


# ---- 重跑替换（删旧在 runner 的 _clear_pending，persist 只插新）----


def test_persist_rerun_replaces_pending(session):
    """同标签二轮（旧 pending）：runner 先清同 origin pending（删旧）→
    persist 插新，分数/证据/task_id 为新值，不叠行。"""
    persist_tags(session, "a1", "t1", [_ptag("tag_1", conf=0.95, rel=0.8)], "r1")
    assert _clear_pending(session, "a1", "closed") == 1
    new_evidence = [Evidence("scene_002", 3000, 4000, "新一轮证据")]
    s = persist_tags(session, "a1", "t2",
                     [_ptag("tag_1", conf=0.5, rel=0.7, evidence=new_evidence)],
                     "r2")
    assert s == PersistSummary(inserted=1, deleted_pending=0, skipped_finalized=0)
    row = _row(session, tag_id="tag_1")
    assert row.confidence == 0.5 and row.relevance_score == 0.7
    assert row.task_id == "t2"
    assert json.loads(row.evidence_json) == [
        {"scene_id": "scene_002", "start_ms": 3000, "end_ms": 4000,
         "description": "新一轮证据"}
    ]
    assert session.query(PreAssetTag).count() == 1  # 删旧插新：不叠行


def test_persist_pending_conflict_skipped_not_overwritten(session):
    """persist 只 INSERT OR IGNORE：未清旧 pending 直接重跑 → 旧行原样
    （不被覆盖更新），撞索引跳过计入 deleted_pending（删除是 runner 的
    同 origin 职责，本服务不删行）。"""
    persist_tags(session, "a1", "t1", [_ptag("tag_1", conf=0.95)], "r1")
    s = persist_tags(session, "a1", "t2", [_ptag("tag_1", conf=0.5)], "r2")
    row = _row(session, tag_id="tag_1")
    assert s.inserted == 0 and s.deleted_pending == 1 and s.skipped_finalized == 0
    assert row.confidence == 0.95 and row.task_id == "t1"  # 原值未动


def test_persist_cross_origin_pending_preserved(session):
    """closed 插入撞 open 的 pending 行（同 tag_id 的归一命中）：跳过且
    open 行原样——persist 不删任何 pending，closed 不清 open 待审行。"""
    now = datetime.now(timezone.utc)
    session.add(PreAssetTag(
        asset_id="a1", target_type="content", target_id="a1",
        tag_id="tag_1", candidate_name="悬疑", origin="open",
        confidence=0.8, review_status="pending", task_id="t1",
        created_at=now, updated_at=now))
    session.commit()
    s = persist_tags(session, "a1", "t2", [_ptag("tag_1")], "r1")
    assert s.deleted_pending == 1 and s.inserted == 0
    row = _row(session, tag_id="tag_1")
    assert row.origin == "open" and row.review_status == "pending"
    assert session.query(PreAssetTag).count() == 1  # open 行未被删/覆盖


# ---- 终态保留（approved/rejected 永不被覆盖）----


def test_persist_approved_skipped(session):
    persist_tags(session, "a1", "t1", [_ptag("tag_1", conf=0.95)], "r1")
    _row(session, tag_id="tag_1").review_status = "approved"
    session.commit()
    before = _snapshot(_row(session, tag_id="tag_1"))
    s = persist_tags(session, "a1", "t2", [_ptag("tag_1", conf=0.5)], "r2")
    session.expire_all()
    row = _row(session, tag_id="tag_1")
    assert s.skipped_finalized == 1 and s.inserted == 0
    assert _snapshot(row) == before  # 终态行逐字段不动


def test_persist_rejected_skipped(session):
    """rejected 亦终态（doc §6.1：驳回即终审，翻案走人工改库不靠重跑）。"""
    persist_tags(session, "a1", "t1", [_ptag("tag_1", conf=0.95)], "r1")
    row = _row(session, tag_id="tag_1")
    row.review_status = "rejected"
    row.reviewer_id = "bob"
    row.review_comment = "驳回"
    session.commit()
    before = _snapshot(row)
    s = persist_tags(session, "a1", "t2", [_ptag("tag_1", conf=0.5)], "r2")
    session.expire_all()
    row = _row(session, tag_id="tag_1")
    assert s.skipped_finalized == 1 and s.inserted == 0
    assert _snapshot(row) == before


def test_persist_open_approved_skipped(session):
    """open 候选行 approved 后重跑同样跳过（FINALIZED_STATUSES 两 origin
    统一，按行状态判定与 origin 无关）。"""
    persist_tags(session, "a1", "t1",
                 [_ptag(None, "多线叙事", origin="open", conf=0.9)], "r1")
    _row(session, candidate_name="多线叙事").review_status = "approved"
    session.commit()
    s = persist_tags(session, "a1", "t2",
                     [_ptag(None, "多线叙事", origin="open", conf=0.1)], "r2")
    assert s.skipped_finalized == 1 and s.inserted == 0
    row = _row(session, candidate_name="多线叙事")
    assert row.confidence == 0.9 and row.task_id == "t1"  # 原值未动


def test_finalized_statuses_vocabulary():
    """v2 人工已定集 = {approved, rejected}（merge 退役：并入既有/合入新建
    的终态都是 approved，doc §6.2）。"""
    assert FINALIZED_STATUSES == ("approved", "rejected")


# ---- open 自由候选 ----


def test_persist_open_free_candidate(session):
    """open 自由候选：tag_id 空、candidate_name=tag_name、描述入库、
    candidate_dir_id 生成时不赋值（doc §6.4）。"""
    g = _ptag(None, "多线叙事", origin="open",
              candidate_description="多线并进的叙事结构")
    persist_tags(session, "a1", "t1", [g], "r1")
    row = _row(session, candidate_name="多线叙事")
    assert row.tag_id is None and row.origin == "open"
    assert row.candidate_description == "多线并进的叙事结构"
    assert row.candidate_dir_id is None
    assert row.review_status == "pending" and row.task_id == "t1"


# ---- 契约校验（零副作用）----


def test_persist_closed_without_tag_id_raises(session):
    # origin=closed 但 tag_id 为空：closed 意味着命中目录内定义，tag_id 必填
    with pytest.raises(ValueError):
        persist_tags(session, "a1", "t1", [_ptag(None)], "r1")
    assert session.query(PreAssetTag).count() == 0  # 零副作用


def test_persist_unknown_origin_raises(session):
    # v1 残留词 catalog/generated 均不在 v2 词表（closed|open）
    with pytest.raises(ValueError):
        persist_tags(session, "a1", "t1", [_ptag("tag_1", origin="catalog")], "r1")
    with pytest.raises(ValueError):
        persist_tags(session, "a1", "t1",
                     [_ptag(None, "自由候选", origin="generated")], "r1")
    assert session.query(PreAssetTag).count() == 0  # 零副作用
