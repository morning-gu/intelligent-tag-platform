"""过滤责任链测试：注册表顺序、占位环节透传、自定义环节丢弃与
runner 集成（第 7 步）。

覆盖契约：
- 默认链初始态 list_filters() == ["quality", "fence"]（顺序即执行序，
  质量评估在前、信息围栏在后）；
- name 重复注册抛 ValueError；空链原样透传（run_filter_chain 不改动）；
- 两占位环节原样透传：条数不变、dropped 恒 0、note 标明未评估/未围栏；
- 自定义环节丢弃后 runner 只落通过行，任务仍 succeeded（"过滤掉不算
  失败"）；环节抛错 → 任务 failed + 零半截写入（任务级事务）。
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from conftest import MOCK_EMBEDDER, MOCK_LLM
from sqlalchemy.orm import Session

from tagplatform.db.models import (
    MediaAsset,
    PreAssetTag,
    TagCatalog,
    TagDefine,
    TagGenerationTask,
)
from tagplatform.filtering import (
    FilterContext,
    FilterOutcome,
    list_filters,
    register_filter,
    run_filter_chain,
)
from tagplatform.filtering.base import _CHAIN
from tagplatform.filtering.fence import InfoFenceFilter
from tagplatform.filtering.quality import QualityFilter
from tagplatform.generation import ProducedTag
from tagplatform.schemas import ContentJSON, parse_content
from tagplatform.tasks import run_generation_task
from tagplatform.vectors import encode_vector

_CONTENT_DATA = json.loads(
    (Path(__file__).parent / "fixtures" / "content_movie.json").read_text(
        encoding="utf-8")
)
_CONTENT_JSON = json.dumps(_CONTENT_DATA, ensure_ascii=False)
_CONTENT = parse_content(json.loads(_CONTENT_JSON))


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _tag(name: str, tag_id: str | None = "tag_x") -> ProducedTag:
    """构造一枚最小 ProducedTag（closed 形状：tag_id 非空）。"""
    return ProducedTag(
        origin="closed", tag_id=tag_id, tag_name=name,
        candidate_type=None, candidate_description=None,
        relevance_score=0.9, confidence=0.9, evidence=[],
    )


def _ctx(task_id: str = "task_f") -> FilterContext:
    return FilterContext(task_id=task_id, asset_id="movie_001", mode="closed",
                         content=_CONTENT)


@pytest.fixture()
def restore_chain():
    """快照模块级责任链并在测试后还原，避免测试污染全局状态。"""
    saved = list(_CHAIN)
    yield
    _CHAIN.clear()
    _CHAIN.extend(saved)


# ---- 注册表与默认链 ----


def test_default_chain_order():
    """默认链顺序即"先质量评估、后信息围栏"。"""
    assert list_filters() == ["quality", "fence"]


def test_default_chain_instances_registered():
    assert isinstance(_CHAIN[0], QualityFilter)
    assert isinstance(_CHAIN[1], InfoFenceFilter)


def test_duplicate_name_raises(restore_chain):
    class _Dup:
        name = "quality"

        def filter(self, tags, context, session):
            return FilterOutcome(tags=list(tags))

    with pytest.raises(ValueError, match="已注册"):
        register_filter(_Dup())


def test_empty_chain_passes_through(restore_chain):
    _CHAIN.clear()
    tags = [_tag("甲"), _tag("乙", tag_id="tag_y")]
    kept, reports = run_filter_chain(tags, _ctx(), None)
    assert kept == tags
    assert reports == []


# ---- 占位环节行为 ----


def test_placeholder_stages_pass_through():
    tags = [_tag("甲"), _tag("乙", tag_id="tag_y")]
    kept, reports = run_filter_chain(tags, _ctx(), None)
    assert kept == tags
    assert [r.name for r in reports] == ["quality", "fence"]
    assert all(r.kept == 2 and r.dropped == 0 for r in reports)
    assert "未评估" in reports[0].note
    assert "未围栏" in reports[1].note


# ---- 自定义环节：丢弃 / 改写 / 抛错 ----


class _DropAll:
    """丢弃全部候选的测试环节。"""

    name = "test_drop_all"

    def filter(self, tags, context, session):
        return FilterOutcome(tags=[], note="test: 全丢弃")


class _DropNamed:
    """按标签名丢弃的测试环节（验证链式串接）。"""

    name = "test_drop_named"

    def filter(self, tags, context, session):
        return FilterOutcome(
            tags=[t for t in tags if t.tag_name != "甲"], note="test: 去甲")


def test_custom_filter_drops(restore_chain):
    register_filter(_DropNamed())
    tags = [_tag("甲"), _tag("乙", tag_id="tag_y")]
    kept, reports = run_filter_chain(tags, _ctx(), None)
    assert [t.tag_name for t in kept] == ["乙"]
    dropped = {r.name: r.dropped for r in reports}
    assert dropped["test_drop_named"] == 1
    assert dropped["quality"] == 0 and dropped["fence"] == 0


def test_exception_propagates(restore_chain):
    class _Boom:
        name = "test_boom"

        def filter(self, tags, context, session):
            raise RuntimeError("环节崩溃")

    register_filter(_Boom())
    with pytest.raises(RuntimeError, match="环节崩溃"):
        run_filter_chain([_tag("甲")], _ctx(), None)


# ---- 日志输出（"标签去哪了"要能从日志里查出来） ----


def test_chain_logs_start_stage_and_end(caplog):
    tags = [_tag("甲"), _tag("乙", tag_id="tag_y")]
    with caplog.at_level("INFO", logger="tagplatform.filtering"):
        run_filter_chain(tags, _ctx(), None)
    messages = [record.getMessage() for record in caplog.records]
    assert any("过滤责任链开始" in m and "task_f" in m for m in messages)
    assert any(
        "过滤环节 [1/2] quality 完成" in m and "保留 2 条、丢弃 0 条" in m
        for m in messages)
    assert any("过滤环节 [2/2] fence 完成" in m for m in messages)
    assert any("过滤责任链结束" in m and "2 条候选 → 2 条通过" in m
               for m in messages)


def test_stage_logs_input_tag_names(caplog):
    """两个占位环节各自打出收到的候选名，便于排查丢在哪一步。"""
    tags = [_tag("甲"), _tag("乙", tag_id="tag_y")]
    with caplog.at_level("INFO", logger="tagplatform.filtering"):
        run_filter_chain(tags, _ctx(), None)
    messages = [record.getMessage() for record in caplog.records]
    assert any("质量评估" in m and "收到 2 条候选" in m and "甲" in m
               for m in messages)
    assert any("信息围栏" in m and "收到 2 条候选" in m for m in messages)


def test_dropped_detail_logged(restore_chain, caplog):
    """有丢弃时打出被丢标签名（明细只进日志，不入库）。"""
    register_filter(_DropNamed())
    with caplog.at_level("INFO", logger="tagplatform.filtering"):
        run_filter_chain([_tag("甲"), _tag("乙", tag_id="tag_y")], _ctx(), None)
    messages = [record.getMessage() for record in caplog.records]
    assert any("test_drop_named 丢弃明细" in m and "甲" in m
               for m in messages)


# ---- runner 集成（第 7 步） ----


def _seed_asset(session: Session, content_json: str | None = _CONTENT_JSON) -> None:
    session.add(MediaAsset(
        asset_id="movie_001", asset_type="movie", title="测试影片",
        content_json=content_json, source="test", created_at=_utcnow(),
    ))
    session.flush()


def _seed_catalog(session: Session, per_dir: int = 15) -> None:
    session.add(TagCatalog(dir_id="dir_g", dir_name="类型目录",
                           dir_type="dir_static", description="题材类目"))
    session.flush()
    for i in range(per_dir):
        name, desc = f"类型标签{i}", f"类型描述文本{i}"
        vec = MOCK_EMBEDDER.embed([f"{name}。{desc}"], role="passage")[0]
        session.add(TagDefine(
            tag_id=f"tag_g_{i:02d}", tag_name=name, tag_type="val_fixed",
            description=desc, dir_id="dir_g", status="active",
            embedding=encode_vector(vec),
        ))
    session.commit()


def _make_task(session: Session, task_id: str = "task_filter_1") -> TagGenerationTask:
    _seed_asset(session)
    task = TagGenerationTask(
        task_id=task_id, asset_id="movie_001", mode="closed",
        # min_confidence=0.5：MockLLM confidence 恒过线，保证确有产出可过滤
        policy_json='{"min_confidence": 0.5}',
        status="queued", created_at=_utcnow(),
    )
    session.add(task)
    session.commit()
    return task


def test_runner_drops_before_persist(session, restore_chain):
    """全丢弃环节生效：任务 succeeded（不算失败）但零 pre 行。"""
    _seed_catalog(session)
    t = _make_task(session)
    register_filter(_DropAll())
    done = run_generation_task(t.task_id, session, MOCK_LLM, MOCK_EMBEDDER)
    assert done.status == "succeeded"
    assert session.query(PreAssetTag).count() == 0


def test_runner_filter_error_fails_task(session, restore_chain):
    """环节抛错：任务 failed + error 透出原因 + 零半截写入。"""
    _seed_catalog(session)
    t = _make_task(session, task_id="task_filter_2")

    class _Boom:
        name = "test_boom_runner"

        def filter(self, tags, context, session):
            raise RuntimeError("围栏服务不可用")

    register_filter(_Boom())
    done = run_generation_task(t.task_id, session, MOCK_LLM, MOCK_EMBEDDER)
    assert done.status == "failed" and "围栏服务不可用" in done.error
    assert done.finished_at is not None
    assert session.query(PreAssetTag).count() == 0


def test_runner_default_chain_keeps_all(session):
    """默认占位链不影响既有产出（回归保护：加链不改变入库结果）。"""
    _seed_catalog(session)
    t = _make_task(session, task_id="task_filter_3")
    done = run_generation_task(t.task_id, session, MOCK_LLM, MOCK_EMBEDDER)
    assert done.status == "succeeded"
    assert session.query(PreAssetTag).filter(
        PreAssetTag.task_id == t.task_id).count() >= 1


def test_context_carries_task_and_content():
    """FilterContext 形状：任务标识 + 内容（环节按需取用）。"""
    ctx = _ctx()
    assert ctx.task_id == "task_f" and ctx.asset_id == "movie_001"
    assert ctx.mode == "closed"
    assert isinstance(ctx.content, ContentJSON)
    assert ctx.content.content.scenes  # 夹具内容可被环节读取