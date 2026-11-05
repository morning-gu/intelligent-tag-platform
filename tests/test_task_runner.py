"""生成任务执行器测试（v2 重写：asset 直连 + pre_asset_tag 落库，Task 6）。

覆盖契约（v1 状态机形状 + v2 数据源/写入口）：
- test_run_task_success_writes_tags：成功路径写 pre_asset_tag（origin=
  closed、target_id=asset_id、candidate_name 空）、回填
  prompt_version/model_version/finished_at（v2 删除上下文向量回填——
  内容向量生成时实时计算，doc §3.1/§5）；
- test_run_task_llm_failure_atomic：LLM 失败任务置 failed 且零半截 pre 行
  （任务级事务，异常回滚）；
- test_run_task_empty_catalog_fails：空标签库显式失败（error 含"候选标签为空"）；
- test_task_fails_when_asset_unrecognized：content_json 为空的资产 →
  failed + error 含"未完成内容识别"（Review Focus 4；API 不 500 由
  test_api_generation 的 422 预检覆盖）；
- test_run_task_wrong_status_rejected：非 queued 拒绝执行（ValueError），
  且拒绝不改动状态；
- test_orphan_running_reset：running 孤儿 → failed（error="进程重启中断"），
  仅清 running、重复调用幂等；
- test_run_task_persist_failure_rolls_back_partial_writes：persist 中途
  契约校验失败（closed 缺 tag_id）时，先前未提交的插行一并回滚（零残留）；
- test_executor_thread_pool / test_executor_workers_zero_synchronous：
  真线程池（closed + open 双模式，open 执行器路径随 Task 7 origin 词表
  切换恢复）与同步模式。

Mock 技巧（与 llm/mock.py 咬合，v1 同款消 flake 构造）：
- MockLLM 模式 A 按 sha256(run_id + tag_id) < 0.4 独立采样选中，
  confidence 哈希值域 [0.5,1)；run_id 由 runner 内部 uuid 生成不可控——
  成功路径测试把 min_confidence 降到 0.5（confidence 恒过线）并播种
  30 枚候选，零选中概率 0.6^30 ≈ 2e-7，构造上消除 flake。
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
    TagDefine,
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
_CONTENT_JSON = json.dumps(_CONTENT_DATA, ensure_ascii=False)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _seed_asset(session: Session, asset_id: str = "movie_001",
                content_json: str | None = _CONTENT_JSON) -> None:
    """播种 media_asset：content_json 直落列（None = 未完成内容识别，
    Review Focus 4 的构造入口）。首次创建后复用（主键唯一）。"""
    if session.get(MediaAsset, asset_id) is None:
        session.add(MediaAsset(
            asset_id=asset_id, asset_type="movie", title="测试影片",
            content_json=content_json, source="test", created_at=_utcnow(),
        ))
        session.flush()


def _make_task(session: Session, mode: str = "closed",
               task_id: str = "task_runner_1", policy_json: str | None = None,
               asset_id: str = "movie_001") -> TagGenerationTask:
    """播种 media_asset（含夹具内容）+ tag_generation_task(queued)，返回任务。"""
    _seed_asset(session, asset_id)
    task = TagGenerationTask(
        task_id=task_id, asset_id=asset_id, mode=mode,
        policy_json=policy_json, status="queued", created_at=_utcnow(),
    )
    session.add(task)
    session.commit()
    return task


def _seed_catalog(session: Session, per_dir: int = 15) -> None:
    """播种 2 目录（类型/情绪）× per_dir 枚标签，embedding 由 MockEmbedder
    对 "名称。描述" 现算（与治理入库同源文本、passage 角色）。

    数量取 30：MockLLM 模式 A 按 sha256(run_id+tag_id) < 0.4 独立采样
    （run_id 随机不可控），候选越多零选中概率越低，配合成功路径测试的
    min_confidence=0.5（confidence 恒过线）把 flake 概率压到 0.6^30 ≈ 2e-7。
    """
    session.add(TagCatalog(dir_id="dir_g", dir_name="类型目录",
                           dir_type="dir_static", description="题材类目"))
    session.add(TagCatalog(dir_id="dir_m", dir_name="情绪目录",
                           dir_type="dir_static", description="情绪类目"))
    session.flush()  # 目录（外键父行）先落库，标签行的 dir_id 外键才成立
    for i in range(per_dir):
        name, desc = f"类型标签{i}", f"类型描述文本{i}"
        vec = MOCK_EMBEDDER.embed([f"{name}。{desc}"], role="passage")[0]
        session.add(TagDefine(
            tag_id=f"tag_g_{i:02d}", tag_name=name, tag_type="val_fixed",
            description=desc, dir_id="dir_g", status="active",
            embedding=encode_vector(vec),
        ))
        name, desc = f"情绪标签{i}", f"情绪描述文本{i}"
        vec = MOCK_EMBEDDER.embed([f"{name}。{desc}"], role="passage")[0]
        session.add(TagDefine(
            tag_id=f"tag_m_{i:02d}", tag_name=name, tag_type="val_fixed",
            description=desc, dir_id="dir_m", status="active",
            embedding=encode_vector(vec),
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
    assert t.status == "succeeded" and t.prompt_version == "1"
    assert t.model_version == "mock-1" and t.finished_at is not None
    rows = (session.query(PreAssetTag)
            .filter(PreAssetTag.task_id == t.task_id).all())
    assert len(rows) >= 1
    row = rows[0]
    # v2 落库形状：pre_asset_tag、origin=closed、content 级挂载
    assert row.origin == "closed" and row.review_status == "pending"
    assert row.target_type == "content" and row.target_id == "movie_001"
    assert row.tag_id is not None and row.candidate_name is None
    # MockLLM 场景证据：runner 渲染文本 prompt 含场景行 → 落库
    # evidence_json 非空，证据卡数据来自场景行（fixture 单场景 scene_001）
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
    assert session.query(PreAssetTag).count() == 0   # 任务级事务：零半截写入
    assert t.finished_at is not None


def test_run_task_empty_catalog_fails(session):
    t = _make_task(session)
    run_generation_task(t.task_id, session, MOCK_LLM, MOCK_EMBEDDER)
    assert t.status == "failed" and "候选标签为空" in t.error
    assert session.query(PreAssetTag).count() == 0


def test_task_fails_when_asset_unrecognized(session):
    """Review Focus 4：content_json 为空的资产 → 任务 failed，error 含
    "未完成内容识别"，零半截写入（API 预检 422 不 500 由 e2e 覆盖）。"""
    _seed_asset(session, "movie_null", content_json=None)
    t = _make_task(session, task_id="task_unrecognized", asset_id="movie_null")
    done = run_generation_task(t.task_id, session, MOCK_LLM, MOCK_EMBEDDER)
    assert done.status == "failed" and "未完成内容识别" in done.error
    assert done.finished_at is not None
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
    """桩 LLM：返回一枚合法 + 一枚空 tag_id 的 closed 标签。

    第一枚在 persist 中已插入未提交，第二枚在 persist 契约校验处抛
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
    同时覆盖 open 模式执行器路径（mtg 产线 → pre_asset_tag 落库，Task 7）。
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
            # prompt_version 列为 Text（模型契约）：写入侧即字符串
            # （runner 写 str(template.version)），新会话读回仍为 "1"
            assert str(closed_row.prompt_version) == "1"
            # closed 任务有标签产出（min_confidence=0.5 下零选中概率 ≈ 2e-7）
            assert s.query(PreAssetTag).filter(
                PreAssetTag.task_id == closed_id).count() >= 1
            # open 任务：mtg 产线三候选全部落库为 open 自由候选（库内 30 枚
            # 标签与 Mock 候选近正交，归一不命中），prompt_version=mtg-v1
            open_rows = (s.query(PreAssetTag)
                         .filter(PreAssetTag.task_id == open_id).all())
            assert len(open_rows) == 3
            assert all(row.origin == "open" and row.tag_id is None
                       and row.candidate_name and row.review_status == "pending"
                       for row in open_rows)
            assert str(open_row.prompt_version) == "mtg-v1"
    finally:
        executor.shutdown()
    engine.dispose()


def test_executor_workers_zero_synchronous(tmp_path):
    """workers=0 同步执行：submit 返回即完成（generation_workers=0 测试模式）。"""
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
