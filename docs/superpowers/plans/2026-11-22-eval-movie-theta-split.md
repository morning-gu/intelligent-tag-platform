# 单片明细 θ 可调（P/R 各一门）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans 或 subagent-driven-development（执行方式待用户确认；建议 Native——3 任务小改动）. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 单片明细的 P@K 与 R@K 支持各自独立选 θ（页面两个选择器，缺省=各取最接近 0.4），漏检/命中分类跟 θ_r。

**Architecture:** 纯读侧参数化增量：`movie_breakdown` 的 summarize/breakdown 增加 `theta_p`/`theta_r` 关键字参数（None=沿用选档规则）；router 两端点加可选 query 参数 + 语义校验；前端组件加两个 θ 选择器（切换即重拉+清缓存+收起）。

**Tech Stack:** FastAPI + pydantic；React 19 + antd v6；pytest + vitest。

**Spec:** `docs/superpowers/specs/2026-11-21-eval-movie-badcase-view-design.md` 的「增补（2026-11-22 批准）：θ 可调」节（对账性质、null 规则、分类归属、非目标均已钉死）。

## Global Constraints

- DB 零写入；只读 eval_output_root 既有产物。
- 两端点保持 admin-only；现有行为（缺省参数）**逐位不变**——θ_p=θ_r=最近 0.4 时的输出与现版本一致（回归测试钉死）。
- 指标继续复用 `precision_at_k` / `recall_at_k`（`relevant_grade` 传各 θ）/ `spearman`（θ 无关），零公式重写。
- None≠0：p null ⟺ θ_p 相关集空；r null ⟺ θ_r 相关集空（各对齐各 θ 的网格排除规则）。
- 非法 θ（∉ run thetas）→ 400 带合法值清单，不 500。
- `n_relevant`、明细 hit 标记、misses 一律跟 **θ_r**；Spearman 不受 θ 影响；K 不可调。
- UI 中文文案；测试命令同前一计划（后端 `PYTHONPATH=src C:/code/research-topics/movie-tag-research/intelligent-tag-platform/.venv/Scripts/python.exe -m pytest`；前端 `cd frontend && npx vitest run`；`npx tsc -b`）。

## Review Focus

1. θ_p=θ_r=缺省档时输出与现版本**完全一致**（含排序与明细三组）——Task 1 `test_default_split_equals_single_theta_behavior`（对拍新旧两调用路径）。
2. θ_r 切换后前端明细缓存陈旧（hit/漏检是旧 θ_r 的）——Task 3 `test_theta_change_refetches_and_clears_cache`（断言重拉摘要 + 收起 + 重新展开才取数）。
3. 非法 θ 值（∉ thetas，含近似浮点 0.3）→ 400 detail 含合法值清单——Task 2 `test_movies_theta_query_validation`。
4. split-θ 对账：非空 p 均值 = 网格 (θ_p, K_P) 格、非空 r 均值 = (θ_r, K_R) 格（θ_p≠θ_r）——Task 1 `test_reconciliation_split_theta`。
5. n_relevant 与 misses/hit 均按 θ_r、p_at_k 按 θ_p 的口径混排正确（夹具里两 θ 相关集不同）——Task 1 `test_split_theta_classification_follows_theta_r`。

---

### Task 1: movie_breakdown 拆 θ

**Files:**
- Modify: `src/tagplatform/eval/movie_breakdown.py`
- Test: `tests/test_movie_breakdown.py`（追加）

**Interfaces:**
- Consumes: 现有 `summarize_movies(rows, thetas, ks, library_ids)` / `breakdown_movie(rows, tt, thetas, ks, library_ids, library_names)` 及内部 `_movie_metrics`。
- Produces（Task 2 消费）:
  - `summarize_movies(rows, thetas, ks, library_ids, *, theta_p: float | None = None, theta_r: float | None = None) -> dict` → `{"theta_p": float, "theta_r": float, "k_p": int, "k_r": int, "thetas": list[float], "rows": [...]}`（`theta_used` 键移除；`thetas` = 入参 list(thetas)；None → 各自 `pick_theta`；摘要行字段不变，`n_relevant` 按 θ_r 计）。
  - `breakdown_movie(rows, tt, thetas, ks, library_ids, library_names, *, theta_p=None, theta_r=None) -> dict | None` → 明细 `theta_used` 拆为 `theta_p`/`theta_r`（其余键不变；hit 与 misses 按 θ_r；produced 顺序、out_of_space 不受 θ 影响）。

- [ ] **Step 1: 写失败测试**（追加用例；夹具 thetas=[0.2,0.4,0.6,0.8]、ks=[5,10,20,40]）：

- `test_default_split_equals_single_theta_behavior`：同夹具调 `summarize_movies(rows, ...)`（无新参）与旧口径对照——payload 的 `theta_p`/`theta_r` 均 0.4、`thetas` 四档；`breakdown_movie` 同理 `theta_p`/`theta_r` 0.4 且三组与现行为逐字段相等（以现有测试的期望值为基准）。
- `test_split_theta_classification_follows_theta_r`：夹具某片 gt 含 0.25（≥0.2 <0.4）、0.9（≥0.8）标签：`theta_p=0.2, theta_r=0.8` → `p_at_k` 按 0.2 门槛算（0.25 条算 hit 进分子）、`r_at_k` 按 0.8 算（分母只含 0.9 条）、`n_relevant` 只数 ≥0.8、misses 只含 0.9 条未产出、produced 行 `hit` 只对 ≥0.8 为真（0.25 行 hit=False 但仍入 produced 清单）。
- `test_reconciliation_split_theta`：`theta_p=0.2, theta_r=0.8`，非空 p 均值 == `evaluate_closed` 网格 (0.2,10) 格 precision、非空 r 均值 == (0.8,20) 格 recall（样本构造同前一对账测试；含只在某一 θ 下非空的片验证 null 排除集对齐）。
- `test_split_theta_null_rules_independent`：某片 gt∩库 只在 0.2 门非空（全 <0.8）→ `theta_p=0.2,theta_r=0.8` 时 p 非 null、r null、spearman 照算；排序垫底键只看 p。

- [ ] **Step 2: 跑测试验证 RED**

Run: `PYTHONPATH=src C:/code/research-topics/movie-tag-research/intelligent-tag-platform/.venv/Scripts/python.exe -m pytest tests/test_movie_breakdown.py -v -k "split or default_split"`
Expected: FAIL —— `TypeError: summarize_movies() got an unexpected keyword argument 'theta_p'`

- [ ] **Step 3: 实现拆 θ**

- `_movie_metrics` 拆双 θ：`relevant_p` / `relevant_r` 各自判空（p null ⟺ θ_p 空、r null ⟺ θ_r 空）；`precision_at_k(..., relevant_grade=theta_p)`、`recall_at_k(..., relevant_grade=theta_r)`；spearman 不变。
- summarize/breakdown 签名加关键字参；`theta_p`/`theta_r` 键替换 `theta_used`；payload 增 `thetas`；`n_relevant`/misses/hit 判定统一用 θ_r。
- 现有调用（无参）经默认路径行为不变——跑全部现有用例验证。

- [ ] **Step 4: 跑测试验证 GREEN（含文件级回归）**

Run: `PYTHONPATH=src C:/code/research-topics/movie-tag-research/intelligent-tag-platform/.venv/Scripts/python.exe -m pytest tests/test_movie_breakdown.py -v`
Expected: PASS（9 存量 + 4 新增全绿——存量用例中 theta_used 断言同步改 theta_p/theta_r）

- [ ] **Step 5: Commit**

```bash
git add src/tagplatform/eval/movie_breakdown.py tests/test_movie_breakdown.py
git commit -m "feat: 单片指标 P/R 拆 θ（缺省=最近 0.4，分类跟 θ_r）"
```

---

### Task 2: router query 参数与校验

**Files:**
- Modify: `src/tagplatform/api/routers/eval.py`（两端点 + 模型字段）
- Test: `tests/test_api_eval.py`（追加）

**Interfaces:**
- Consumes: Task 1 新签名；`_movies_read`（现有读侧）；fastapi `Query`。
- Produces（Task 3 消费）:
  - `GET /runs/{run_id}/movies?theta_p=&theta_r=` → `MovieSummaryOut`（`theta_p`/`theta_r`/`k_p`/`k_r`/`thetas: list[float]`/`rows`）
  - `GET /runs/{run_id}/movies/{tt}?theta_p=&theta_r=` → `MovieDetailOut`（`theta_p`/`theta_r` 替换 `theta_used`）
  - 非法 θ → `HTTPException(400, detail="theta_p 需为该 run 网格档之一：[0.2, 0.4, 0.6, 0.8]")` 式文案

- [ ] **Step 1: 写失败测试**（追加）：

- `test_movies_theta_query_validation`：`theta_p=0.3` → 400 且 detail 含合法值；`theta_p=0.2&theta_r=0.8` → 200 且响应 `theta_p==0.2`、`theta_r==0.8`、`thetas==[0.2,0.4,0.6,0.8]`、行 `p_at_k` 与夹具手算（0.2 门）一致、`n_relevant` 按 0.8 门计；无参 → `theta_p==theta_r==0.4`（缺省回归）。
- `test_movie_detail_theta_query`：detail 带 `theta_r=0.8` → hit/misses 按 0.8 门；`theta_p` 只回显。
- `test_movies_theta_non_admin_401`：带参非 admin → 401。

- [ ] **Step 2: 跑测试验证 RED**

Run: `PYTHONPATH=src C:/code/research-topics/movie-tag-research/intelligent-tag-platform/.venv/Scripts/python.exe -m pytest tests/test_api_eval.py -v -k theta`
Expected: FAIL —— query 参数被忽略（响应 theta_p 仍 0.4 或键缺失断言失败）

- [ ] **Step 3: 实现端点参数**

- 两端点签名加 `theta_p: float | None = Query(None)` / `theta_r: float | None = Query(None)`；在 `_movies_read` 拿到 thetas 后校验：非 None 且（float 相等比较）∉ thetas → 400 带合法清单；透传给 summarize/breakdown。
- 模型字段：`MovieSummaryOut` 增 `thetas: list[float]`、`theta_used` → `theta_p`/`theta_r`；`MovieDetailOut` 同替换。模块 docstring 与 `_movies_read` docstring 增 θ 可调一句。

- [ ] **Step 4: 跑测试验证 GREEN（文件级回归）**

Run: `PYTHONPATH=src C:/code/research-topics/movie-tag-research/intelligent-tag-platform/.venv/Scripts/python.exe -m pytest tests/test_api_eval.py -v`
Expected: PASS（存量 movie 端点用例的 theta_used 断言同步改）

- [ ] **Step 5: Commit**

```bash
git add src/tagplatform/api/routers/eval.py tests/test_api_eval.py
git commit -m "feat: 单片两端点 theta_p/theta_r query 参数（校验 400+缺省回归）"
```

---

### Task 3: 前端 θ 选择器

**Files:**
- Modify: `frontend/src/api/eval.ts`、`frontend/src/components/ClosedEvalMovieTable.tsx`
- Test: `frontend/src/components/ClosedEvalMovieTable.test.tsx`（追加）

**Interfaces:**
- Consumes: Task 2 响应形状；`apiFetch`。
- Produces: `getRunMovies(runId: string, theta?: { p?: number; r?: number })` / `getRunMovieDetail(runId: string, tt: string, theta?: { p?: number; r?: number })`（有值时拼 `?theta_p=&theta_r=`）；类型 `MovieSummaryPayload { theta_p; theta_r; k_p; k_r; thetas: number[]; rows }`、`MovieDetail { theta_p; theta_r; ... }`；组件头部两 Select（θ_P/θ_R，options = `payload.thetas`，受控值初始 = 服务端回传值）。

**行为**：切换任一 θ → 重拉摘要（带参）+ **清空明细缓存 + 收起全部展开行**（`setExpandedRowKeys([])`）；表头字面量 `P@{k_p}(θ{theta_p})`、`R@{k_r}(θ{theta_r})`；选择器选项来自当前 payload 的 `thetas`（换 run 后自动更新）。

- [ ] **Step 1: 写失败测试**（追加）：

- `test_theta_change_refetches_and_clears_cache`：首载（无参，mock 回 0.4/0.4）→ 展开一行取明细 → 改 θ_P 选择器到 0.2（fireEvent.change）→ `getRunMovies` 第二次调用带 `theta_p=0.2`、`getRunMovieDetail` 仅首展开 1 次（未自动重拉）、展开行已收起 → 再展开同一行 → 第二次带 `theta_p=0.2` 明细调用。
- `test_theta_selectors_render_from_payload`：两个 Select 渲染 `thetas` 四选项、初值 0.4；表头字面量 `P@10(θ0.2)`（k_p=10、切换后 payload 回 0.2 的夹具）。
- `test_run_switch_resets_theta_state`：换 runId 重挂载后 θ 回服务端默认（不残留上个 run 的选择）。

- [ ] **Step 2: 跑测试验证 RED**

Run: `cd frontend && npx vitest run src/components/ClosedEvalMovieTable.test.tsx`
Expected: FAIL —— Select 不存在（queryByRole 找不到 combobox）

- [ ] **Step 3: 实现选择器与联动**

- `eval.ts`：两函数加可选 `theta` 对象参拼 query；类型更新（`theta_used` → `theta_p`/`theta_r`；payload 增 `thetas`）。
- 组件：`thetaP`/`thetaR` state（初值来自首载 payload，换 run 重置）；头部两 `Select size="small" style={{width:90}}`（受控）；`onChange` → set 状态 → `loadSummary(带参)` + `setDetails(new Map())` + `setExpandedRowKeys([])`；表头拼门字面量；明细懒加载调用带当前 θ。
- 明细缓存键维持 `runId:tt`（θ 切换整表清空，无需入键）。

- [ ] **Step 4: 跑测试验证 GREEN + 全套**

Run: `cd frontend && npx vitest run && npx tsc -b`
Expected: 前端全绿（存量 movie 表用例的表头断言同步改 `P@10(θ0.4)` 式）；tsc 零错。
Run: `PYTHONPATH=src C:/code/research-topics/movie-tag-research/intelligent-tag-platform/.venv/Scripts/python.exe -m pytest`
Expected: 后端全绿

- [ ] **Step 5: Commit**

```bash
git add frontend/src/api/eval.ts frontend/src/components/ClosedEvalMovieTable.tsx frontend/src/components/ClosedEvalMovieTable.test.tsx
git commit -m "feat: 单片明细 θ_P/θ_R 选择器（切换重拉+清缓存+收起）"
```
