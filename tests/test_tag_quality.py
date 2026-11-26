"""标签质量五档汇总指标与历史迁移保留。

生产附带质量评估（分流快照、评估队列与单片重试）已移除：开放生成与闭环
生成同一收尾流程，质量评估统一走独立开放评测页。本文件只保留两块：
- quality_metrics 的五档聚合口径（开放评测报告 open_report 复用）；
- 0012 → head 迁移保留既有生产数据（tag_quality_evaluation 表与
  quality_model 列作为历史数据保留，不再写入）。
"""
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.orm import Session

from tagplatform.capabilities.storage import create_engine_for
from tagplatform.capabilities.storage.models import TagGenerationTask, TagQualityEvaluation
from tagplatform.services.eval.tag_quality import quality_metrics
from tagplatform.settings import get_settings


def _score(relevance=2, semantics=2, value=2) -> dict:
    return {
        "relevance": {"score": relevance, "reason": "内容评分"},
        "semantics": {"score": semantics, "reason": "语义评分"},
        "value": {"score": value, "reason": "标签价值评分"},
    }


def test_metrics_missing_value_dimension_keeps_separate_coverage():
    # 历史记录缺 value 对象：不进 value 分母，也不算无法判断；
    # 显式 score=null 才计入无法判断。
    legacy = _score()
    del legacy["value"]
    scores = [legacy] + [_score(value=rating) for rating in (0, 1, 2, None)]
    metrics = quality_metrics(5, scores)
    assert metrics.evaluated_tags == 5
    assert metrics.max_score == 4
    assert metrics.value.model_dump() == {
        "evaluated_tags": 4, "average": 1.0, "high_rate": 0.0,
        "distribution": [1, 1, 1, 0, 0], "undetermined": 1, "undetermined_rate": 0.25,
    }
    assert metrics.joint_high_rate == 0
    assert metrics.three_dimension_high_rate == 0
    assert metrics.undetermined_tags == 1


def test_metrics_aggregates_on_five_level_scale():
    metrics = quality_metrics(2, [_score(4, 4, 3), _score(4, 4, 4)])
    assert metrics.coverage == 1
    assert metrics.max_score == 4
    assert metrics.relevance.distribution == [0, 0, 0, 0, 2]
    assert metrics.relevance.average == 4
    assert metrics.joint_high_rate == 1
    assert metrics.value.average == 3.5
    assert metrics.value.distribution == [0, 0, 0, 1, 1]
    assert metrics.three_dimension_high_rate == 0.5


def test_metrics_supports_weighted_float_value_scores():
    # 2.85 四舍五入入 3 档，3.65 四舍五入入 4 档；均分保留精确小数 (2.85+3.65)/2 = 3.25
    metrics = quality_metrics(2, [_score(4, 4, 2.85), _score(4, 4, 3.65)])
    assert metrics.value.average == 3.25
    assert metrics.value.distribution == [0, 0, 0, 1, 1]
    assert metrics.value.high_rate == 0.5
    assert metrics.three_dimension_high_rate == 0.5


def test_migration_preserves_existing_production_results(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    db_path = str(tmp_path / "quality.db")
    monkeypatch.setenv("TAGPLT_DATABASE_PATH", db_path)
    get_settings.cache_clear()
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    engine = create_engine_for(db_path)
    try:
        command.upgrade(cfg, "0012")
        with engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO media_asset (asset_id, asset_type, title) VALUES ('a', 'movie', 'old')"
            ))
            connection.execute(text(
                "INSERT INTO tag_generation_task (task_id, asset_id, mode, status) "
                "VALUES ('old_task', 'a', 'open', 'succeeded')"
            ))
            connection.execute(text(
                "INSERT INTO pre_asset_tag (asset_id, task_id, target_id, origin, candidate_name) "
                "VALUES ('a', 'old_task', 'a', 'open', '旧标签')"
            ))
        command.upgrade(cfg, "head")
        with Session(engine) as session:
            task = session.get(TagGenerationTask, "old_task")
            assert task.status == "succeeded"
            assert task.batch_id is None and task.quality_model is None
            assert session.get(TagQualityEvaluation, "old_task") is None
            assert session.scalar(text("SELECT candidate_name FROM pre_asset_tag")) == "旧标签"
    finally:
        get_settings.cache_clear()
        engine.dispose()
