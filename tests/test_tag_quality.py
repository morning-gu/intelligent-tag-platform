"""标签质量五档汇总指标测试（开放评测报告 open_report 复用）。"""

from tagplatform.services.eval.tag_quality import quality_metrics


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
