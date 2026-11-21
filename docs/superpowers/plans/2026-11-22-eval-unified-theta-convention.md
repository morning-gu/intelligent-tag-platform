# 三处统一取格约定（P@10/nDCG@10→θ0.2 行，R@20→θ0.8 行）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans（用户已批准 Native 直执行）. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 评测历史、基线对照/汇总卡、单片明细三处统一取格约定：K=10 类指标（P/nDCG）取 θ≈0.2 行、R@20 取 θ≈0.8 行；取格规则统一为「最接近目标的有值格、并列取小、无 → None」；表头标注 θ。

**Architecture:** 约定单一事实源放 `closed_ranking.py`（常量），后端两读侧（movie_breakdown 缺省、closed_eval `_read_summary`）与前端 pickField 消费；历史 `_read_summary` 从「θ=0.4 精确匹配」升级为「最近有值格」（与前端同规则，custom-θ run 不再空列）。全部读侧，老 run 即刻生效。

**Tech Stack:** FastAPI + pydantic；React 19 + antd v6；pytest + vitest。

**Spec:** `docs/superpowers/specs/2026-11-21-eval-movie-badcase-view-design.md` 的「增补（2026-11-22 批准）：θ 可调」节 + 本计划头部的用户拍板（nDCG 跟 P 走 θ0.2；表头标 θ）。

## Global Constraints

- DB 零写入；全部读侧（report.json 网格 / raw_results 现算），落盘结构零改动。
- 取格规则（三处统一）：**最接近目标的有值格、并列取小（round 9 防浮点噪声）、无有值格 → None**。
- 目标值：`DEFAULT_THETA_P = 0.2`（P/nDCG@K=10）、`DEFAULT_THETA_R = 0.8`（R@K=20）；K 常量 `DEFAULT_K_P = 10`、`DEFAULT_K_R = 20` 一并收口。
- None≠0；coverage/Spearman 不受 θ 影响；report.md/metrics.csv 完整网格不动。
- UI 中文文案；表头标注：**汇总卡与单片明细表头标实际取格 θ**（回退时如实如 θ0.4）；**基线对照列头与历史表列头标约定值 θ0.2/θ0.8**（列级表头，三行/多行共用无法逐格标）。
- 测试命令同前（后端 `PYTHONPATH=src <venv>/Scripts/python.exe -m pytest`；前端 `cd frontend && npx vitest run`；`npx tsc -b`）。

## Review Focus

1. custom-θ run（thetas=[0.5]）历史列不再 None——最近有值格回退生效——Task 2 `test_history_summary_custom_thetas_fallback`。
2. θ0.2 行全 None（无有值格）→ 历史列 None 而非乱取——Task 2 `test_history_summary_theta_row_empty_falls_back_or_none`。
3. 缺省路径单片值与历史列/汇总卡同族：单片 p 均值（θ_p 缺省）= 网格 (0.2,10) 格 = history p10——Task 1 对账 + Task 2 取格一致性。
4. 老夹具在值域缝隙（0.2≤gt<0.4）的片缺省下 p 从 null 翻非 null——Task 1 各测试期望值重算（计划已给逐用例新值，照抄）。
5. 汇总卡/基线/历史表头 θ 字面量（约定值或实际取格值）——Task 3 断言。

---

### Task 1: 约定常量 + movie_breakdown 缺省拆目标

**Files:**
- Modify: `src/tagplatform/eval/closed_ranking.py`、`src/tagplatform/eval/movie_breakdown.py`
- Test: `tests/test_movie_breakdown.py`

**Interfaces:**
- Produces（Task 2/3 消费）: `closed_ranking.DEFAULT_THETA_P = 0.2`、`DEFAULT_THETA_R = 0.8`、`DEFAULT_K_P = 10`、`DEFAULT_K_R = 20`（`__all__` 导出；docstring 一段记统一取格约定）；`movie_breakdown.pick_theta(thetas, target)`（**签名变更**：加 target 参，无默认值，tie 规则不变 round 9）；`_THETA_TARGET`/`_TARGET_K_P`/`_TARGET_K_R` 模块内常量删除改 import；summarize/breakdown 缺省 `theta_p = pick_theta(thetas, DEFAULT_THETA_P)`、`theta_r = pick_theta(thetas, DEFAULT_THETA_R)`。

- [ ] **Step 1: 改测试**（夹具 `THETAS = [0.2, 0.4, 0.6]` → 缺省变为 **θ_p=0.2、θ_r=0.6**（0.6 是 0.8 的最近档）；`THETAS_SPLIT = [0.2,0.4,0.6,0.8]` → 缺省 **θ_p=0.2、θ_r=0.8**）。逐用例新期望值（已手算，照抄）：

- `test_pick_theta_and_k_tie_rules`：改为参数化形态——`pick_theta([0.2,0.6], 0.4)==0.2`、`pick_theta([0.2,0.4], 0.4)==0.4`、`pick_theta([0.2,0.4,0.6], 0.2)==0.2`、`pick_theta([0.2,0.4,0.6], 0.8)==0.6`；pick_k 断言原样。
- `test_hit_miss_noise_classification`：θ 断言 `(0.4,0.4)` → `(0.2,0.6)`；**其余值不变**（gt {gA:.81,gB:.77,gC:.61,gD:.1}：p@0.2 相关集 {gA,gB,gC} 同 0.4 → p=0.1；r@0.6 同集 → 1/3；n_relevant=3；detail hit gC(.61≥0.6)=True、misses=[gA,gB] 同）。
- `test_out_of_space_excluded_from_metrics_but_listed`：θ 断言 `(0.2,0.6)`，值全同（gt 全 0.9）。
- `test_theta_empty_relevant_nulls_pr_keeps_spearman`：**夹具改** cold gt `{"low1": 0.15, "low2": 0.05}`（0.15<0.2 才能保 p-null 语义；0.3 会在 θ0.2 门翻非 null）→ p/r null、spearman 1.0（xs=[0.9,0] ys=[0.15,0.05] 同向）、n_relevant=0、序 [m-hot, m-cold] 不变。
- `test_no_intersection_and_failed_rows_null`：θ 无关，仅 `n_relevant`（m-fail gt{f1:.9} @0.6 → 1）不变。
- `test_summary_sort_failed_first_p_asc_nulls_last`：**夹具改** n1 gt `{"c1": 0.15}`（原 0.3 在 θ0.2 门翻 p=0.1）→ n1 p null 垫底；b2/m1 p=0.2；序 [z9, a2, m1, s3] 不变。
- `test_produced_order_defensive_resort`：仅 θ 键断言改（值断言是 d1==d2/s1==s2 等式，不变）。
- `test_misses_name_join_missing_none`：值同（0.9/0.8 ≥0.6）。
- `test_reconciliation_with_grid`：**夹具改** mC gt `{"c1": 0.15}`（原 0.3 在 0.2 门非空、破坏两侧排除对齐）；网格侧取 `(theta==0.2, k==10)` 格对账（pvals=[0.1,0.2]、`g.n_valid==2`、`n_invalid==1` 断言不变——0.15<0.2 与原 0.3<0.4 同构）。
- `test_default_split_equals_single_theta_behavior`（THETAS_SPLIT）：θ 断言 `(0.2,0.8)`；**值重算**——p@0.2=0.1（同）、r@0.8：relevant {gA:.81}、top20=[gC,gD] 无命中 → **0.0**（原 1/3）；`n_relevant==1`；detail：produced hit `[False, False]`（gC .61、gD .1 皆 <0.8）、misses==[(gA,0.81)]、gt_score 透传不变。
- 显式参测试（classification/reconciliation_split/null_rules_split）：**不动**（显式 θ 不受缺省影响）。

- [ ] **Step 2: 跑 RED**

Run: `PYTHONPATH=src <venv> -m pytest tests/test_movie_breakdown.py -v`
Expected: FAIL——`pick_theta() missing 1 required positional argument` + 缺省 θ 断言失配

- [ ] **Step 3: 实现**

- closed_ranking.py：四常量 + `__all__` + docstring「统一取格约定」段（三处消费方点名）。
- movie_breakdown.py：import 四常量；`pick_theta(thetas, target)` 签名与 docstring 更新；`_THETA_TARGET`/`_TARGET_K_P`/`_TARGET_K_R` 删除；summarize/breakdown 缺省两行改；模块 docstring 增补一句。

- [ ] **Step 4: 跑 GREEN（文件级）**

Run: 同 Step 2
Expected: PASS（13 全绿）

- [ ] **Step 5: Commit**

```bash
git add src/tagplatform/eval/closed_ranking.py src/tagplatform/eval/movie_breakdown.py tests/test_movie_breakdown.py
git commit -m "feat: 取格约定常量收口 closed_ranking + 单片缺省 P→θ0.2/R→θ0.8"
```

---

### Task 2: 历史 `_read_summary` 换约定（最近有值格）

**Files:**
- Modify: `src/tagplatform/closed_eval.py`（`_read_summary`）、`src/tagplatform/api/routers/eval.py`（RunSummaryOut docstring）
- Test: `tests/test_closed_eval.py`（history 摘要用例）、`tests/test_api_eval.py`（movie 端点缺省断言 + RunSummary 文档核对）

**Interfaces:**
- Consumes: Task 1 的四常量。
- Produces: `_read_summary` 取格：p10/ndcg10 = 最近 θ0.2 的**有值**格 @K=10；r20 = 最近 θ0.8 有值格 @K=20（并列取小 round 9；无有值格 → None）；custom-θ run 回退最近有值格。

- [ ] **Step 1: 改测试**

- `tests/test_closed_eval.py`：定位现有 history/摘要四键用例（`_read_summary` 或 history() 断言 p10/r20/ndcg10 来自 θ0.4 格），夹具网格改含 θ0.2/θ0.8 行、断言换格；新增两条：
  - `test_history_summary_custom_thetas_fallback`：网格仅 θ=0.5 行（有值）→ p10/ndcg10/r20 取 0.5 格值（不 None）。
  - `test_history_summary_theta_row_empty_falls_back_or_none`：θ0.2 行 K=10 两格 precision/ndcg 为 null、θ0.4 行有值 → p10/ndcg10 回退 0.4 格；全行无值 → None。
- `tests/test_api_eval.py`：
  - `test_run_movies_endpoint`：θ 键断言 `(0.4,0.4)` → `(0.2,0.6)`（meta thetas [0.2,0.4,0.6]）；**行值不变**（夹具 A 的 p@0.2、r@0.6、n_relevant、detail hit [T,T,F]、misses {t7} 均与 0.4 门同——已手算核验，gD/t9=0.1<0.2、t2=0.8≥0.6、t7=0.85≥0.6）。
  - `test_run_movie_detail_endpoint` / `d2` 段：θ 键断言 `(0.2,0.6)`；hit/misses 值不变（t1 .9≥0.6、t2 .8≥0.6、t9 .1<0.6）。
  - `test_movies_default_grid_when_meta_missing`：DEFAULT 网格四档 → `(0.2,0.8)`；ks-only 用例同。
  - `test_movies_theta_query_validation`：default 段 `(0.4,0.4)` → `(0.2,0.8)`；`drow["p_at_k"]` approx 0.1 → **0.2**（θ_p=0.2：t1(.3)+t2(.7) 相关 2 命中 /10）；r 0.5 → **0.0**（θ_r=0.8：分母只 t7，未产出）；n_relevant 2 → **1**。split 段（显式 0.2/0.8）不变。
  - RunSummaryOut/RunSummary 相关文档断言（如有「θ=0.4」字样）同步。

- [ ] **Step 2: 跑 RED**

Run: `PYTHONPATH=src <venv> -m pytest tests/test_closed_eval.py tests/test_api_eval.py -v -k "history or summary or movies or movie"`
Expected: FAIL——缺省 θ 键失配 + custom-θ 回退用例（新行为未实现 → None 或旧值）

- [ ] **Step 3: 实现**

- `_read_summary` 重写取格：内嵌 helper `_pick_valued_cell(grid, theta_target, k, field)`（valued 判定 = 字段值为 int/float；`min` key `(round(abs(θ-target),9), θ)`）；p10/ndcg10 用 `DEFAULT_THETA_P`、r20 用 `DEFAULT_THETA_R`、K 用 `DEFAULT_K_P/K_R`（import from closed_ranking）；docstring 更新（统一规则 + 回退语义）。
- `eval.py` RunSummaryOut docstring：θ0.2/θ0.8 + 最近有值格口径一句。

- [ ] **Step 4: 跑 GREEN（两文件级）**

Run: 同 Step 2 范围
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/tagplatform/closed_eval.py src/tagplatform/api/routers/eval.py tests/test_closed_eval.py tests/test_api_eval.py
git commit -m "feat: 历史摘要四键换统一取格约定（θ0.2/θ0.8 最近有值格，custom-θ 回退）"
```

---

### Task 3: 前端取格目标 + 表头标 θ

**Files:**
- Modify: `frontend/src/components/ClosedEvalReport.tsx`（pickField 目标参 + 实际 θ 标注）、`frontend/src/pages/ClosedEval.tsx`（历史列头）、`frontend/src/api/eval.ts`（RunSummary 文档）
- Test: `frontend/src/components/ClosedEvalReport.test.tsx`、`frontend/src/components/ClosedEvalMovieTable.test.tsx`、`frontend/src/pages/ClosedEval.test.tsx`

**Interfaces:**
- Consumes: 约定值 0.2/0.8（TS 侧常量 `THETA_P_TARGET = 0.2` / `THETA_R_TARGET = 0.8`，注释指向后端 closed_ranking 单一事实源）。
- Produces: `pickField(rows, k, field, thetaTarget)`；新 helper `pickTheta(rows, k, thetaTarget): number | null`（返回实际取格 θ，无有值格 → null）；汇总卡题 `P@10(θ{实际})`（null → 标约定值字面量）；基线列头/历史列头标约定 `P@10(θ0.2)`/`R@20(θ0.8)`/`nDCG@10(θ0.2)`。

- [ ] **Step 1: 改测试**

- `ClosedEvalReport.test.tsx`：夹具 grid 增/改 θ 行使 0.2/0.8 档有值（原夹具 θ0.4/0.6 两行 → 需覆盖 0.2 与 0.8）；汇总卡与基线断言按新取格重算 + 卡题/列头字面量断言（`P@10(θ0.2)` 式）；一条回退用例（θ0.2 行 null → 卡题标实际 θ0.4）。
- `ClosedEvalMovieTable.test.tsx`：SUMMARY/DETAIL 夹具 `theta_p/theta_r` 0.4 → **0.2/0.8**；表头断言 `P@8(θ0.4)` → `P@8(θ0.2)`、`R@20(θ0.4)` → `R@20(θ0.8)`；θ 选择器初值断言 0.4 → 0.2/0.8（两处 textContent）；run-switch 重置断言同；显式切换用例不动。
- `ClosedEval.test.tsx`：历史表列头断言 `P@10` → `P@10(θ0.2)`、`R@20` → `R@20(θ0.8)`（ndcg 列如显示则 `nDCG@10(θ0.2)`）。

- [ ] **Step 2: 跑 RED**

Run: `cd frontend && npx vitest run src/components/ClosedEvalReport.test.tsx src/components/ClosedEvalMovieTable.test.tsx src/pages/ClosedEval.test.tsx`
Expected: FAIL——表头字面量/取格值失配

- [ ] **Step 3: 实现**

- `ClosedEvalReport.tsx`：`THETA_P_TARGET/THETA_R_TARGET` 常量；`pickField` 加 target 参（现有「同 K 有值格中 θ 最接近」逻辑的 target 换参）；ndcg10/p10 用 0.2、r20 用 0.8；`pickTheta` helper 取实际格 θ；卡题拼 `P@10(θ{θ ?? '0.2'})`（实际优先、无格回退约定字面量）；基线列头拼约定字面量；文件头注释更新。
- `ClosedEval.tsx`：历史表 columns 的 P@10/R@20（及 ndcg 若有）标题加 `(θ0.2)`/`(θ0.8)` 字面量。
- `api/eval.ts`：RunSummary doc 注释改（θ0.2/θ0.8、最近有值格回退）。

- [ ] **Step 4: 全套验证**

Run: `cd frontend && npx vitest run && npx tsc -b`
Expected: 前端全绿 + tsc 零错
Run: `PYTHONPATH=src <venv> -m pytest`
Expected: 后端全绿

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/ClosedEvalReport.tsx frontend/src/components/ClosedEvalMovieTable.test.tsx frontend/src/pages/ClosedEval.tsx frontend/src/components/ClosedEvalReport.test.tsx frontend/src/api/eval.ts frontend/src/pages/ClosedEval.test.tsx
git commit -m "feat: 前端三处统一取格（P/nDCG→θ0.2、R→θ0.8）+ 表头标 θ"
```
