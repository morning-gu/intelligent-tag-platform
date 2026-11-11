"""消融实验框架：只改一个因子，其余全固定，量化"这个因子到底值不值"。

为什么需要这个模块
==================

开放式生成有四个可调因子：输入模态（关键帧/场景文本/台词）、生成轮数、
语义合并阈值、judge 开关。改参数后"看起来标签更好了"是不作数的判断——
候选条数、judge 通过率、标签具体程度会同时变化，凭感觉永远说不清是哪一项
起了作用。

消融实验的纪律（与 genometag-mllm 的 `configs/ablation/*.yaml` 一致）
====================================================================

上游五份配置 `full / video-only / script-only / dialogue-only / video-script`
**只有一个字段不同**（`annotation.modalities`），其余参数逐字相同，最后用
`06_evaluate.py --extra ...` 生成对照表。本模块把这条纪律固化成代码：

1. `Variant` 只描述"改了什么"，其余参数由调用方从同一份 `Settings` 拷贝；
2. `compare()` 只吃两个变体的逐项原值，**要求同一批受试项**（缺项直接报错，
   不允许"少评几条把平均拉高"）；
3. 差异用**配对 bootstrap** 判定，而不是比两个点估计的大小——同一批片子
   上跑两个配置本来就是配对的，配对比独立检验灵敏得多。

本地模态映射
============

`prompts.render_content` 的资产文本天然三分，`build_generation_messages`
再叠加关键帧，因此：

- `video`   → 场景关键帧（`frames_by_scene` 非空）；
- `script`  → 摘要 + 人物 + 场景描述 + 氛围/情绪/地点/音乐；
- `dialogue`→ 场景内的 `台词：` 行。

三个都开 = full。关掉 video 就传空帧表；关掉 dialogue 就滤掉台词行。

分层红线：本模块只用标准库，不 import fastapi / numpy / 平台模块
（设计文档 §9.4）。
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

__all__ = [
    "ABLATION_VARIANTS",
    "MODALITIES",
    "Variant",
    "VariantResult",
    "compare",
    "paired_bootstrap",
    "variant_by_name",
]

# 三种输入模态（顺序即展示顺序；也是审计 JSON 里的字段名）
MODALITIES = ("video", "script", "dialogue")


@dataclass(frozen=True)
class Variant:
    """一个消融变体：名字 + 开了哪几种模态。

    `modalities` 用 `frozenset` 而不是 tuple，是因为它的语义就是集合
    （顺序无关），且"是不是全覆盖"要反复判断。空集合**不允许**：一条内容
    都不给模型，产出没有任何解释力，纯属浪费调用。
    """

    name: str
    modalities: frozenset[str]

    def __post_init__(self) -> None:
        unknown = set(self.modalities) - set(MODALITIES)
        if unknown:
            raise ValueError(
                f"未知模态 {sorted(unknown)}，只支持 {list(MODALITIES)}")
        if not self.modalities:
            raise ValueError(f"变体 {self.name} 没有开启任何模态")

    @property
    def is_full(self) -> bool:
        """是否全模态（对照组）。"""
        return self.modalities == frozenset(MODALITIES)

    @property
    def dropped(self) -> tuple[str, ...]:
        """相对 full 关掉了哪些模态（用于表格里的"缺什么"列）。"""
        return tuple(m for m in MODALITIES if m not in self.modalities)


# 与上游 `configs/ablation/` 五份配置一一对应
ABLATION_VARIANTS: tuple[Variant, ...] = (
    Variant("full", frozenset(MODALITIES)),
    Variant("video-only", frozenset({"video"})),
    Variant("script-only", frozenset({"script"})),
    Variant("dialogue-only", frozenset({"dialogue"})),
    Variant("video-script", frozenset({"video", "script"})),
)


def variant_by_name(name: str) -> Variant:
    """按名字取变体（脚本 `--variants` 参数用）；未知名字报错并列出全部。"""
    for variant in ABLATION_VARIANTS:
        if variant.name == name:
            return variant
    known = ", ".join(v.name for v in ABLATION_VARIANTS)
    raise KeyError(f"未知变体 {name!r}，可用：{known}")


@dataclass
class VariantResult:
    """一个变体在一批受试项上的结果。

    - `per_item`：`{指标名: [每项原值]}`，顺序必须与其他变体一致（`compare`
      按位置配对）；
    - `metrics`：`{指标名: 宏平均}`，由 `finalize()` 从 `per_item` 算出来——
      不手工填，避免"平均值和明细对不上"。
    """

    variant: Variant
    per_item: dict[str, list[float]] = field(default_factory=dict)
    metrics: dict[str, float | None] = field(default_factory=dict)
    items: list[str] = field(default_factory=list)

    def add(self, metrics: dict[str, float], *, item: str = "") -> None:
        """追加一项的指标原值（每次调用代表一个受试项）。

        `item` 是受试项的名字（如 `asset_id`），只用于留痕与排查：`compare`
        按**位置**配对而不按名字，所以两个变体的 `items` 顺序必须一致
        （脚本层统一按排好序的资产名遍历来保证）。
        """
        self.items.append(item)
        for name, value in metrics.items():
            self.per_item.setdefault(name, []).append(float(value))

    def finalize(self) -> VariantResult:
        """从 `per_item` 算宏平均；空列表记 `None`（"暂无"），不记 0。"""
        self.metrics = {
            name: (sum(values) / len(values) if values else None)
            for name, values in self.per_item.items()
        }
        return self

    @property
    def n_items(self) -> int:
        """受试项数（取任意指标的长度；空结果记 0）。"""
        return len(next(iter(self.per_item.values()), []))


def paired_bootstrap(left: list[float], right: list[float], *,
                     n_resamples: int = 5000, seed: int = 20260101
                     ) -> tuple[float, float, tuple[float, float]]:
    """配对 bootstrap：`(均值差 left-right, 双侧 p 值, 差的 95% 区间)`。

    与上游 `paired_bootstrap` 同口径（对逐项差值重采样、双侧 p），补两点：
    - 返回置信区间而不只是 p 值：区间跨 0 与 p<0.05 是一致的，但区间宽度
      更能说明"差异有多大"，而不是只回答"显不显著"；
    - 显式校验长度相等——消融的前提就是同一批受试项，长度不等说明调用方
      把两份不同批次的结果混在一起了，必须当场报错。

    p 值算法：`2·min(P(重采样均值 ≤ 0), P(重采样均值 ≥ 0))`，夹到 1.0。
    样本数小于 2 时差值无分布可谈，直接返回 `(0.0, 1.0, (0.0, 0.0))`。
    """
    if len(left) != len(right):
        raise ValueError(
            f"配对样本长度必须一致（左 {len(left)} / 右 {len(right)}）——"
            "消融要求同一批受试项")
    if len(left) < 2:
        return 0.0, 1.0, (0.0, 0.0)

    diffs = [a - b for a, b in zip(left, right, strict=True)]
    observed = sum(diffs) / len(diffs)
    rng = random.Random(seed)
    n = len(diffs)
    means: list[float] = []
    for _ in range(max(1, n_resamples)):
        total = 0.0
        for _ in range(n):
            total += diffs[rng.randrange(n)]
        means.append(total / n)
    means.sort()

    non_positive = sum(1 for value in means if value <= 0) / len(means)
    non_negative = sum(1 for value in means if value >= 0) / len(means)
    p_value = min(1.0, 2 * min(non_positive, non_negative))

    low_index = int(0.025 * len(means))
    high_index = min(len(means) - 1, int(0.975 * len(means)) - 1)
    return observed, p_value, (means[max(0, low_index)], means[high_index])


def compare(full: VariantResult, variant: VariantResult, *,
            n_resamples: int = 5000, seed: int = 20260101) -> dict[str, dict]:
    """全模态 vs 某个消融变体：逐指标给出差值、p 值与区间。

    **差值方向固定为 `full − variant`**：正数 = 全模态更好（关掉这项模态损失
    了性能），负数 = 该模态不但没用还在添乱。方向写反是这类报表最常见的
    误读，所以这里把它定死而不是留给调用方选。

    返回 `{指标名: {"delta", "p", "ci", "full", "variant"}}`。只比较两个
    变体都有的指标：某指标只在一边出现，说明两边评的不是同一件事，静默跳过
    比报错更危险——所以跳过时会把名字记进返回值的 `"_skipped"`（字符串
    列表），让调用方能打印出来。
    """
    if not full.variant.is_full:
        raise ValueError(
            f"对照组必须是全模态变体，实际是 {full.variant.name}"
            f"（{sorted(full.variant.modalities)}）")
    if variant.variant.name == full.variant.name:
        raise ValueError("对照组与变体不能是同一个")

    out: dict[str, dict] = {}
    skipped: list[str] = []
    for name, left in full.per_item.items():
        right = variant.per_item.get(name)
        if right is None:
            skipped.append(name)
            continue
        if len(left) != len(right):
            raise ValueError(
                f"指标 {name} 的受试项数不一致（full {len(left)} / "
                f"{variant.variant.name} {len(right)}）")
        delta, p_value, ci = paired_bootstrap(left, right,
                                              n_resamples=n_resamples, seed=seed)
        out[name] = {
            "full": full.metrics.get(name),
            "variant": variant.metrics.get(name),
            "delta": delta,
            "p": p_value,
            "ci": ci,
        }
    # 只遍历 full 会漏掉"变体独有"的指标——那同样说明两边评的不是同一件事，
    # 不报出来就是静默丢数据（例如某个变体多算了一项指标）。
    skipped.extend(name for name in variant.per_item if name not in full.per_item)
    if skipped:
        out["_skipped"] = skipped  # type: ignore[assignment]
    return out