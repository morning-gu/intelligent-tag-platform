"""重跑替换语义测试（doc §6.1，Review Focus 1/2，v2 pre_asset_tag）。

- Review Focus 1（test_rerun_scoped_by_origin）：同资产先 open 后 closed
  两个任务，closed 重跑只清 origin=closed 的 pending——open 的待审候选
  原样保留（open 的 approved 行同理不动）；
- Review Focus 2（test_rerun_preserves_finalized）：重跑不动终态——
  approved/rejected 的 pre 行与 asset_tag 挂载逐字段不变，pending 行被
  新证据替换（删旧插新）。

旧轮 pending/终态行按 v1 同款手法直接播种（模拟历史任务产出）；新任务
真跑（MockLLM + 30 枚候选 + min_confidence=0.5 消采样 flake）。删除用
synchronize_session=False 执行，会话内对象不同步——断言一律先
expire_all 再重查，保证读到库内真值。
"""

import json
from datetime import datetime, timezone

from conftest import MOCK_EMBEDDER, MOCK_LLM

from tagplatform.db.models import AssetTag, PreAssetTag
from tagplatform.tasks import run_generation_task
from test_task_runner import _make_task, _seed_catalog

_NOW = datetime.now(timezone.utc)

_SNAPSHOT_ATTRS = ("id", "asset_id", "target_type", "target_id", "tag_id",
                   "candidate_name", "candidate_dir_id", "candidate_description",
                   "origin", "relevance_score", "confidence", "evidence_json",
                   "task_id", "review_status", "reviewer_id", "review_comment",
                   "created_at", "updated_at")


def _snapshot(row) -> dict:
    return {attr: getattr(row, attr) for attr in _SNAPSHOT_ATTRS}


def _pre(asset_id, task_id, *, origin, tag_id=None, candidate_name=None,
         status="pending", conf=0.8, evidence="[]", reviewer=None,
         comment=None) -> PreAssetTag:
    return PreAssetTag(
        asset_id=asset_id, target_type="content", target_id=asset_id,
        tag_id=tag_id, candidate_name=candidate_name, origin=origin,
        relevance_score=0.7, confidence=conf, evidence_json=evidence,
        task_id=task_id, review_status=status, reviewer_id=reviewer,
        review_comment=comment, created_at=_NOW, updated_at=_NOW,
    )


def test_rerun_scoped_by_origin(session):
    """Review Focus 1：closed 重跑不清 open 的待审行（删除按 origin 限界）。"""
    _seed_catalog(session)
    _make_task(session, mode="open", task_id="task_old_open")
    _make_task(session, mode="closed", task_id="task_old_closed")
    session.add_all([
        _pre("movie_001", "task_old_open", origin="open",
             candidate_name="开放候选甲"),
        _pre("movie_001", "task_old_open", origin="open",
             candidate_name="开放候选乙"),
        _pre("movie_001", "task_old_open", origin="open",
             candidate_name="已定稿丙", status="approved",
             reviewer="alice", comment="通过"),
        _pre("movie_001", "task_old_closed", origin="closed", tag_id="tag_g_00"),
    ])
    session.commit()

    task = _make_task(session, mode="closed", task_id="task_new_closed",
                      policy_json='{"min_confidence": 0.5}')
    done = run_generation_task(task.task_id, session, MOCK_LLM, MOCK_EMBEDDER)
    assert done.status == "succeeded", done.error

    session.expire_all()
    rows = session.query(PreAssetTag).all()
    # open 的 pending 候选原样保留（逐字段不动）
    for name in ("开放候选甲", "开放候选乙"):
        row = next(r for r in rows
                   if r.origin == "open" and r.candidate_name == name)
        assert row.review_status == "pending"
        assert row.task_id == "task_old_open" and row.evidence_json == "[]"
    # open 的 approved 行不动
    row = next(r for r in rows
               if r.origin == "open" and r.candidate_name == "已定稿丙")
    assert row.review_status == "approved" and row.reviewer_id == "alice"
    # closed 的旧 pending 行已删（同 origin 替换；closed 不触 open 行）
    assert not any(r.origin == "closed" and r.task_id == "task_old_closed"
                   for r in rows)
    # 新 closed 行已落库
    new_rows = [r for r in rows if r.task_id == "task_new_closed"]
    assert len(new_rows) >= 1
    assert all(r.origin == "closed" and r.review_status == "pending"
               for r in new_rows)


def test_rerun_preserves_finalized(session):
    """Review Focus 2：重跑不动终态——approved/rejected 的 pre 行与
    asset_tag 挂载逐字段不变，pending 行被新证据替换。"""
    _seed_catalog(session)
    _make_task(session, mode="closed", task_id="task_old")
    old_evidence = json.dumps(
        [{"scene_id": "scene_001", "start_ms": 1, "end_ms": 2,
          "description": "旧证据"}], ensure_ascii=False)
    session.add_all([
        _pre("movie_001", "task_old", origin="closed", tag_id="tag_g_00",
             status="approved", conf=0.9, evidence=old_evidence,
             reviewer="alice", comment="通过"),
        _pre("movie_001", "task_old", origin="closed", tag_id="tag_g_01",
             status="rejected", conf=0.4, reviewer="bob", comment="驳回"),
        _pre("movie_001", "task_old", origin="closed", tag_id="tag_g_02",
             conf=0.6, evidence=old_evidence),
    ])
    session.add(AssetTag(
        asset_id="movie_001", target_type="content", target_id="movie_001",
        tag_id="tag_g_00", tag_name="类型标签0",
        created_at=_NOW, updated_at=_NOW))
    session.commit()
    session.expire_all()

    rows = session.query(PreAssetTag).order_by(PreAssetTag.id).all()
    approved = next(r for r in rows if r.review_status == "approved")
    rejected = next(r for r in rows if r.review_status == "rejected")
    mount = session.query(AssetTag).one()
    approved_before, rejected_before = _snapshot(approved), _snapshot(rejected)
    mount_before = {a: getattr(mount, a) for a in
                    ("id", "asset_id", "target_type", "target_id", "tag_id",
                     "tag_name", "created_at", "updated_at")}

    task = _make_task(session, mode="closed", task_id="task_new",
                      policy_json='{"min_confidence": 0.5}')
    done = run_generation_task(task.task_id, session, MOCK_LLM, MOCK_EMBEDDER)
    assert done.status == "succeeded", done.error

    session.expire_all()
    rows = session.query(PreAssetTag).all()
    # 终态行逐字段不变（即便本轮重产出同标签，INSERT OR IGNORE 也不触碰）
    assert _snapshot(next(r for r in rows if r.id == approved.id)) == approved_before
    assert _snapshot(next(r for r in rows if r.id == rejected.id)) == rejected_before
    mount = session.query(AssetTag).one()
    assert {a: getattr(mount, a) for a in mount_before} == mount_before
    # pending 行被替换（删旧插新）：旧任务名下不再有任何 closed pending 行；
    # 若本轮重产出同标签，新行挂新任务、证据为新值（SQLite rowid 复用被删
    # 末行的 id——行身份按 tag_id+task_id 断言，不用 id）
    assert not any(r.task_id == "task_old" and r.review_status == "pending"
                   for r in rows)
    reproduced = [r for r in rows if r.tag_id == "tag_g_02"]
    for r in reproduced:
        assert r.task_id == "task_new" and "旧证据" not in r.evidence_json
