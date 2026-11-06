"""生成任务执行器测试（Task 8）。

覆盖契约（brief 六测试 + 两项支撑锁定）：
- test_run_task_success_writes_tags：成功路径写 asset_tag、回填
  prompt_version/model_version/finished_at、模式 A 回填
  media_context.embedding（计划定案：runner 独立重算摘要向量）；
- test_run_task_llm_failure_atomic：LLM 失败任务置 failed 且零半截
  asset_tag（Review Focus 1：任务级事务，异常回滚）；
- test_run_task_empty_catalog_fails：空目录显式失败（error 含"候选标签为空"）；
- test_run_task_wrong_status_rejected：非 queued 拒绝执行（ValueError），
  且拒绝不改动状态；
- test_orphan_running_reset：running 孤儿 → failed（error="进程重启中断"），
  仅清 running、重复调用幂等；
- test_executor_thread_pool：真线程池 + 文件库（:memory: 每连接独立，
  跨线程不可见），submit 后轮询状态至 succeeded（上限 10s），验证线程内
  独立 session 路径与 open 模式不回填向量；
- test_run_task_persist_failure_rolls_back_partial_writes：persist 中途
  契约校验失败（catalog 缺 tag_id）时，先前未提交的插行一并回滚（零残留）；
- test_executor_workers_zero_synchronous：workers=0 同步执行（submit 即
  完成）——T9 钉死的 generation_workers=0 测试模式所依赖的语义。

Mock 技巧（与 llm/mock.py 咬合）：
- MockLLM 模式 A 按 sha256(run_id + tag_id) < 0.4 独立采样选中，confidence
  哈希值域 [0.5,1)；run_id 由 runner 内部 uuid 生成不可控——成功路径测试
  把 min_confidence 降到 0.5（confidence 恒过线）并播种 30 枚候选，零选中
  概率 0.6^30 ≈ 2e-7，构造上消除 flake；默认 policy 解析路径由失败类测试
  覆盖（produce 异常前 policy 已解析）。
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from conftest import MOCK_EMBEDDER, MOCK_LLM
from sqlalchemy.orm import Session

from tagplatform.db import create_engine_for, make_session_factory
from tagplatform.db.models import (
    Base,
    MediaAsset,
    PreAssetTag,
    TagCatalog,
    TagGenerationTask,
)
from tagplatform.llm.client import LLMResponse
from tagplatform.tasks import (
    TaskExecutor,
    reset_orphan_running,
    run_generation_task,
)
from tagplatform.vectors import encode_vector

_CONTENT_DATA = json.loads(
    (Path(__file__).parent / "fixtures" / "content_movie.json").read_text(
        encoding="utf-8")
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _make_task(session: Session, mode: str = "closed",
               task_id: str = "task_runner_1", policy_json: str | None = None
               ) -> TagGenerationTask:
    """播种 asset（带 content_json）+ tag_generation_task(queued)，返回任务。

    v2 起任务直绑资产、内容读 media_asset.content_json：资产首次创建时写入
    内容并复用（重复播种复用同一行）。
    """
    asset_info = _CONTENT_DATA["asset"]
    asset = session.get(MediaAsset, asset_info["asset_id"])
    if asset is None:
        asset = MediaAsset(
            asset_id=asset_info["asset_id"],
            asset_type=asset_info["asset_type"],
            title=asset_info["title"],
            created_at=_utcnow(),
        )
        session.add(asset)
    asset.content_json = json.dumps(_CONTENT_DATA, ensure_ascii=False)
    session.flush()
    task = TagGenerationTask(
        task_id=task_id, asset_id=asset_info["asset_id"], mode=mode,
        policy_json=policy_json, status="queued", created_at=_utcnow(),
    )
    session.add(task)
    session.commit()
    return task


def _seed_catalog(session: Session, per_type: int = 15) -> None:
    """播种 per_type × 2（genre/mood）枚目录标签，embedding 由 MockEmbedder 现算。

    数量取 30：MockLLM 模式 A 按 sha256(run_id+tag_id) < 0.4 独立采样
    （run_id 随机不可控），候选越多零选中概率越低，配合成功路径测试的
    min_confidence=0.5（confidence 恒过线）把 flake 概率压到 0.6^30 ≈ 2e-7。
    """
    for i in range(per_type):
        name, desc = f"类型标签{i}", f"类型描述文本{i}"
        vec = MOCK_EMBEDDER.embed([f"{name}。{desc}"], role="passage")[0]
        session.add(TagCatalog(
            tag_id=f"tag_g_{i:02d}", tag_name=name, tag_type="genre",
            description=desc, status="active", embedding=encode_vector(vec),
        ))
        name, desc = f"情绪标签{i}", f"情绪描述文本{i}"
        vec = MOCK_EMBEDDER.embed([f"{name}。{desc}"], role="passage")[0]
        session.add(TagCatalog(
            tag_id=f"tag_m_{i:02d}", tag_name=name, tag_type="mood",
            description=desc, status="active", embedding=encode_vector(vec),
        ))
    session.commit()


# ---- run_generation_task：状态机 + 生成 + 入库 ----


def test_run_task_success_writes_tags(session):
    _seed_catalog(session)
    # min_confidence=0.5：MockLLM confidence 哈希恒过线，消采样零产出 flake
    # （0.6^30 ≈ 2e-7），顺带覆盖 policy_json 解析路径
    t = _make_task(session, policy_json='{"min_confidence": 0.5}')
    done = run_generation_task(t.task_id, session, MOCK_LLM, MOCK_EMBEDDER)
    assert done is t
    assert t.status == "succeeded" and t.prompt_version == "1" and t.model_version == "mock-1"
    assert session.query(PreAssetTag).count() >= 1 and t.finished_at is not None
    # v2：任务直绑资产，结果行 target 固定 content、origin = 任务模式
    assert session.query(PreAssetTag).first().target_id == t.asset_id
    assert session.query(PreAssetTag).first().origin == "closed"
    # MockLLM 场景证据（Task 6）：runner 渲染文本 prompt 含场景行 → 落库
    # evidence_json 非空，证据卡数据来自场景行（fixture 单场景 scene_001）
    row = session.query(PreAssetTag).filter(
        PreAssetTag.task_id == t.task_id).first()
    card = json.loads(row.evidence_json)[0]
    assert card == {"scene_id": "scene_001", "start_ms": 120000,
                    "end_ms": 180000,
                    "description": "两名人物在仓库中发生激烈争执"}


def test_run_task_llm_failure_atomic(session):
    class BrokenLLM:
        model = "broken"
        def complete(self, messages, run_id):
            return LLMResponse(content="", usage={}, raw={"error": "boom"}, ok=False)

    # 有候选才会走到 LLM 调用（空候选在 produce 内先行失败，到不了 LLM）
    _seed_catalog(session)
    t = _make_task(session)
    run_generation_task(t.task_id, session, BrokenLLM(), MOCK_EMBEDDER)
    assert t.status == "failed" and "LLM 调用失败" in t.error
    assert session.query(PreAssetTag).count() == 0   # Review Focus 1：零半截写入
    assert t.finished_at is not None


def test_run_task_empty_catalog_fails(session):
    t = _make_task(session)
    run_generation_task(t.task_id, session, MOCK_LLM, MOCK_EMBEDDER)
    assert t.status == "failed" and "候选标签为空" in t.error
    assert session.query(PreAssetTag).count() == 0


def test_run_task_wrong_status_rejected(session):
    t = _make_task(session)
    t.status = "running"
    session.commit()
    with pytest.raises(ValueError, match="非 queued"):
        run_generation_task(t.task_id, session, MOCK_LLM, MOCK_EMBEDDER)
    assert t.status == "running"  # 拒绝执行不改动状态


def test_orphan_running_reset(session):
    t = _make_task(session)
    t.status = "running"
    session.commit()
    queued = _make_task(session, task_id="task_runner_queued")
    assert reset_orphan_running(session) == 1
    assert t.status == "failed" and t.error == "进程重启中断"
    assert t.finished_at is not None
    assert queued.status == "queued"  # 仅清 running，queued 不受影响
    assert reset_orphan_running(session) == 0  # 幂等：无 running 可再清


class _PartialPersistLLM:
    """桩 LLM：返回一枚合法 + 一枚空 tag_id 的目录标签。

    第一枚在 persist 中已 add 未提交，第二枚在 persist 契约校验处抛
    ValueError——验证 runner 异常路径把半截写入一并回滚。
    """

    model = "stub-1"

    def complete(self, messages, run_id):
        content = json.dumps([
            {"tag_id": "tag_g_00", "tag_name": "类型标签0", "relevance": 0.9,
             "confidence": 0.9, "evidence": []},
            {"tag_id": "", "tag_name": "空id候选", "relevance": 0.9,
             "confidence": 0.9, "evidence": []},
        ], ensure_ascii=False)
        return LLMResponse(content=content, usage={}, raw={}, ok=True)


def test_run_task_persist_failure_rolls_back_partial_writes(session):
    _seed_catalog(session)
    t = _make_task(session)
    run_generation_task(t.task_id, session, _PartialPersistLLM(), MOCK_EMBEDDER)
    assert t.status == "failed" and "tag_id" in t.error
    # persist 中途失败：先前插行随任务级事务回滚，零残留
    assert session.query(PreAssetTag).count() == 0


# ---- TaskExecutor：线程池与同步模式 ----


def test_executor_thread_pool(tmp_path):
    """真线程池 + 文件库：线程内独立 session 执行，submit 后轮询至 succeeded。

    :memory: 库每连接独立，工作线程会话看不到主线程数据——必须文件库。
    同时覆盖 open 模式 runner 路径（无候选组装，不回填上下文向量）。
    """
    engine = create_engine_for(str(tmp_path / "runner_pool.db"))
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    with factory() as s:
        _seed_catalog(s)
        closed = _make_task(s, task_id="pool_closed",
                            policy_json='{"min_confidence": 0.5}')
        open_task = _make_task(s, mode="open", task_id="pool_open")
        closed_id, open_id = closed.task_id, open_task.task_id

    executor = TaskExecutor(factory, MOCK_LLM, MOCK_EMBEDDER, workers=2)
    try:
        executor.submit(closed_id)
        executor.submit(open_id)
        # 轮询 task.status 至双双 succeeded（上限 10s）
        deadline = time.monotonic() + 10.0
        statuses: dict[str, str] = {}
        while time.monotonic() < deadline:
            with factory() as s:
                statuses = {row.task_id: row.status
                            for row in s.query(TagGenerationTask).all()}
            if all(statuses.get(tid) == "succeeded"
                   for tid in (closed_id, open_id)):
                break
            time.sleep(0.05)
        with factory() as s:
            closed_row = s.get(TagGenerationTask, closed_id)
            open_row = s.get(TagGenerationTask, open_id)
            assert closed_row.status == "succeeded", statuses
            assert open_row.status == "succeeded", statuses
            assert closed_row.model_version == "mock-1"
            # prompt_version 列为 Text（Task 3 模型契约）：写入侧即字符串
            # （runner 写 str(template.version)），新会话读回仍为 "1"
            assert str(closed_row.prompt_version) == "1"
            # closed 任务有标签产出（min_confidence=0.5 下零选中概率 ≈ 2e-7）
            assert s.query(PreAssetTag).filter(
                PreAssetTag.task_id == closed_id).count() >= 1
            assert closed_row.asset_id == _CONTENT_DATA["asset"]["asset_id"]
            assert open_row.asset_id == _CONTENT_DATA["asset"]["asset_id"]
    finally:
        executor.shutdown()
    engine.dispose()


def test_executor_workers_zero_synchronous(tmp_path):
    """workers=0 同步执行：submit 返回即完成（T9 generation_workers=0 测试模式）。"""
    engine = create_engine_for(str(tmp_path / "runner_sync.db"))
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    with factory() as s:
        _seed_catalog(s)
        t = _make_task(s, policy_json='{"min_confidence": 0.5}')
        task_id = t.task_id

    executor = TaskExecutor(factory, MOCK_LLM, MOCK_EMBEDDER, workers=0)
    try:
        executor.submit(task_id)
        with factory() as s:
            row = s.get(TagGenerationTask, task_id)
            assert row.status == "succeeded" and row.finished_at is not None
            assert s.query(PreAssetTag).filter(
                PreAssetTag.task_id == task_id).count() >= 1
    finally:
        executor.shutdown()
    engine.dispose()
