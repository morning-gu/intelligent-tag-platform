"""回归：命中库内标签（`origin="catalog"` + `alias_name`）时登记别名。

事故现场（2026-11-10，8101 真机）：合并 main 后 `record_alias` 从
`tagplatform.library` 搬进 `tagplatform.ingest`，第一个参数由「标签库对象」
变成「ORM Session」（别名从 JSON 库落进 `tag_define.aliases` 表），
而 `tasks/runner.py` 仍按老签名传 `library`，于是 open 生成一旦出现
「相似并入」的候选就

    AttributeError: 'TagLibrary' object has no attribute 'get'

任务直接 failed（`gen_99d1dd463f89`）。这条用例把整条 runner 链路
（stub producer → persist_tags → record_alias → commit）跑一遍，钉住签名。
"""

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from conftest import MOCK_EMBEDDER, MOCK_LLM

from tagplatform.db.models import (
    MediaAsset,
    PreAssetTag,
    TagChangeLog,
    TagDefine,
    TagGenerationTask,
)
from tagplatform.generation import ProducedTag
from tagplatform.tasks import run_generation_task

_CONTENT = {
    "schema_version": "1.0",
    "asset": {"asset_id": "asset_alias_case", "asset_type": "movie",
              "title": "别名回归", "duration_ms": 30000},
    "content": {
        "summary": "雨夜复仇",
        "scenes": [
            {"scene_id": "scene_001",
             "time_range": {"start_ms": 0, "end_ms": 30000},
             "description": "主角在雨夜复仇",
             "visual_features": ["雨夜", "街道"],
             "dialogues": [],
             "entities": [],
             "events": []},
        ],
    },
    "metadata": {"source": "fixture", "model_version": "stub_v1",
                 "recognition_status": "completed"},
}


@dataclass
class _StubProducer:
    """吐一枚「命中库内标签 + 模型原词不同」的候选，逼出别名登记分支。"""

    builds_prompt: bool = True   # 跳过 DB 模板，避免依赖 prompts 表
    prompt_version: str = "stub"

    def produce(self, spec, session, llm, embedder):
        return [
            ProducedTag(
                origin="catalog", tag_id="tag_revenge", tag_name="复仇",
                candidate_type=None, candidate_description=None,
                relevance_score=0.9, confidence=0.9, evidence=[],
                dir_id="dir_theme", target_type="content",
                target_id=spec.asset_id, alias_name="报仇",
            )
        ]


def _seed_task(session):
    session.add(MediaAsset(
        asset_id=_CONTENT["asset"]["asset_id"], asset_type="movie",
        title=_CONTENT["asset"]["title"],
        content_json=json.dumps(_CONTENT, ensure_ascii=False),
        created_at=datetime.now(timezone.utc)))
    task = TagGenerationTask(
        task_id="gen_alias_case", asset_id=_CONTENT["asset"]["asset_id"],
        mode="open", status="queued", created_at=datetime.now(timezone.utc))
    session.add(task)
    session.commit()
    return task


def test_catalog_hit_records_alias(session, monkeypatch):
    """别名写进 tag_define.aliases + 变更日志，任务仍是 succeeded。"""
    import tagplatform.tasks.runner as runner_mod

    monkeypatch.setattr(runner_mod, "get_producer", lambda mode: _StubProducer())
    task = _seed_task(session)

    done = run_generation_task(task.task_id, session, MOCK_LLM, MOCK_EMBEDDER)

    assert done.status == "succeeded", f"任务失败: {done.error!r}"
    assert done.error is None

    # 1) 别名落库（record_alias 的副作用）
    tag = session.get(TagDefine, "tag_revenge")
    assert tag is not None
    assert "报仇" in json.loads(tag.aliases or "[]")

    # 2) 变更日志有一条 update
    log = (session.query(TagChangeLog)
           .filter(TagChangeLog.tag_id == "tag_revenge")
           .order_by(TagChangeLog.log_id.desc()).first())
    assert log is not None and log.action == "update"

    # 3) 候选行上仍保留别名预览（persist_tags 写 candidate_alias）
    row = (session.query(PreAssetTag)
           .filter(PreAssetTag.task_id == task.task_id).one())
    assert row.candidate_alias == "报仇"
    assert row.origin == "open"


def test_record_alias_signature_is_session_first():
    """record_alias 的第一个位置参数必须是 Session（防止签名再次漂移）。"""
    import inspect

    from tagplatform.ingest import record_alias

    params = list(inspect.signature(record_alias).parameters.values())
    assert params[0].name == "session"
    assert params[0].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert "library" not in inspect.signature(record_alias).parameters
