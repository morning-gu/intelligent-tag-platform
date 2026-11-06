"""标签结果入库服务测试：ProducedTag → pre_asset_tag（文档 §6.1 语义）。

覆盖契约：
- 插入 pending：新 (asset, target, 标签) → 一行 pending，target 固定 content；
- 重跑替换：同 origin 的 pending 行先被清掉再插新（不叠行、值刷新）；
- 已定稿保留：approved / rejected 行重跑跳过（不覆盖、不复活）；
- origin 隔离：closed 重跑不清 open 的待审行，反之亦然；
- 唯一索引兜底：同批重复产出只落一行（第二个被跳过）；
- 契约校验：catalog 缺 tag_id / 未知 origin 直接 ValueError（零副作用）。

注：create_engine_for 逐连接开启 foreign_keys=ON，用 fixture 先补齐
media_asset / tag_catalog / tag_generation_task 父行。
"""

import json

import pytest

from tagplatform.db.models import (
    MediaAsset,
    PreAssetTag,
    TagCatalog,
    TagGenerationTask,
)
from tagplatform.generation import Evidence, ProducedTag
from tagplatform.results import PersistSummary, persist_tags


def _ptag(tag_id=None, name="悬疑", origin="catalog", conf=0.9):
    return ProducedTag(origin=origin, tag_id=tag_id, tag_name=name,
                       candidate_type=None, candidate_description=None,
                       relevance_score=0.9, confidence=conf,
                       evidence=[Evidence("scene_001", 1000, 2000, "证据")])


@pytest.fixture(autouse=True)
def _seed_fk_parents(session):
    session.add(MediaAsset(asset_id="a1", asset_type="movie", title="测试影片"))
    session.add(TagCatalog(tag_id="tag_1", tag_name="悬疑", tag_type="genre",
                           description="以悬念和谜题为核心"))
    session.add_all([
        TagGenerationTask(task_id="t1", asset_id="a1", mode="closed",
                          status="succeeded"),
        TagGenerationTask(task_id="t2", asset_id="a1", mode="closed",
                          status="succeeded"),
    ])
    session.commit()


def test_persist_insert_pending(session):
    s = persist_tags(session, "a1", "t1", [_ptag("tag_1")], "closed")
    row = session.query(PreAssetTag).one()
    assert s == PersistSummary(0, 1, 0)
    assert row.review_status == "pending"
    assert row.target_type == "content" and row.target_id == "a1"
    assert row.origin == "closed" and row.tag_id == "tag_1"
    assert row.candidate_name is None


def test_persist_rerun_replaces_pending(session):
    persist_tags(session, "a1", "t1", [_ptag("tag_1", conf=0.95)], "closed")
    s = persist_tags(session, "a1", "t2", [_ptag("tag_1", conf=0.5)], "closed")
    row = session.query(PreAssetTag).one()
    assert s.cleared == 1 and s.inserted == 1
    assert row.confidence == 0.5 and row.task_id == "t2"


def test_persist_keeps_approved(session):
    persist_tags(session, "a1", "t1", [_ptag("tag_1", conf=0.95)], "closed")
    session.query(PreAssetTag).one().review_status = "approved"
    session.commit()
    s = persist_tags(session, "a1", "t2", [_ptag("tag_1", conf=0.5)], "closed")
    row = session.query(PreAssetTag).one()
    assert s.skipped_finalized == 1
    assert row.confidence == 0.95 and row.task_id == "t1"


def test_persist_keeps_rejected(session):
    persist_tags(session, "a1", "t1", [_ptag("tag_1", conf=0.95)], "closed")
    session.query(PreAssetTag).one().review_status = "rejected"
    session.commit()
    s = persist_tags(session, "a1", "t2", [_ptag("tag_1", conf=0.5)], "closed")
    row = session.query(PreAssetTag).one()
    assert s.skipped_finalized == 1
    assert row.confidence == 0.95 and row.task_id == "t1"


def test_persist_origin_isolation(session):
    """closed 的待审行不被 open 重跑清掉（两模式各管各的未审行）。"""
    persist_tags(session, "a1", "t1", [_ptag("tag_1")], "closed")
    persist_tags(session, "a1", "t2",
                 [_ptag(None, "多线叙事", origin="generated")], "open")
    assert session.query(PreAssetTag).count() == 2
    s = persist_tags(session, "a1", "t2",
                     [_ptag(None, "多线叙事", origin="generated")], "open")
    assert s.cleared == 1 and session.query(PreAssetTag).count() == 2


def test_persist_generated_candidate(session):
    g = _ptag(None, "多线叙事", origin="generated")
    persist_tags(session, "a1", "t1", [g], "open")
    row = session.query(PreAssetTag).one()
    assert row.candidate_name == "多线叙事" and row.tag_id is None
    assert row.origin == "open"


def test_persist_duplicate_in_batch_lands_once(session):
    """同一批里的重复产出只落一行（库内唯一索引 + 逐条存在性检查）。"""
    s = persist_tags(session, "a1", "t1",
                     [_ptag("tag_1"), _ptag("tag_1", conf=0.4)], "closed")
    assert s.inserted == 1 and s.skipped_finalized == 1
    assert session.query(PreAssetTag).count() == 1


def test_persist_writes_evidence_json_and_timestamps(session):
    persist_tags(session, "a1", "t1", [_ptag("tag_1")], "closed")
    row = session.query(PreAssetTag).one()
    assert json.loads(row.evidence_json) == [
        {"scene_id": "scene_001", "start_ms": 1000, "end_ms": 2000,
         "description": "证据"}
    ]
    assert row.created_at is not None and row.updated_at is not None


def test_persist_catalog_without_tag_id_raises(session):
    with pytest.raises(ValueError):
        persist_tags(session, "a1", "t1", [_ptag(None)], "closed")


def test_persist_unknown_origin_raises(session):
    with pytest.raises(ValueError):
        persist_tags(session, "a1", "t1", [_ptag("tag_1")], "alien")
