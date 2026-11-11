"""消融实验框架：纪律比算法更重要，所以重点锁"拒绝误用"。"""

from __future__ import annotations

import pytest

from tagplatform.eval import (
    ABLATION_VARIANTS,
    MODALITIES,
    Variant,
    VariantResult,
    compare,
    paired_bootstrap,
    variant_by_name,
)


# ---------------------------------------------------------------- 变体定义

def test_variants_match_upstream_config_names():
    """与 genometag-mllm `configs/ablation/*.yaml` 的五份配置逐一对应。"""
    assert [variant.name for variant in ABLATION_VARIANTS] == [
        "full", "video-only", "script-only", "dialogue-only", "video-script",
    ]


def test_only_full_variant_is_full():
    full = [v.name for v in ABLATION_VARIANTS if v.is_full]

    assert full == ["full"]
    assert variant_by_name("full").modalities == frozenset(MODALITIES)


def test_dropped_lists_missing_modalities_in_stable_order():
    """顺序固定为 `MODALITIES` 声明的顺序，报表才可比。"""
    assert variant_by_name("full").dropped == ()
    assert variant_by_name("video-only").dropped == ("script", "dialogue")
    assert variant_by_name("video-script").dropped == ("dialogue",)


def test_variant_rejects_unknown_and_empty_modalities():
    with pytest.raises(ValueError, match="未知模态"):
        Variant("audio-only", frozenset({"audio"}))
    with pytest.raises(ValueError, match="没有开启任何模态"):
        Variant("nothing", frozenset())


def test_variant_by_name_lists_valid_names_on_typo():
    with pytest.raises(KeyError, match="video-only"):
        variant_by_name("video_only")


# ---------------------------------------------------------------- 结果聚合

def test_finalize_uses_macro_average():
    result = VariantResult(variant_by_name("video-only"))
    result.add({"accepted": 2.0, "frames": 0.0}, item="a1")
    result.add({"accepted": 4.0, "frames": 0.0}, item="a2")

    result.finalize()

    assert result.metrics["accepted"] == pytest.approx(3.0)
    assert result.metrics["frames"] == pytest.approx(0.0)
    assert result.n_items == 2
    assert result.items == ["a1", "a2"]


def test_finalize_empty_is_none_not_zero():
    """"一条都没评"算出的平均是"暂无"，不是 0——0 会被读成"性能归零"。"""
    result = VariantResult(variant_by_name("script-only")).finalize()

    assert result.metrics == {}
    assert result.n_items == 0


def test_add_missing_metric_on_one_item_keeps_lists_uneven():
    """只要某个指标少加一项，长度立刻不等 —— `compare` 必须能抓到这种错。"""
    result = VariantResult(variant_by_name("video-only"))
    result.add({"a": 1.0}, item="x")
    result.add({"b": 2.0}, item="y")

    result.finalize()

    assert len(result.per_item["a"]) == 1
    assert len(result.per_item["b"]) == 1


# ---------------------------------------------------------------- 配对 bootstrap

def test_paired_bootstrap_constant_shift_is_never_zero_difference():
    """每一对差值都是 3 → 重采样恒为 3，p 必为 0（不可能跨过 0）。"""
    delta, p_value, ci = paired_bootstrap([3.0, 3.0, 3.0], [0.0, 0.0, 0.0],
                                          n_resamples=200, seed=7)

    assert delta == pytest.approx(3.0)
    assert p_value == 0.0
    assert ci == (pytest.approx(3.0), pytest.approx(3.0))


def test_paired_bootstrap_p_is_never_above_one():
    """代码里写了 `min(1.0, ...)`；不能因为重采样噪声给出 p>1。"""
    _, p_value, _ = paired_bootstrap([1.0, -1.0, 2.0, -2.0], [0.0] * 4,
                                     n_resamples=500, seed=3)

    assert 0.0 <= p_value <= 1.0


def test_paired_bootstrap_is_deterministic_for_a_seed():
    first = paired_bootstrap([1.0, 2.0, 3.0], [0.0, 0.0, 0.0], n_resamples=300, seed=42)
    second = paired_bootstrap([1.0, 2.0, 3.0], [0.0, 0.0, 0.0], n_resamples=300, seed=42)

    assert first == second


def test_paired_bootstrap_rejects_length_mismatch():
    with pytest.raises(ValueError, match="同一批受试项"):
        paired_bootstrap([1.0, 2.0], [1.0])


def test_paired_bootstrap_single_item_is_neutral():
    """一项没有分布可谈，返回中性结论而不是造一个 p 值出来。"""
    assert paired_bootstrap([5.0], [0.0]) == (0.0, 1.0, (0.0, 0.0))
    assert paired_bootstrap([], []) == (0.0, 1.0, (0.0, 0.0))


# ---------------------------------------------------------------- compare

def _result(name: str, values: dict[str, list[float]]) -> VariantResult:
    result = VariantResult(variant=variant_by_name(name))
    for index in range(len(next(iter(values.values()), []))):
        result.add({key: items[index] for key, items in values.items()},
                   item=f"asset-{index}")
    return result.finalize()


def test_compare_delta_is_full_minus_variant():
    """方向定死为 full − 变体：正数 = 全模态更好。写反是这类报表的经典误读。"""
    full = _result("full", {"accepted": [3.0, 5.0, 4.0]})
    variant = _result("video-only", {"accepted": [1.0, 3.0, 2.0]})

    out = compare(full, variant, n_resamples=200, seed=11)

    assert out["accepted"]["delta"] == pytest.approx(2.0)
    assert out["accepted"]["full"] == pytest.approx(4.0)
    assert out["accepted"]["variant"] == pytest.approx(2.0)


def test_compare_reports_metrics_present_on_only_one_side():
    """一边有、一边没有 → 跳过但**留名**：静默丢指标比报错更危险。"""
    full = _result("full", {"accepted": [1.0, 2.0], "judge_pass": [0.5, 0.5]})
    variant = _result("dialogue-only", {"accepted": [1.0, 2.0], "extra": [1.0, 1.0]})

    out = compare(full, variant, n_resamples=200)

    assert "judge_pass" in out["_skipped"]
    assert "extra" in out["_skipped"]
    assert "extra" not in out


def test_compare_rejects_non_full_control():
    """对照组必须是全模态，否则"差值"没有解释基准。"""
    control = _result("video-only", {"accepted": [1.0, 2.0]})
    variant = _result("script-only", {"accepted": [1.0, 2.0]})

    with pytest.raises(ValueError, match="对照组必须是全模态"):
        compare(control, variant)


def test_compare_rejects_same_variant_twice():
    full = _result("full", {"accepted": [1.0, 2.0]})
    other = _result("full", {"accepted": [1.0, 2.0]})

    with pytest.raises(ValueError, match="不能是同一个"):
        compare(full, other)


def test_compare_rejects_unequal_item_counts():
    """少评几条把平均拉高，正是消融最容易犯的作弊。"""
    full = _result("full", {"accepted": [1.0, 2.0, 3.0]})
    variant = _result("video-only", {"accepted": [1.0, 2.0]})

    with pytest.raises(ValueError, match="受试项数不一致"):
        compare(full, variant)