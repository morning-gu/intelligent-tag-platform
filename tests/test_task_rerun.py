"""重跑替换语义：open 任务再次成功时清掉同资产未审核的旧候选。"""
import json
from datetime import datetime, timezone

from conftest import MOCK_EMBEDDER, MOCK_LLM

from tagplatform.db.models import AssetTag, GenerationTask
from tagplatform.tasks import run_generation_task
from test_task_runner import _make_task


def _tag(asset_id, task_id, name, status="pending", origin="generated"):
    now = datetime.now(timezone.utc)
    return AssetTag(asset_id=asset_id, task_id=task_id, candidate_name=name,
                    origin=origin, confidence=0.8, review_status=status,
                    evidence_json="[]", created_at=now, updated_at=now)


def test_open_rerun_replaces_pending_rows(session):
    """旧 open 任务的 pending 行被清掉；approved 行与 closed 行不受影响。"""
    old = _make_task(session, mode="open", task_id="task_old_open")
    old.status = "succeeded"
    session.commit()
    closed_task = _make_task(session, mode="closed", task_id="task_closed")
    closed_task.status = "succeeded"
    session.commit()

    asset_id = "movie_001"
    session.add_all([
        _tag(asset_id, "task_old_open", "旧候选甲"),
        _tag(asset_id, "task_old_open", "旧候选乙"),
        _tag(asset_id, "task_old_open", "已定稿丙", status="approved"),
        _tag(asset_id, "task_closed", "闭环候选丁"),
    ])
    session.commit()

    task = _make_task(session, mode="open", task_id="task_new_open")
    run_generation_task(task.task_id, session, MOCK_LLM, MOCK_EMBEDDER)
    session.refresh(task)
    assert task.status == "succeeded", task.error

    rows = {row.candidate_name: row for row in session.query(AssetTag).all()}
    assert "旧候选甲" not in rows and "旧候选乙" not in rows
    assert "已定稿丙" in rows and rows["已定稿丙"].review_status == "approved"
    assert "闭环候选丁" in rows
    # 本次三候选已落库
    assert {"多线叙事", "情感张力", "视听风格"} <= set(rows)


def test_closed_task_does_not_clear_pending(session):
    """closed 模式不声明 replaces_pending：旧 pending 行保留（不动既有语义）。"""
    old = _make_task(session, mode="closed", task_id="task_closed_old")
    old.status = "succeeded"
    session.commit()
    session.add(_tag("movie_001", "task_closed_old", "旧闭环候选"))
    session.commit()

    task = _make_task(session, mode="closed", task_id="task_closed_new",
                      policy_json='{"min_confidence": 0.5}')
    from test_task_runner import _seed_catalog
    _seed_catalog(session)
    run_generation_task(task.task_id, session, MOCK_LLM, MOCK_EMBEDDER)
    session.refresh(task)
    assert task.status == "succeeded", task.error
    names = {row.candidate_name for row in session.query(AssetTag).all()}
    assert "旧闭环候选" in names
