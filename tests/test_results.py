"""标签结果入库服务测试：ProducedTag → pre_asset_tag（v2 语义）。

覆盖契约：
- **命中库内**（origin=catalog）→ 直接 approved + reviewer_id="system"，
  不进入人工队列；同名写"自动通过"，带 alias_name 写"相似并入"；
- **库外新词**（origin=generated）→ pending，必须带 dir_id（候选目录）与
  挂载点（content → asset_id；scene → scene_id；character → character_id）；
- 重跑替换：同 origin 的 pending 行先被清掉再插新（不叠行、值刷新）；
- 已定稿保留：approved / rejected 行重跑跳过（不覆盖、不复活）；
- origin 隔离：closed 重跑不清 open 的待审行，反之亦然；
- 唯一索引兜底：同批重复产出只落一行（第二个被跳过）；
- 契约校验：catalog 缺 tag_id / generated 缺 dir_id / 未知 origin → ValueError。
"""

import json

import pytest

from tagplatform.db.models import MediaAsset, PreAssetTag, TagGenerationTask
from tagplatform.generation import Evidence, ProducedTag
from tagplatform.results import PersistSummary, persist_tags


def _ptag(tag_id=None, name="悬疑", origin="catalog", conf=0.9, *,
          dir_id=None, target_type="content", target_id=None, alias=None):
    return ProducedTag(
        origin=origin, tag_id=tag_id, tag_name=name,
        candidate_type=None, candidate_description="候选描述",
        relevance_score=0.9, confidence=conf,
        evidence=[Evidence("scene_001", 1000, 2000, "证据")],
        dir_id=dir_id, target_type=target_type, target_id=target_id,
        alias_name=alias,
    )


@pytest.fixture(autouse=True)
def _seed_fk_parents(session):
    session.add(MediaAsset(asset_id="a1", asset_type="movie", title="测试影片"))
    session.add_all([
        TagGenerationTask(task_id="t1", asset_id="a1", mode="closed",
                          status="succeeded"),
        TagGenerationTask(task_id="t2", asset_id="a1", mode="closed",
                          status="succeeded"),
    ])
    session.commit()


def test_catalog_hit_is_auto_approved(session):
    s = persist_tags(session, "a1", "t1", [_ptag("tag_action", "动作")], "closed")
    row = session.query(PreAssetTag).one()
    assert s == PersistSummary(cleared=0, inserted=1, skipped_finalized=0,
                               auto_approved=1)
    assert row.review_status == "approved"
    assert row.reviewer_id == "system"
    assert "同名" in (row.review_comment or "")
    assert row.target_type == "content" and row.target_id == "a1"
    assert row.origin == "closed" and row.tag_id == "tag_action"
    assert row.candidate_name is None and row.candidate_alias is None


def test_catalog_similar_records_alias(session):
    s = persist_tags(session, "a1", "t1",
                     [_ptag("tag_revenge", "复仇", alias="报仇雪恨")], "closed")
    row = session.query(PreAssetTag).one()
    assert s.auto_approved == 1
    assert row.candidate_alias == "报仇雪恨"
    assert "别名候选" in (row.review_comment or "")


def test_scene_scope_mounts_on_scene(session):
    persist_tags(session, "a1", "t1",
                 [_ptag("tag_twist", "剧情反转", target_type="scene",
                        target_id="scene_001")], "closed")
    row = session.query(PreAssetTag).one()
    assert (row.target_type, row.target_id) == ("scene", "scene_001")


def test_generated_candidate_lands_pending_with_dir(session):
    s = persist_tags(session, "a1", "t1",
                     [_ptag(None, "多线叙事", origin="generated",
                            dir_id="dir_theme")], "open")
    row = session.query(PreAssetTag).one()
    assert s.inserted == 1 and s.auto_approved == 0
    assert row.review_status == "pending"
    assert row.candidate_name == "多线叙事" and row.tag_id is None
    assert row.candidate_dir_id == "dir_theme"
    assert row.reviewer_id is None
    assert row.origin == "open"


def test_generated_without_dir_raises(session):
    with pytest.raises(ValueError):
        persist_tags(session, "a1", "t1",
                     [_ptag(None, "多线叙事", origin="generated")], "open")


def test_rerun_replaces_pending_generated(session):
    persist_tags(session, "a1", "t1",
                 [_ptag(None, "多线叙事", origin="generated", dir_id="dir_theme",
                        conf=0.95)], "open")
    s = persist_tags(session, "a1", "t2",
                     [_ptag(None, "多线叙事", origin="generated", dir_id="dir_theme",
                            conf=0.5)], "open")
    row = session.query(PreAssetTag).one()
    assert s.cleared == 1 and s.inserted == 1
    assert row.confidence == 0.5 and row.task_id == "t2"


def test_rerun_keeps_auto_approved(session):
    """系统自动通过的行是定稿行：重跑不覆盖（避免置信度来回跳）。"""
    persist_tags(session, "a1", "t1", [_ptag("tag_action", "动作", conf=0.95)],
                 "closed")
    s = persist_tags(session, "a1", "t2", [_ptag("tag_action", "动作", conf=0.5)],
                     "closed")
    row = session.query(PreAssetTag).one()
    assert s.skipped_finalized == 1
    assert row.confidence == 0.95 and row.task_id == "t1"


def test_rerun_keeps_rejected(session):
    persist_tags(session, "a1", "t1",
                 [_ptag(None, "多线叙事", origin="generated", dir_id="dir_theme")],
                 "open")
    session.query(PreAssetTag).one().review_status = "rejected"
    session.commit()
    s = persist_tags(session, "a1", "t2",
                     [_ptag(None, "多线叙事", origin="generated",
                            dir_id="dir_theme", conf=0.5)], "open")
    row = session.query(PreAssetTag).one()
    assert s.skipped_finalized == 1
    assert row.review_status == "rejected"


def test_origin_isolation(session):
    """closed 的待审行不被 open 重跑清掉（两模式各管各的未审行）。"""
    persist_tags(session, "a1", "t1",
                 [_ptag(None, "闭环新词", origin="generated", dir_id="dir_theme")],
                 "closed")
    persist_tags(session, "a1", "t2",
                 [_ptag(None, "多线叙事", origin="generated", dir_id="dir_theme")],
                 "open")
    assert session.query(PreAssetTag).count() == 2
    s = persist_tags(session, "a1", "t2",
                     [_ptag(None, "多线叙事", origin="generated",
                            dir_id="dir_theme")], "open")
    assert s.cleared == 1 and session.query(PreAssetTag).count() == 2


def test_duplicate_in_batch_lands_once(session):
    """同一批里的重复产出只落一行（库内唯一索引 + 逐条存在性检查）。"""
    s = persist_tags(session, "a1", "t1",
                     [_ptag("tag_action", "动作"),
                      _ptag("tag_action", "动作", conf=0.4)], "closed")
    assert s.inserted == 1 and s.skipped_finalized == 1
    assert session.query(PreAssetTag).count() == 1


def test_persist_writes_evidence_json_and_timestamps(session):
    persist_tags(session, "a1", "t1", [_ptag("tag_action", "动作")], "closed")
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
        persist_tags(session, "a1", "t1", [_ptag("tag_action")], "alien")
