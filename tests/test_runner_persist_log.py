"""runner.py 入库汇总日志行的契约锁定（观测盲点修复）。

背景（排查 gen_d873e057289c“选了 10 只入库 5”与 gen_80bb0cd888d9“0 条产出”
两次扑空）：persist_tags 的 skipped_finalized 原先完全静默——命中资产既有
行（任何 origin、任何状态）被跳过时不落库、不打日志，前端按 task_id 查任务
详情只能看到 inserted 的行，“产出 N 条 ≠ 入库 N 条”的差值无处可查。

runner.py 现在在 persist 后必打一行 INFO：

    任务 {task_id} 入库汇总：产出 N 条 → 新插 X 条（自动通过 Y、挂载 Z）、
    撞既有行跳过 S 条

本文件用 caplog 锁定该行为：
- 成功路径必打（即使产出 0 条也打）；closed 全量入审（doc §3.3）下
  自动通过/挂载恒为 0，产出条条入审；
- 跳过数与库内既有行对得上（第一轮产出全部终审为 approved 再重跑，
  选中条条撞定稿行：skip>0、insert=0）；
- 已定稿行仍不复活（回归“不覆盖”语义在日志口径下同样成立）。
"""

import logging
import re

import pytest
from conftest import MOCK_EMBEDDER, MOCK_LLM
from sqlalchemy.orm import Session

from tagplatform.db.models import PreAssetTag
from tagplatform.tasks import run_generation_task

from tests.test_task_runner import _make_task, _seed_catalog, _utcnow

_PATTERN = re.compile(
    r"任务 (?P<task_id>\S+) 入库汇总：产出 (?P<produced>\d+) 条 → 新插 "
    r"(?P<inserted>\d+) 条（自动通过 (?P<auto>\d+)、挂载 (?P<mounted>\d+)）、"
    r"撞既有行跳过 (?P<skipped>\d+) 条")


@pytest.fixture
def seeded_task(session):
    _seed_catalog(session)
    return _make_task(session, policy_json='{"min_confidence": 0.5}')


def _persist_log(caplog, task_id: str) -> re.Match | None:
    """从 caplog 里捞该任务的入库汇总行，返回解析结果。"""
    for record in caplog.records:
        if record.levelname == "INFO":
            m = _PATTERN.search(record.getMessage())
            if m and m.group("task_id") == task_id:
                return m
    return None


def test_persist_summary_log_line_present(seeded_task, session, caplog):
    """成功路径必打入库汇总行（产出/新插/自动通过/挂载/跳过 五个数齐全）。

    Mock 模式 A 选中项 applicability 恒 ≥4（mock.py _score_five_point），
    全部过门控；closed 产出入审（doc §3.3）：自动通过与挂载恒为 0。
    """
    with caplog.at_level(logging.INFO, logger="tagplatform.tasks.runner"):
        run_generation_task(seeded_task.task_id, session, MOCK_LLM, MOCK_EMBEDDER)
    m = _persist_log(caplog, seeded_task.task_id)
    assert m is not None, "入库汇总 INFO 行缺失：persist 观测盲点将复发"
    produced = int(m.group("produced"))
    inserted = int(m.group("inserted"))
    auto = int(m.group("auto"))
    mounted = int(m.group("mounted"))
    skipped = int(m.group("skipped"))
    assert produced == inserted + skipped  # 产出 = 新插 + 跳过，账要平
    assert produced >= 1  # 30 选 40% 采样，零选中概率 ≈ 2e-7
    assert auto == 0 and mounted == 0  # closed 全量入审：无系统自动通过
    assert inserted >= 1  # Mock 模式 A 产出的 catalog 行全新插入


def test_persist_summary_log_reports_skipped_on_rerun(
        session, seeded_task, caplog):
    """重跑同资产：命中已定稿行被跳过，日志的“跳过”数要如实可见。

    第一轮 closed 产出全部 pending 入审；人工终审批量置 approved（模拟
    审核通过），再把任务拨回 queued 重跑。重跑先清 pending（pending 无
    残留），Mock 按 run_id 哈希采样两轮选中集合不同，但第二轮命中既有
    approved 行按 (asset, target, tag) 不复活：skip 数 = 命中数 > 0。
    """
    run_generation_task(seeded_task.task_id, session, MOCK_LLM, MOCK_EMBEDDER)
    assert seeded_task.status == "succeeded"
    # 第一轮产出行全部终审为 approved（模拟人工审核通过 → 已定稿）
    session.query(PreAssetTag).filter(
        PreAssetTag.asset_id == seeded_task.asset_id).update(
        {"review_status": "approved", "updated_at": _utcnow()},
        synchronize_session=False)
    session.commit()
    # 第一轮落库行的 (id, created_at) 快照：定稿行不复活 = 重跑后原样保留
    before = {
        r.id: r.created_at for r in session.query(PreAssetTag).filter(
            PreAssetTag.asset_id == seeded_task.asset_id).all()
    }
    assert before  # Mock 采样 30 选，第一轮必有产出

    seeded_task.status = "queued"
    session.commit()
    with caplog.at_level(logging.INFO, logger="tagplatform.tasks.runner"):
        run_generation_task(seeded_task.task_id, session, MOCK_LLM, MOCK_EMBEDDER)
    m = _persist_log(caplog, seeded_task.task_id)
    assert m is not None
    produced = int(m.group("produced"))
    inserted = int(m.group("inserted"))
    skipped = int(m.group("skipped"))
    assert produced == inserted + skipped  # 账目守恒
    assert skipped >= 1, "重跑应至少撞一条既有 approved 行，但日志跳过数为 0"
    after = {
        r.id: r.created_at for r in session.query(PreAssetTag).filter(
            PreAssetTag.asset_id == seeded_task.asset_id).all()
    }
    assert all(after.get(i) == t for i, t in before.items())  # 不复活、不覆盖
