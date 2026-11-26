"""标签价值三方向权重配置、聚合与分档归桶单测。"""
from __future__ import annotations

import pytest

from tagplatform.services.tags.value_scoring import (
    DEFAULT_VALUE_WEIGHTS,
    VALUE_DIRECTIONS,
    aggregate_value_score,
    format_score,
    normalize_tag_kind,
    parse_direction_weights,
    score_bucket,
    weights_dict_for_kind,
)
from tagplatform.settings import Settings


def test_default_weights_match_spec_by_tag_kind():
    settings = Settings(_env_file=None)
    weights = settings.tag_value_weights
    assert weights == {k: pytest.approx(v) for k, v in DEFAULT_VALUE_WEIGHTS.items()}


def test_settings_weights_support_override_and_normalization():
    settings = Settings(
        tag_value_weights_content="2,3,5",
        tag_value_weights_scene="6,3,1",
        tag_value_weights_character="5,3,2",
    )
    assert settings.tag_value_weights["content"] == pytest.approx((0.2, 0.3, 0.5))
    assert settings.tag_value_weights["scene"] == pytest.approx((0.6, 0.3, 0.1))
    assert settings.tag_value_weights["character"] == pytest.approx((0.5, 0.3, 0.2))


@pytest.mark.parametrize("bad", ["", "1,2", "1,2,3,4", "-1,2,3", "0,0,0", "a,b,c"])
def test_invalid_weights_raise(bad):
    with pytest.raises(ValueError):
        parse_direction_weights(bad, "tag_value_weights_content")


def test_aggregate_value_score_applies_kind_specific_weights():
    scores = {"presentation": 4, "search": 2, "recommendation": 0}
    # content: 0.30*4 + 0.40*2 + 0.30*0 = 2.00
    assert aggregate_value_score(scores, "content") == 2.0
    # scene: 0.50*4 + 0.40*2 + 0.10*0 = 2.80
    assert aggregate_value_score(scores, "scene") == 2.8
    # character: 0.55*4 + 0.30*2 + 0.15*0 = 2.80
    assert aggregate_value_score(scores, "character") == 2.8

    scores2 = {"presentation": 3, "search": 4, "recommendation": 1}
    # content: 0.90 + 1.60 + 0.30 = 2.80
    assert aggregate_value_score(scores2, "content") == 2.8
    # scene: 1.50 + 1.60 + 0.10 = 3.20
    assert aggregate_value_score(scores2, "scene") == 3.2
    # character: 1.65 + 1.20 + 0.15 = 3.00
    assert aggregate_value_score(scores2, "character") == 3.0

    # 任意方向为 None 时聚合结果为 None（None != 0）
    assert aggregate_value_score(
        {"presentation": 4, "search": None, "recommendation": 2}, "content"
    ) is None


def test_normalize_tag_kind_and_weights_dict():
    assert normalize_tag_kind("content") == "content"
    assert normalize_tag_kind("film") == "content"
    assert normalize_tag_kind("内容级") == "content"
    assert normalize_tag_kind("scene") == "scene"
    assert normalize_tag_kind("场景级") == "scene"
    assert normalize_tag_kind("character") == "character"
    assert normalize_tag_kind("人物级") == "character"
    assert normalize_tag_kind("角色级") == "character"
    assert normalize_tag_kind(None) == "content"
    assert tuple(weights_dict_for_kind("scene")) == VALUE_DIRECTIONS
    assert weights_dict_for_kind("scene") == {
        "presentation": 0.5, "search": 0.4, "recommendation": 0.1,
    }


def test_score_bucket_uses_half_up_and_format_score():
    assert score_bucket(0.49) == 0
    assert score_bucket(0.50) == 1
    assert score_bucket(1.50) == 2
    assert score_bucket(2.50) == 3
    assert score_bucket(3.49) == 3
    assert score_bucket(3.50) == 4
    assert score_bucket(4.0) == 4
    assert format_score(None) == "null"
    assert format_score(3) == "3"
    assert format_score(3.0) == "3"
    assert format_score(2.85) == "2.85"
