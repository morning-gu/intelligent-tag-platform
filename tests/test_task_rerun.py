"""重跑替换语义（文档 §6.1）：同 origin 的 pending 行先清后写。

- open 重跑清掉旧 open 的待审候选，approved 行与 closed 行不受影响；
- closed 重跑同理，只清自己 origin 的待审行；
- 已定稿（approved/rejected）行任何模式重跑都不复活。
"""
from datetime import datetime, timezone

from conftest import MOCK_EMBEDDER, MOCK_LLM

from tagplatform.db.models import PreAssetTag
from tagplatform.tasks import run_generation_task
from test_task_runner import _make_task, _seed_catalog


def _tag(asset_id, task_id, name, status="pending", origin="open"):
    now = datetime.now(timezone.utc)
    return PreAssetTag(asset_id=asset_id, target_type="content",
                       target_id=asset_id, task_id=task_id,
                       candidate_name=name, origin=origin, confidence=0.8,
                       review_status=status, evidence_json="[]",
                       created_at=now, updated_at=now)


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
        _tag(asset_id, "task_closed", "闭环候选丁", origin="closed"),
    ])
    session.commit()

    task = _make_task(session, mode="open", task_id="task_new_open")
    run_generation_task(task.task_id, session, MOCK_LLM, MOCK_EMBEDDER)
    session.refresh(task)
    assert task.status == "succeeded", task.error

    rows = {row.candidate_name: row for row in session.query(PreAssetTag).all()}
    assert "旧候选甲" not in rows and "旧候选乙" not in rows
    assert "已定稿丙" in rows and rows["已定稿丙"].review_status == "approved"
    assert "闭环候选丁" in rows
    # 本次三候选已落库
    assert {"多线叙事", "情感张力", "视听风格"} <= set(rows)


def test_closed_rerun_replaces_only_its_own_pending(session):
    """closed 重跑同样先清后写，但共享的 open 待审行保留。"""
    old = _make_task(session, mode="closed", task_id="task_closed_old")
    old.status = "succeeded"
    session.commit()
    session.add_all([
        _tag("movie_001", "task_closed_old", "旧闭环候选", origin="closed"),
        _tag("movie_001", "task_closed_old", "开放候选戊", origin="open"),
    ])
    session.commit()

    task = _make_task(session, mode="closed", task_id="task_closed_new",
                      policy_json='{"min_confidence": 0.5}')
    _seed_catalog(session)
    run_generation_task(task.task_id, session, MOCK_LLM, MOCK_EMBEDDER)
    session.refresh(task)
    assert task.status == "succeeded", task.error
    names = {row.candidate_name for row in session.query(PreAssetTag).all()}
    assert "旧闭环候选" not in names
    assert "开放候选戊" in names
