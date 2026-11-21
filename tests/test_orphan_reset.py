"""孤儿任务启动复位测试（Task 13）。

覆盖契约（brief 三用例：两种任务类型各一个 + 非 running 行不受影响）：
- test_asset_build_orphan_reset：AssetBuildTask running 孤儿 → failed
  （error="进程重启中断"、finished_at 落库——措辞对齐 generation
  reset_orphan_running 现文案）；
- test_tag_import_orphan_reset：TagImportTask 在途行（processing，其
  "running" 等位状态）→ failed——该表无 error 列，说明写 result_summary
  JSON（与 TagImportExecutor 原失败收尾同构）；
- test_orphan_reset_ignores_non_running：非在途行（queued/succeeded/
  completed/revoked）不受影响，复位计数只含在途孤儿（幂等语义同
  reset_orphan_running：无在途可清时返回 0）。

reset_orphan_tasks(engine) 收引擎：内部自建独立 session 一次事务提交
（与 lifespan 启动期调用形态一致）；测试经 session.get_bind() 取当前
内存库引擎，复位后 expire_all 强制重读（跨 session 写入不进本 session
身份映射）。
"""

import json

from tagplatform.capabilities.storage.models import (
    AssetBuildTask,
    MediaAsset,
    TagImportTask,
)
from tagplatform.tasks import reset_orphan_tasks


def _seed_asset(session, asset_id: str = "movie_orphan_1") -> None:
    """播种媒体资产父行（asset_build_task.asset_id 外键指向 media_asset）。"""
    if session.get(MediaAsset, asset_id) is None:
        session.add(MediaAsset(asset_id=asset_id, asset_type="movie",
                               title="孤儿复位测试", created_at=None))
        session.flush()


def _add_build_task(session, task_id: str, status: str) -> AssetBuildTask:
    task = AssetBuildTask(task_id=task_id, asset_id="movie_orphan_1",
                          movie_id="movie_orphan_1", status=status,
                          total_scenes=3, done_scenes=1)
    session.add(task)
    return task


def _add_import_task(session, task_id: str, status: str) -> TagImportTask:
    task = TagImportTask(task_id=task_id, file_name=f"{task_id}.xlsx",
                         status=status, created_at=None)
    session.add(task)
    return task


def test_asset_build_orphan_reset(session):
    """AssetBuildTask running 孤儿复位 failed：error 含重启说明 + finished_at 落库。"""
    _seed_asset(session)
    _add_build_task(session, "build_orphan", "running")
    session.commit()

    engine = session.get_bind()
    assert reset_orphan_tasks(engine) == 1

    session.expire_all()
    row = session.get(AssetBuildTask, "build_orphan")
    assert row.status == "failed"
    assert row.error == "进程重启中断"
    assert row.finished_at is not None


def test_tag_import_orphan_reset(session):
    """TagImportTask processing 孤儿复位 failed：说明写 result_summary JSON。"""
    _add_import_task(session, "import_orphan", "processing")
    session.commit()

    engine = session.get_bind()
    assert reset_orphan_tasks(engine) == 1

    session.expire_all()
    row = session.get(TagImportTask, "import_orphan")
    assert row.status == "failed"
    summary = json.loads(row.result_summary)
    assert "进程重启中断" in summary["error"]


def test_orphan_reset_ignores_non_running(session):
    """非在途行不受影响：queued/succeeded/completed/revoked 原样保留，
    复位计数只含 running（build）与 processing（import）孤儿。"""
    _seed_asset(session)
    _add_build_task(session, "build_queued", "queued")
    _add_build_task(session, "build_succeeded", "succeeded")
    _add_build_task(session, "build_running", "running")
    _add_import_task(session, "import_completed", "completed")
    _add_import_task(session, "import_revoked", "revoked")
    _add_import_task(session, "import_processing", "processing")
    session.commit()

    engine = session.get_bind()
    assert reset_orphan_tasks(engine) == 2

    session.expire_all()
    statuses = {t.task_id: t.status for t in session.query(AssetBuildTask).all()}
    statuses.update({t.task_id: t.status
                     for t in session.query(TagImportTask).all()})
    assert statuses == {
        "build_queued": "queued",
        "build_succeeded": "succeeded",
        "build_running": "failed",
        "import_completed": "completed",
        "import_revoked": "revoked",
        "import_processing": "failed",
    }
    # 幂等：在途孤儿清零后再调返回 0（与 reset_orphan_running 同语义）
    assert reset_orphan_tasks(engine) == 0
