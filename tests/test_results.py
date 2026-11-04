"""标签结果入库服务测试（Task 7）。

覆盖契约（brief 四规则）：
- 插入 pending：新 (asset_id, 标签) → 新行 review_status="pending"；
- approved 保留：重跑命中 approved 行 → 跳过不动（confidence/task_id
  保持原值）；
- pending 覆盖：重跑命中 pending 行 → 覆盖更新（confidence/task_id
  刷新为新任务的值）；
- generated 候选 upsert：按 (asset_id, candidate_name) 同规则；
- 已定跳过两 origin 统一 {approved, rejected, merged}（阶段 3 B3 拍板：
  rejected 与 approved/merged 同属人工已定，重跑覆盖会自动复活审核
  驳回的候选；计划 catalog 侧漏写 merged 系计划文本缺陷——catalog 与
  generated 侧各有 approved/merged/rejected 跳过锁定测试）。

附加锁定：evidence_json 为 Evidence asdict 序列化、插入行时间戳显式
非空、未知 origin 与 catalog 缺 tag_id 报 ValueError（零副作用）。

注：brief 测试片段的 asset_id/task_id 为库中不存在的裸值（a1/t1/t2），
而 create_engine_for 逐连接开启 foreign_keys=ON（test_models.py 同教
训）——用 autouse fixture 先补齐外键父行，测试体保持计划原文逐字不动。
"""

import json

import pytest

from tagplatform.db.models import AssetTag, GenerationTask, MediaAsset, TagCatalog
from tagplatform.generation import Evidence, ProducedTag
from tagplatform.results import PersistSummary, persist_tags


def _ptag(tag_id=None, name="悬疑", origin="catalog", conf=0.9):
    return ProducedTag(origin=origin, tag_id=tag_id, tag_name=name, candidate_type=None,
                       candidate_description=None, relevance_score=0.9, confidence=conf,
                       evidence=[Evidence("scene_001", 1000, 2000, "证据")])


@pytest.fixture(autouse=True)
def _seed_fk_parents(session):
    """补齐 brief 片段裸值（a1/t1/t2/tag_1）的外键父行，避免 IntegrityError
    掩盖 upsert 语义断言（asset_tag.asset_id → media_asset、
    asset_tag.task_id → generation_task、asset_tag.tag_id → tag_catalog）。"""
    session.add(MediaAsset(asset_id="a1", asset_type="movie", title="测试影片"))
    session.add(TagCatalog(tag_id="tag_1", tag_name="悬疑", tag_type="genre",
                           description="以悬念和谜题为核心"))
    session.add_all([
        GenerationTask(task_id="t1", mode="closed", status="succeeded"),
        GenerationTask(task_id="t2", mode="closed", status="succeeded"),
    ])
    session.commit()


def test_persist_insert_pending(session):
    s = persist_tags(session, "a1", "t1", [_ptag("tag_1")], "r1")
    assert s == PersistSummary(1, 0, 0) and session.query(AssetTag).one().review_status == "pending"


def test_persist_rerun_overwrites_pending_keeps_approved(session):
    persist_tags(session, "a1", "t1", [_ptag("tag_1", conf=0.95)], "r1")
    session.query(AssetTag).one().review_status = "approved"; session.commit()
    s = persist_tags(session, "a1", "t2", [_ptag("tag_1", conf=0.5)], "r2")
    row = session.query(AssetTag).one()
    assert s.skipped_approved == 1 and row.confidence == 0.95 and row.task_id == "t1"  # 原值未动


def test_persist_catalog_merged_skipped(session):
    """catalog 行 merged 后重跑亦跳过（controller ruling：已定跳过集两
    origin 统一为 approved/merged；与 approved 保留测试同构，一行状态差）。"""
    persist_tags(session, "a1", "t1", [_ptag("tag_1", conf=0.95)], "r1")
    session.query(AssetTag).one().review_status = "merged"
    session.commit()
    s = persist_tags(session, "a1", "t2", [_ptag("tag_1", conf=0.5)], "r2")
    row = session.query(AssetTag).one()
    assert s.skipped_approved == 1 and row.confidence == 0.95 and row.task_id == "t1"  # 原值未动


def test_persist_catalog_rejected_skipped(session):
    """catalog 行 rejected 后重跑亦跳过（阶段 3 B3 拍板：rejected 入人工
    已定集，与 approved 保留测试同构，一行状态差——重跑覆盖会自动复活
    审核驳回的候选）。"""
    persist_tags(session, "a1", "t1", [_ptag("tag_1", conf=0.95)], "r1")
    session.query(AssetTag).one().review_status = "rejected"
    session.commit()
    s = persist_tags(session, "a1", "t2", [_ptag("tag_1", conf=0.5)], "r2")
    row = session.query(AssetTag).one()
    assert s.skipped_approved == 1 and row.confidence == 0.95 and row.task_id == "t1"  # 原值未动


def test_persist_pending_overwritten(session):
    persist_tags(session, "a1", "t1", [_ptag("tag_1", conf=0.95)], "r1")
    s = persist_tags(session, "a1", "t2", [_ptag("tag_1", conf=0.5)], "r2")
    row = session.query(AssetTag).one()
    assert s.updated == 1 and row.confidence == 0.5 and row.task_id == "t2"


def test_persist_generated_candidate_upsert(session):
    g = _ptag(None, "多线叙事", origin="generated")
    persist_tags(session, "a1", "t1", [g], "r1")
    assert session.query(AssetTag).one().candidate_name == "多线叙事"
    # 重跑覆盖 pending 候选：按 candidate_name 命中同一行，不重复插行
    s = persist_tags(session, "a1", "t2",
                     [_ptag(None, "多线叙事", origin="generated", conf=0.5)], "r2")
    row = session.query(AssetTag).one()
    assert s.updated == 1 and session.query(AssetTag).count() == 1
    assert row.confidence == 0.5 and row.task_id == "t2"
    # approved 候选跳过（原值未动）
    row.review_status = "approved"
    session.commit()
    s = persist_tags(session, "a1", "t2",
                     [_ptag(None, "多线叙事", origin="generated", conf=0.1)], "r2")
    assert s.skipped_approved == 1
    row = session.query(AssetTag).one()
    assert row.confidence == 0.5 and row.task_id == "t2"


def test_persist_generated_merged_skipped(session):
    """generated 行 merged 后重跑跳过（brief 原文规则，与 ruling 统一集合一致）。"""
    persist_tags(session, "a1", "t1",
                 [_ptag(None, "多线叙事", origin="generated", conf=0.8)], "r1")
    session.query(AssetTag).one().review_status = "merged"
    session.commit()
    s = persist_tags(session, "a1", "t2",
                     [_ptag(None, "多线叙事", origin="generated", conf=0.1)], "r2")
    row = session.query(AssetTag).one()
    assert s.skipped_approved == 1 and row.confidence == 0.8 and row.task_id == "t1"


def test_persist_generated_rejected_skipped(session):
    """generated 行 rejected 后重跑跳过（阶段 3 B3 拍板：已定跳过集扩为
    approved/rejected/merged，两 origin 统一——驳回的候选不得被重跑复活）。"""
    persist_tags(session, "a1", "t1",
                 [_ptag(None, "多线叙事", origin="generated", conf=0.8)], "r1")
    session.query(AssetTag).one().review_status = "rejected"
    session.commit()
    s = persist_tags(session, "a1", "t2",
                     [_ptag(None, "多线叙事", origin="generated", conf=0.1)], "r2")
    row = session.query(AssetTag).one()
    assert s.skipped_approved == 1 and row.confidence == 0.8 and row.task_id == "t1"


def test_persist_writes_evidence_json_and_timestamps(session):
    """evidence_json 为 Evidence asdict 列表序列化；时间戳显式赋值非空。"""
    persist_tags(session, "a1", "t1", [_ptag("tag_1")], "r1")
    row = session.query(AssetTag).one()
    assert json.loads(row.evidence_json) == [
        {"scene_id": "scene_001", "start_ms": 1000, "end_ms": 2000, "description": "证据"}
    ]
    assert row.created_at is not None and row.updated_at is not None


def test_persist_catalog_without_tag_id_raises(session):
    # origin=catalog 但 tag_id 为 None：按 (asset_id, tag_id=None) 查询会
    # 误命中 generated 行（tag_id 为 NULL），契约要求 catalog tag_id 必填
    with pytest.raises(ValueError):
        persist_tags(session, "a1", "t1", [_ptag(None)], "r1")


def test_persist_unknown_origin_raises(session):
    # 非法 origin 拒绝入库且零副作用（查询前即校验）
    with pytest.raises(ValueError):
        persist_tags(session, "a1", "t1", [_ptag("tag_1", origin="alien")], "r1")


def _tag_with_level(level: str) -> ProducedTag:
    return ProducedTag(origin="generated", tag_id=None, tag_name="主观镜头",
                       candidate_type="shot", candidate_description="第一人称视角",
                       relevance_score=None, confidence=0.8,
                       evidence=[Evidence("scene_001", 1000, 2000, "证据")],
                       level=level)


def test_persist_level_insert_and_overwrite(session):
    """层级随产出落库：插入写 scene；同候选重跑可刷新为 film。"""
    persist_tags(session, "a1", "t1", [_tag_with_level("scene")], "r1")
    assert session.query(AssetTag).one().level == "scene"

    persist_tags(session, "a1", "t1", [_tag_with_level("film")], "r2")
    assert session.query(AssetTag).one().level == "film"
