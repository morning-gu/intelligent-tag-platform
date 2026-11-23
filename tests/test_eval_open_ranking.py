"""eval/open_ranking 的单测：开放评测指标装配的口径钉死。

被测模块纯标准库、可被脚本与未来离线工具独立复用，测试同样只依赖
pytest 与被测模块本身。这里钉死的是几件容易写错的事：

1. **None≠0**——空分母（无有效片、该 θ 下覆盖率无片可算、无新词片）报
   None，与"得 0 分"严格区分；计数类（no_gt / review_missing / n_*）如实
   记 0；
2. **交集外命中只计数**（Review Focus 3）——不进准确率分子分母、不进
   标定，只进 no_gt 与命中通过率；
3. **评审失败片与漏判条目**——整片评审失败（整体 None）不进通过率/标定
   但保留在 n_ok、准确率照算、命中标签计入 review_missing；漏判条目
   （条目 None）排除出通过率与标定的分子分母、计入 review_missing；
4. **手算例**——准确率/随机/先验/覆盖/标定逐格对得上手工数字，防止公式
   漂移。
"""

from __future__ import annotations

import pytest

from tagplatform.services.eval.open_ranking import (
    DEFAULT_THETAS,
    DEFAULT_THETA_HEADLINE,
    OpenSample,
    evaluate_open,
)

# 手算夹具的全局 gt（4 部片）——先验均值（按"有分的影片"取均值）：
#   a=(0.9+0.8)/2=0.85  b=(0.7+0.2)/2=0.45  c=0.5  d=0.3  e=0.1
#   f=0.6  g=0.9  h=0.4  i=0.3  j=0.1
GT = {
    "m1": {"a": 0.9, "b": 0.7, "c": 0.5, "d": 0.3, "e": 0.1},
    "m2": {"a": 0.8, "b": 0.2, "f": 0.6},
    "m3": {"g": 0.9, "h": 0.4},
    "m4": {"i": 0.3, "j": 0.1},
}


def _multi_samples():
    """四片夹具：正常片 / 评审失败片 / hits_in 空片 / θ 下相关集空片。

    - m1 正常：hits=(a,x,b,c)，x 交集外；评审 a=pass、x=pass、b=fail、
      c=漏判；新词 3 条（1 pass、1 漏判、1 fail）。
    - m2 评审失败（hit_pass/new_pass 整体 None）：hits=(b,a,y)，y 交集外，
      准确率照算、通过率/标定不进、命中标签计入 review_missing。
    - m3 hits_in 空（唯一命中 z 交集外）→ invalid 单列；评审成功，仍进
      通过率与新词占比。
    - m4 交集 {i:0.3, j:0.1} 全 <0.4：θ=0.4 下覆盖率分母为 0 → 不进该格；
      准确率照进（合法 0）。
    """
    return [
        OpenSample(
            label="m1",
            hit_ids=("a", "x", "b", "c"),
            hit_in_flags=(True, False, True, True),
            intersection=GT["m1"],
            hit_pass=(True, True, False, None),
            new_pass=(True, None, False),
        ),
        OpenSample(
            label="m2",
            hit_ids=("b", "a", "y"),
            hit_in_flags=(True, True, False),
            intersection=GT["m2"],
            hit_pass=None,
            new_pass=None,
        ),
        OpenSample(
            label="m3",
            hit_ids=("z",),
            hit_in_flags=(False,),
            intersection=GT["m3"],
            hit_pass=(True,),
            new_pass=(False,),
        ),
        OpenSample(
            label="m4",
            hit_ids=("i",),
            hit_in_flags=(True,),
            intersection=GT["m4"],
            hit_pass=(True,),
            new_pass=(),
        ),
    ]


# ---------------------------------------------------------------- 常量

def test_default_constants():
    assert DEFAULT_THETAS == (0.2, 0.4, 0.6, 0.8)
    assert DEFAULT_THETA_HEADLINE == 0.4


# ---------------------------------------------------------------- 单片手算例

def test_single_film_hand_case():
    # m1 单片：hits_in=(a,b,c) 产出序；交集 5 标签 {0.9,0.7,0.5,0.3,0.1}
    s = OpenSample(
        label="m1",
        hit_ids=("a", "x", "b", "c"),
        hit_in_flags=(True, False, True, True),
        intersection=GT["m1"],
        hit_pass=(True, True, False, None),
        new_pass=(True, None, False),
    )
    r = evaluate_open([s], GT)
    assert (r.n_total, r.n_ok, r.n_failed, r.n_review_failed,
            r.n_invalid) == (1, 1, 0, 0, 0)
    assert r.no_gt == 1  # 交集外命中 x 只计数
    assert r.review_missing == 2  # c 漏判 + 新词漏判 1 条
    # 准确率@θ：分子 = hit_in 中 gt≥θ 数，分母恒 3
    #   θ=0.2:3/3  θ=0.4:3/3  θ=0.6:2/3  θ=0.8:1/3
    assert [row.accuracy for row in r.theta_rows] == pytest.approx(
        [1.0, 1.0, 2 / 3, 1 / 3])
    # 随机基线 r/n（n=交集 5 标签）：r=4/3/2/1 → 0.8/0.6/0.4/0.2
    assert [row.accuracy_random for row in r.theta_rows] == pytest.approx(
        [0.8, 0.6, 0.4, 0.2])
    # 先验基线：交集按先验均值降序 = a(.85), c(.5), b(.45), d, e；取前 3 条
    # (a,c,b)，对应该片 gt (0.9, 0.5, 0.7)，precision@θ = 3/3/2/1 除以 3
    #   θ=0.6：a✓ b✓(0.7) c✗ → 2/3；θ=0.8：仅 a✓ → 1/3
    assert [row.accuracy_prior for row in r.theta_rows] == pytest.approx(
        [1.0, 1.0, 2 / 3, 1 / 3])
    # 覆盖率@θ：分母 = 交集内 gt≥θ 总数 4/3/2/1，分子 = 3/3/2/1
    assert [row.coverage for row in r.theta_rows] == pytest.approx(
        [0.75, 1.0, 1.0, 1.0])
    # 标定（仅 hit_in、漏判 c 排除）：条目 a(pass,0.9)、b(fail,0.7)
    #   θ=0.2/0.4/0.6：TP=1 FN=1 → 一致率 0.5、假阴 0.5
    #   θ=0.8：b(0.7)<0.8 变 TN → 一致率 1.0、假阳 0、假阴 0
    assert [row.n_calibrated for row in r.theta_rows] == [1, 1, 1, 1]
    assert [row.agreement for row in r.theta_rows] == pytest.approx(
        [0.5, 0.5, 0.5, 1.0])
    assert [row.fp_rate for row in r.theta_rows] == pytest.approx(
        [0.0, 0.0, 0.0, 0.0])
    assert [row.fn_rate for row in r.theta_rows] == pytest.approx(
        [0.5, 0.5, 0.5, 0.0])
    # nDCG：hits_in 恰按 gt 降序（全列表不截断）→ 1.0
    assert r.ndcg_macro == pytest.approx(1.0)
    # 新词通过率：非 None 2 条中 1 pass → 0.5；命中通过率：非 None 3 条中
    # 2 pass（含交集外 x）→ 2/3；新词占比 3/(4+3)=3/7
    assert (r.new_pass_macro, r.new_pass_micro, r.new_pass_n) == pytest.approx(
        (0.5, 0.5, 1))
    assert (r.hit_pass_macro, r.hit_pass_micro, r.hit_pass_n) == pytest.approx(
        (2 / 3, 2 / 3, 1))
    assert r.new_ratio_macro == pytest.approx(3 / 7)


# ---------------------------------------------------------------- 多片宏平均

def test_multi_film_macro_and_accounting():
    r = evaluate_open(_multi_samples(), GT)
    # 样本核算：total=4；ok=3（评审失败片 m2 保留在 ok）；invalid=1（m3）；
    # review_failed=1（m2）；produce 失败片不在样本合同内 → failed=0
    assert (r.n_total, r.n_ok, r.n_failed, r.n_review_failed,
            r.n_invalid) == (4, 3, 0, 1, 1)
    assert r.no_gt == 3  # x/y/z 三条交集外命中
    # review_missing：m1 漏判 2 条 + m2 整片失败命中标签 3 条 = 5
    assert r.review_missing == 5
    # nDCG：m1 全降序 1.0；m2 产出序 (b,a) 逆序→
    #   actual=0.2+0.8/log2(3)=0.70474，ideal=0.8+0.2/log2(3)=0.92619，
    #   ndcg=0.76091；m4 单条 1.0 → macro=(1+0.76091+1)/3
    assert r.ndcg_macro == pytest.approx(0.9203, abs=1e-3)
    # 通过率（评审失败片 m2 排除；漏判条目排除出分子分母）：
    #   新词：m1 1/2、m3 0/1 → macro=0.25、micro=1/3、n=2（m4 零新词不进）
    #   命中：m1 2/3、m3 1/1、m4 1/1 → macro=8/9、micro=4/5、n=3
    assert (r.new_pass_macro, r.new_pass_micro,
            r.new_pass_n) == pytest.approx((0.25, 1 / 3, 2))
    assert (r.hit_pass_macro, r.hit_pass_micro,
            r.hit_pass_n) == pytest.approx((8 / 9, 0.8, 3))
    # 新词占比：m1 3/7、m3 1/2、m4 0/1（m2 新词数不可知，排除）
    assert r.new_ratio_macro == pytest.approx((3 / 7 + 0.5 + 0.0) / 3)
    # n_valid = 交集内命中非空的片数（θ 无关，m1/m2/m4）：
    assert all(row.n_valid == 3 for row in r.theta_rows)
    # 逐 θ 宏平均（手算见各表达式）：
    #   θ=0.2：acc=1.0  random=(0.8+1+0.5)/3  prior=1.0
    #           cov=(0.75+2/3+1)/3  agr=(0.5+1)/2  fp=0  fn=0.25
    #   θ=0.4：acc=(1+0.5+0)/3  random=(0.6+2/3+0)/3  prior=(1+1+0)/3
    #           cov=(1+0.5)/2（m4 覆盖率分母 0，不进该格）agr=(0.5+0)/2
    #   θ=0.6：acc=(2/3+0.5+0)/3  random=(0.4+2/3+0)/3  prior=(2/3+1+0)/3
    #           （m1 先验 top3=(a,c,b) 对应 gt (0.9,0.5,0.7)：0.6 下 2 条≥θ）
    #           cov=0.75  agr=0.25  fn=(0.5+0)/2
    #   θ=0.8：acc=(1/3+0.5+0)/3  random=(0.2+1/3+0)/3  prior=(1/3+0.5+0)/3
    #           cov=1.0  agr=(1+0)/2  fp=0.5  fn=0
    acc = [1.0, 0.5, (2 / 3 + 0.5) / 3, (1 / 3 + 0.5) / 3]
    rand = [(0.8 + 1.0 + 0.5) / 3, (0.6 + 2 / 3) / 3, (0.4 + 2 / 3) / 3,
            (0.2 + 1 / 3) / 3]
    prior = [1.0, 2 / 3, (2 / 3 + 1.0) / 3, (1 / 3 + 0.5) / 3]
    cov = [(0.75 + 2 / 3 + 1.0) / 3, 0.75, 0.75, 1.0]
    agr = [0.75, 0.25, 0.25, 0.5]
    fp = [0.0, 0.5, 0.5, 0.5]
    fn = [0.25, 0.25, 0.25, 0.0]
    for i, row in enumerate(r.theta_rows):
        assert row.theta == pytest.approx(DEFAULT_THETAS[i])
        assert row.accuracy == pytest.approx(acc[i])
        assert row.accuracy_random == pytest.approx(rand[i])
        assert row.accuracy_prior == pytest.approx(prior[i])
        assert row.coverage == pytest.approx(cov[i])
        assert row.agreement == pytest.approx(agr[i])
        assert row.fp_rate == pytest.approx(fp[i])
        assert row.fn_rate == pytest.approx(fn[i])
        assert row.n_calibrated == 2  # m1 + m4（m2 评审失败、m3 无 hit_in）
    # 警示：invalid 单列 + 评审失败片列名
    assert any("m3" in w for w in r.warnings)
    assert any("m2" in w for w in r.warnings)


# ---------------------------------------------------------------- Review Focus 3

def test_out_of_intersection_hits_counted_only():
    # 交集外命中 zz：进命中通过率与 no_gt，不进准确率分母、不进标定
    s = OpenSample(label="m", hit_ids=("t0", "zz"), hit_in_flags=(True, False),
                   intersection={"t0": 0.9}, hit_pass=(True, True),
                   new_pass=())
    r = evaluate_open([s], {"m": {"t0": 0.9}}, thetas=(0.4,))
    row = r.theta_rows[0]
    assert row.accuracy == pytest.approx(1.0)  # 分母 1 而非 2
    assert r.no_gt == 1
    assert r.hit_pass_macro == pytest.approx(1.0)  # 分母含交集外命中
    assert row.n_calibrated == 1 and row.agreement == pytest.approx(1.0)
    assert r.new_pass_macro is None  # 零新词 → None（None≠0）
    assert r.new_ratio_macro == pytest.approx(0.0)  # 0 新词但总产出 2 → 0.0


# ---------------------------------------------------------------- θ 下相关集空

def test_theta_empty_relevant_set():
    # cold 片交集内无 gt≥0.4 标签：覆盖率不进该 0.4 格（分母 0），
    # 准确率照进（合法 0），n_valid 不变
    gt = {"hot": {"a": 0.9, "b": 0.8}, "cold": {"c": 0.3}}
    samples = [
        OpenSample(label="hot", hit_ids=("a",), hit_in_flags=(True,),
                   intersection=gt["hot"], hit_pass=(True,), new_pass=()),
        OpenSample(label="cold", hit_ids=("c",), hit_in_flags=(True,),
                   intersection=gt["cold"], hit_pass=(True,), new_pass=()),
    ]
    r = evaluate_open(samples, gt, thetas=(0.2, 0.4))
    assert all(row.n_valid == 2 for row in r.theta_rows)
    # 准确率@0.4：hot 1.0 + cold 0.0（合法 0，不剔除）
    assert r.theta_rows[1].accuracy == pytest.approx(0.5)
    # 覆盖率@0.4：cold 分母 0 → 不进格；只剩 hot（分母 2、分子 1）→ 0.5
    assert r.theta_rows[1].coverage == pytest.approx(0.5)
    # 覆盖率@0.2：hot 1/2、cold 1/1 → 0.75
    assert r.theta_rows[0].coverage == pytest.approx(0.75)
    # 随机@0.4：hot r/n=2/2、cold r=0 → 解析 0（参与，不剔除）→ 0.5
    assert r.theta_rows[1].accuracy_random == pytest.approx(0.5)
    # 先验@0.4：cold 取前 1 条 (c:0.3)<0.4 → 0.0
    assert r.theta_rows[1].accuracy_prior == pytest.approx(0.5)
    # 标定@0.4：hot TP、cold FP → 一致率 0.5、假阳 0.5
    assert r.theta_rows[1].agreement == pytest.approx(0.5)
    assert r.theta_rows[1].fp_rate == pytest.approx(0.5)
    assert r.theta_rows[1].fn_rate == pytest.approx(0.0)


# ---------------------------------------------------------------- 评审失败片

def test_review_failed_film_kept_in_ok_excluded_from_rates():
    s = OpenSample(label="m", hit_ids=("a", "b"), hit_in_flags=(True, True),
                   intersection={"a": 0.9, "b": 0.2}, hit_pass=None,
                   new_pass=None)
    r = evaluate_open([s], {"m": {"a": 0.9, "b": 0.2}})
    assert (r.n_total, r.n_ok, r.n_review_failed) == (1, 1, 1)  # 保留在 n_ok
    assert r.theta_rows[1].accuracy == pytest.approx(0.5)  # 准确率照算
    assert r.new_pass_macro is None and r.hit_pass_macro is None
    assert r.new_pass_n == 0 and r.hit_pass_n == 0
    assert r.theta_rows[1].n_calibrated == 0  # 不进标定
    assert r.theta_rows[1].agreement is None  # None≠0
    assert r.theta_rows[1].agreement_ci is None
    assert r.review_missing == 2  # 整片失败的命中标签计入 review_missing
    assert r.new_ratio_macro is None  # 新词数不可知 → 排除
    assert any("m" in w for w in r.warnings)


# ---------------------------------------------------------------- 漏判条目

def test_missing_verdict_entries_excluded():
    # hit_pass=(True, None)：b 漏判 → 命中通过率分母 1、标定只含 a、
    # review_missing 计 1；new_pass=(None, False, True)：新词分母 2
    s = OpenSample(label="m", hit_ids=("a", "b"), hit_in_flags=(True, True),
                   intersection={"a": 0.9, "b": 0.7}, hit_pass=(True, None),
                   new_pass=(None, False, True))
    r = evaluate_open([s], {"m": {"a": 0.9, "b": 0.7}}, thetas=(0.4,))
    assert r.hit_pass_macro == pytest.approx(1.0)  # 1 pass / 1 非 None
    assert r.new_pass_macro == pytest.approx(0.5)  # 1 pass / 2 非 None
    assert r.review_missing == 2
    row = r.theta_rows[0]
    assert row.n_calibrated == 1
    assert row.agreement == pytest.approx(1.0)  # 仅 a：TP=1/1
    assert r.new_ratio_macro == pytest.approx(3 / 5)  # 3 新词 / 5 总产出


# ---------------------------------------------------------------- CI / 空输入

def test_bootstrap_ci_keys_present():
    # 三部同样本 → CI 退化为 (值, 值)
    gt = {"m": {"a": 0.9}}
    samples = [
        OpenSample(label=f"s{i}", hit_ids=("a",), hit_in_flags=(True,),
                   intersection=gt["m"], hit_pass=(True,), new_pass=())
        for i in range(3)
    ]
    r = evaluate_open(samples, gt, thetas=(0.4,))
    row = r.theta_rows[0]
    assert row.accuracy_ci == pytest.approx((1.0, 1.0))
    assert row.agreement_ci == pytest.approx((1.0, 1.0))
    assert r.ndcg_ci == pytest.approx((1.0, 1.0))


def test_empty_samples_none_not_zero():
    r = evaluate_open([], {}, thetas=(0.4,))
    assert r.n_total == 0 and r.n_ok == 0 and r.n_invalid == 0
    assert r.no_gt == 0 and r.review_missing == 0  # 计数类如实为 0
    assert len(r.theta_rows) == 1
    row = r.theta_rows[0]
    assert (row.accuracy, row.accuracy_ci, row.accuracy_random,
            row.accuracy_prior, row.coverage) == (None, None, None, None, None)
    assert (row.agreement, row.agreement_ci, row.fp_rate,
            row.fn_rate) == (None, None, None, None)
    assert (r.ndcg_macro, r.ndcg_ci, r.new_pass_macro, r.hit_pass_macro,
            r.new_ratio_macro) == (None, None, None, None, None)
    assert r.warnings  # 没有样本可评
