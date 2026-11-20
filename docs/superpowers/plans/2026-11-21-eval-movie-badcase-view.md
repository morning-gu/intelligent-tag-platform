# 闭环评测单片明细（badcase 视图）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development（用户已选 sub-driven：每任务全新实现者 + 全新评审，末尾整分支评审）. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 终态 run 的单片 badcase 视图——报告页新增「单片明细」表（失败置顶、P@K 升序的坏例优先），展开看「计分清单 vs GT / 空间外 / 漏检」对照。

**Architecture:** 纯读侧增量。读时现算：`movie_breakdown` 纯函数模块从 `raw_results.json` 行算单片指标（复用 ranking/closed_ranking 原语保对账口径）；薄 router 两端点读盘组装；前端新组件懒加载展开。落盘结构零改动，历史 run 即时可用。

**Tech Stack:** FastAPI + pydantic（后端）；React 19 + antd v6 + vitest（前端）；pytest（后端）。

**Spec:** `docs/superpowers/specs/2026-11-21-eval-movie-badcase-view-design.md`（已批准；口径钉死：对账口径/计分清单/null 规则/包层响应）

## Global Constraints

- DB 零写入红线：只读 `eval_output_root` 下既有产物。
- 新端点全部 admin-only（`Depends(require_admin)`）；现有 11 端点不动。
- None≠0：指标 null → 前端「暂无」；θ-空相关集/无交集/失败片按 spec null 规则。
- **对账钉子：单片指标必须复用 `tagplatform.eval.ranking` 的 `precision_at_k` / `recall_at_k` 与 `tagplatform.eval.closed_ranking` 的 `spearman`，不得重写公式**。
- `movie_breakdown` 只用标准库 + `tagplatform.eval.ranking` / `closed_ranking` import（同 closed_ranking 分层红线，不 import fastapi/numpy）。
- run_id 守卫复用 router 现有 `_run_dir`；tt 只做 JSON 行内查找，不参与路径拼接。
- UI 中文文案。
- 测试命令（worktree 根目录）：
  - 后端单文件：`PYTHONPATH=src C:/code/research-topics/movie-tag-research/intelligent-tag-platform/.venv/Scripts/python.exe -m pytest tests/test_movie_breakdown.py -v`
  - 后端全量：同上去掉文件参数
  - 前端：`cd frontend && npx vitest run`（单文件加路径）
  - TS 检查：`cd frontend && npx tsc -b`

## Review Focus

1. 空间外产出占满 top-K 的片：P@K 不被噪声拉低、`out_of_space` 全量可见——Task 1 `test_out_of_space_excluded_from_metrics_but_listed`。
2. 有交集但 θ 下相关集空的片：P/R null 垫底（不置顶淹没真坏例）、Spearman 照算——Task 1 `test_theta_empty_relevant_nulls_pr_keeps_spearman`。
3. `raw_results.json` 缺失/截断（运行中 run、写盘非原子窗口）：404「该 run 无单片明细」而非 500——Task 2 `test_movies_404_when_raw_results_missing_or_truncated`。
4. `meta.json` 缺 thetas/ks 或损坏（老 run）：回落 DEFAULT 档位照常出数——Task 2 `test_movies_default_grid_when_meta_missing`。
5. produced 顺序不可信（人工编辑过的产物）：防御性按 (-sf, tag_id) 再排，指标与展示序不变——Task 1 `test_produced_order_defensive_resort`。

---

### Task 1: movie_breakdown 纯函数模块

**Files:**
- Create: `src/tagplatform/eval/movie_breakdown.py`
- Test: `tests/test_movie_breakdown.py`

**Interfaces:**
- Consumes: `precision_at_k(ranked, relevance, k, *, relevant_grade)`、`recall_at_k(...)`（`tagplatform.eval.ranking`；**对空相关集记 0.0**——null 规则必须由本模块先判空再调）；`spearman(xs, ys) -> float | None`（`tagplatform.eval.closed_ranking`）；raw 行形状（`raw_results.json` 行）：`{"tt": str, "title": str, "ok": bool, "error": str | None, "produced": [{"tag_id", "tag_name", "sf", "confidence", "evidence_count"}], "gt": {tag_id: float}, "elapsed_ms": int}`。
- Produces（Task 2 消费）:
  - `pick_theta(thetas: Sequence[float]) -> float` —— 最接近 0.4，并列取小
  - `pick_k(ks: Sequence[int], target: int) -> int` —— 最接近 target，并列取小
  - `summarize_movies(rows: list[dict], thetas: Sequence[float], ks: Sequence[int], library_ids: set[str]) -> dict` → `{"theta_used": float, "k_p": int, "k_r": int, "rows": [摘要行]}`；摘要行 `{"tt", "title", "ok", "error", "spearman", "p_at_k", "r_at_k", "elapsed_ms", "n_produced", "n_relevant"}`（指标 `float | None`；`n_produced` = len(produced) 原始条数含空间外；`n_relevant` = |gt∩库且 ≥θ| 计数）；排序：失败置顶 → P 非空按 `p_at_k` 升序 → null 垫底 → 同分按 tt。
  - `breakdown_movie(rows: list[dict], tt: str, thetas: Sequence[float], ks: Sequence[int], library_ids: set[str], library_names: dict[str, str]) -> dict | None` → 明细 `{"tt", "title", "ok", "error", "elapsed_ms", "theta_used", "k_p", "k_r", "produced", "out_of_space", "misses"}`；`produced` = 计分清单（GT 空间 = **各行 gt 键的并集**；防御性按 (-sf, tag_id) 排），每条 `{"tag_id", "tag_name", "sf", "confidence", "evidence_count", "gt_score", "hit"}`（`gt_score` = 该片 gt 原分、不在该片 gt → None；`hit` = gt_score 非None 且 ≥theta_used）；`out_of_space` 每条 `{"tag_id", "tag_name", "sf"}`；`misses` = relevant(gt∩库 ≥θ) − 全部产出 tag_id 集，按 (-gt_score, tag_id) 排，每条 `{"tag_id", "tag_name", "gt_score"}`（tag_name = library_names.get，缺 → None）。失败片：三表全空、error 原样。未知 tt → None。

- [ ] **Step 1: 写失败测试** `tests/test_movie_breakdown.py`

测试用例（夹具：手造 raw 行 dict；thetas=[0.2,0.4,0.6]、ks=[5,10,20] → theta_used=0.4、k_p=10、k_r=20）：

- `test_pick_theta_and_k_tie_rules`：`pick_theta([0.2,0.6])==0.2`（并列取小）、`pick_theta([0.2,0.4])==0.4`、`pick_k([5,20],10)==5`、`pick_k([15,5],10)==5`、`pick_k([15],10)==15`。
- `test_hit_miss_noise_classification`：一部片 produced 3 条（sf 序：hit 条 gt=0.61 / 低分条 gt=0.1 / 空间外条），gt 另含 2 个 ≥θ 未产出标签（0.81、0.77）→ `breakdown_movie` 返回：produced[0].hit is True、produced[1].hit is False 且 gt_score==0.1 原样、out_of_space 1 条带 sf、misses 2 条按 gt 降序（0.81 在前）；p_at_k/r_at_k/spearman 按 fixture 手算值断言（例：top-K 只含前 2 条计分条时 p=命中数/10）。
- `test_out_of_space_excluded_from_metrics_but_listed`：空间外条目 sf 最高排第一——P@K 只数计分清单 top-K，空间外不占坑；out_of_space 含该条。
- `test_theta_empty_relevant_nulls_pr_keeps_spearman`：gt∩库 非空但全 <θ → p_at_k/r_at_k is None、spearman 非 None；该行在 summarize 排序中位于非空 P 行之后（垫底）。
- `test_no_intersection_and_failed_rows_null`：gt 空 dict → spearman/p/r 全 None；ok=False 行 → 全 None + error 透传 + 三空表；n_relevant 仍按 gt∩库 ≥θ 计数。
- `test_summary_sort_failed_first_p_asc_nulls_last`：三片（失败、p=0.2、p=None）→ rows 顺序 [失败, p=0.2, p=None]；两片同 p → tt 字典序。
- `test_produced_order_defensive_resort`：同一片 produced 逆序传入 → 指标与 breakdown.produced 顺序不变。
- `test_misses_name_join_missing_none`：library_names 缺该 tag → tag_name None；含 → 值。
- `test_reconciliation_with_grid`：夹具使 library_ids ⊇ 全部 gt/produced 标签；双路过算——`summarize_movies` 的非空 p_at_k 均值 == `evaluate_closed(samples, thetas, ks).grid` 中 (theta=0.4, k=10) 格 `precision`（样本构造与 summarize 同法：`ranked`=计分清单 tuple、`relevance`=gt∩库、`system_scores`={tag_id: sf}）。θ-空/无交集片两侧同被排除（网格 valid_theta 预滤 ↔ 单片 null），均值应精确相等。

- [ ] **Step 2: 跑测试验证 RED**

Run: `PYTHONPATH=src C:/code/research-topics/movie-tag-research/intelligent-tag-platform/.venv/Scripts/python.exe -m pytest tests/test_movie_breakdown.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'tagplatform.eval.movie_breakdown'`

- [ ] **Step 3: 实现 `src/tagplatform/eval/movie_breakdown.py`**

实现要点（签名见 Interfaces；模块 docstring 记对账口径与已知取舍两行）：

- 内部 `_gt_space(rows) -> set[str]`（各行 gt 键并集）、`_relevance(row, library_ids) -> dict[str, float]`（gt∩库）。
- 单片指标：`ok` 假或 relevance 空 → 全 None；否则 `relevant_theta = {t for t, s in relevance.items() if s >= theta}` **先判空**——空则 p/r=None（precision/recall 原语对空集记 0.0，网格靠 valid_theta 预滤，单片自管 null），非空才调 `precision_at_k(scoring_tuple, relevance, k_p, relevant_grade=theta)` / `recall_at_k(..., k_r, ...)`。
- `spearman(sorted(relevance) 的 [scoring_sf.get(t, 0.0)] × [relevance[t]])`（与 assemble_report 同式）。
- 计分清单 = produced 中 tag_id ∈ gt_space 的条目，防御性 `sorted(key=lambda r: (-sf, tag_id))`。
- summarize 排序 key：`lambda r: (r["ok"] is True, r["p_at_k"] is None, r["p_at_k"] if r["p_at_k"] is not None else 0.0, r["tt"])`——ok=False（失败）的 False 排最前；非 null P 升序；null 垫底；同分按 tt。

- [ ] **Step 4: 跑测试验证 GREEN**

Run: 同 Step 2
Expected: PASS（9 个用例全绿）

- [ ] **Step 5: Commit**

```bash
git add src/tagplatform/eval/movie_breakdown.py tests/test_movie_breakdown.py
git commit -m "feat: 单片 badcase 指标纯函数模块（对账口径+防御性排序）"
```

---

### Task 2: router 两端点

**Files:**
- Modify: `src/tagplatform/api/routers/eval.py`（新增 `_library_tag_map` 助手 + 7 个 pydantic 模型 + 2 端点；模块 docstring 11→13 端点）
- Test: `tests/test_api_eval.py`（文件末追加）

**Interfaces:**
- Consumes: Task 1 的 `pick/summarize_movies/breakdown_movie`（签名见上）；router 现有 `_run_dir(settings, run_id) -> Path | None`、`require_admin`、`get_settings_dep`；`try_load_library`（已 import）；`DEFAULT_THETAS/DEFAULT_KS`（已 import）；磁盘夹具模式同 `test_get_run_disk_only_fallback`（mkdir run 目录 + 手写 meta.json/raw_results.json）。
- Produces（Task 3 前端消费同形 JSON）:
  - `GET /api/v1/eval/closed/runs/{run_id}/movies` → `MovieSummaryOut`（`{theta_used, k_p, k_r, rows}` 包层，spec 钉死）
  - `GET /api/v1/eval/closed/runs/{run_id}/movies/{tt}` → `MovieDetailOut`

模型：`MovieSummaryRowOut` / `MovieSummaryOut` / `ProducedTagOut` / `OutOfSpaceTagOut` / `MissTagOut` / `MovieDetailOut`（字段与 Task 1 产出的 dict 键一一对应；可空字段 `| None = None`，形与 `RunSummaryOut` 同款）。

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_api_eval.py`）

用例（夹具：`_seed_dataset_and_library(client)` + run 目录手写 raw_results.json/meta.json，行数据造一部 hit/一部失败）：

- `test_run_movies_endpoint`：happy path——包层三键（theta_used=0.4/k_p=10/k_r=20 来自 meta thetas/ks）、rows 排序断言（失败置顶）、行字段齐全；admin。
- `test_run_movie_detail_endpoint`：happy path——produced/out_of_space/misses 三组、hit 标记、未知 tt → 404。
- `test_movies_404_when_raw_results_missing_or_truncated`：run 目录存在、raw_results.json 缺失 → 404 detail 含「无单片明细」；写入 `"{截断"` → 同 404。
- `test_movies_default_grid_when_meta_missing`：meta.json 缺失 → 响应 theta_used/k_p/k_r 为 DEFAULT 档（0.4/10/20）。
- `test_movies_non_admin_401`：复用现有非 admin 断言模式（两端点各一）。
- `test_movies_run_dir_guard`：`..%2F` 或绝对路径形态 run_id → 404（同 files 端点守卫测试模式）。

- [ ] **Step 2: 跑测试验证 RED**

Run: `PYTHONPATH=src C:/code/research-topics/movie-tag-research/intelligent-tag-platform/.venv/Scripts/python.exe -m pytest tests/test_api_eval.py -v -k movies`
Expected: FAIL —— 404（路由不存在，FastAPI 对未注册路径返回 404/405）

- [ ] **Step 3: 实现端点**

- `_library_tag_map() -> dict[str, str]`：`try_load_library()` → `{tag.tag_id: tag.tag_name}`，库未就绪 → `{}`（与 `_library_tag_ids` 同款容错，形参考 `eval.py:152`）。
- 两端点共同读侧（协议转换，模式同 `get_run_report`）：`_run_dir` → None 即 404 run 不存在；`json.loads((run_dir / "raw_results.json").read_text())`，`(OSError, ValueError)` 或非 list → `HTTPException(404, detail=f"该 run 无单片明细（未完成或产物缺失）：{run_id!r}")`；meta.json 读 thetas/ks，`(OSError, ValueError, KeyError, TypeError)` 或元素类型异常 → `DEFAULT_THETAS` / `DEFAULT_KS`。
- `/movies`：`summarize_movies(rows, thetas, ks, set(tag_map))` → `MovieSummaryOut(**payload)`；`/movies/{tt}`：`breakdown_movie(rows, tt, thetas, ks, set(tag_map), tag_map)`，None → `HTTPException(404, detail=f"影片不在该 run 明细中：{tt!r}")`。

- [ ] **Step 4: 跑测试验证 GREEN（含文件级回归）**

Run: `PYTHONPATH=src C:/code/research-topics/movie-tag-research/intelligent-tag-platform/.venv/Scripts/python.exe -m pytest tests/test_api_eval.py -v`
Expected: PASS（新增 6+ 全绿、存量不红）

- [ ] **Step 5: Commit**

```bash
git add src/tagplatform/api/routers/eval.py tests/test_api_eval.py
git commit -m "feat: 单片明细两端点（admin、读侧容错、包层选档回传）"
```

---

### Task 3: 前端 API 函数 + 单片明细组件

**Files:**
- Modify: `frontend/src/api/eval.ts`
- Create: `frontend/src/components/ClosedEvalMovieTable.tsx`
- Test: `frontend/src/components/ClosedEvalMovieTable.test.tsx`

**Interfaces:**
- Consumes: `apiFetch<T>`（`./client`）；Task 2 响应形状；组件测试 mock 模式同 `ClosedEval.test.tsx`（`vi.mock('../api/eval')` + `vi.mocked`）。
- Produces（Task 4 消费）: `getRunMovies(runId: string): Promise<MovieSummaryPayload>`、`getRunMovieDetail(runId: string, tt: string): Promise<MovieDetail>`、类型 `MovieSummaryRow / MovieSummaryPayload / MovieDetail / ProducedTag / OutOfSpaceTag / MissTag`；组件 `export default function ClosedEvalMovieTable({ runId }: { runId: string }): JSX.Element`。

类型（null 透传，与后端模型一致）：`MovieSummaryPayload { theta_used: number; k_p: number; k_r: number; rows: MovieSummaryRow[] }`；`MovieSummaryRow { tt: string; title: string; ok: boolean; error: string | null; spearman: number | null; p_at_k: number | null; r_at_k: number | null; elapsed_ms: number; n_produced: number; n_relevant: number }`；`MovieDetail { tt; title; ok; error: string | null; elapsed_ms: number; theta_used: number; k_p: number; k_r: number; produced: ProducedTag[]; out_of_space: OutOfSpaceTag[]; misses: MissTag[] }`；`ProducedTag { tag_id: string; tag_name: string | null; sf: number; confidence: number; evidence_count: number; gt_score: number | null; hit: boolean }`；`OutOfSpaceTag { tag_id: string; tag_name: string | null; sf: number }`；`MissTag { tag_id: string; tag_name: string | null; gt_score: number }`。

组件行为：mount → `getRunMovies`；列 `tt / 标题 / 状态(成功|失败) / Spearman / P@{k_p} / R@{k_r} / 耗时(秒) / 产出数 / 相关数`（表头字面量由 payload 档位拼出，如 `P@8`）；数值列带 antd sorter（服务端序为初始态，不预激活）；null 一律「暂无」；expandable 展开懒加载 `getRunMovieDetail(tt)` 并按 tt 缓存（重复展开不重复请求）；失败片展开只渲染 error 原文；成功片三组：计分清单小表（标签 `tag_name ?? tag_id`、sf、conf、GT 分、命中 ●/—）、空间外组、漏检组（标签、GT 分）；明细 fetch 失败 → 展开区显示 ApiError.message。

- [ ] **Step 1: 写失败测试** `ClosedEvalMovieTable.test.tsx`

用例（mock `../api/eval` 的 getRunMovies/getRunMovieDetail；夹具 3 行：失败片、p=0.2 命中片、p=None 片）：

- `renders server order and dynamic headers`：行序 = mock 数组序（不重排）；`P@8`/`R@20` 表头字面量（k_p=8 夹具）。
- `expands lazily and caches detail`：展开一行 → `vi.mocked(getRunMovieDetail)` called times 1 且三组渲染（produced 行带 ● 命中、tag_name null 回退 tag_id、misses 降序）；收起再展开 → 仍 called times 1。
- `failed row shows error only`：展开失败片 → error 文本可见、无 produced/misses 表。
- `null metrics render 暂无`。
- `detail fetch error shows message`：mockRejectedValueOnce(new Error('boom')) → 展开区出 'boom'。

- [ ] **Step 2: 跑测试验证 RED**

Run: `cd frontend && npx vitest run src/components/ClosedEvalMovieTable.test.tsx`
Expected: FAIL —— 无法解析 `./ClosedEvalMovieTable`（文件不存在）

- [ ] **Step 3: 实现 api 函数与组件**

- `eval.ts` 追加 6 个类型 + 2 个函数（`apiFetch<MovieSummaryPayload>(\`/api/v1/eval/closed/runs/${runId}/movies\`)`，detail 同形加 `/${tt}`；注释同文件风格，记录「与后端 MovieSummaryOut/MovieDetailOut 一致、null=暂无」）。
- 组件：antd `Table` + `expandable={{ expandedRowRender, onExpand }}`；明细状态 `Map<tt, MovieDetail | 'error'>` 缓存在 `useState`；列 `dataIndex` 直取，耗时 `Math.round(elapsed_ms/1000)`；三组各一个 size="small" Table 或列表（展示截断：计分清单全量滚动，执行者自定）。

- [ ] **Step 4: 跑测试验证 GREEN**

Run: `cd frontend && npx vitest run src/components/ClosedEvalMovieTable.test.tsx`
Expected: PASS（5 用例全绿）

- [ ] **Step 5: Commit**

```bash
git add frontend/src/api/eval.ts frontend/src/components/ClosedEvalMovieTable.tsx frontend/src/components/ClosedEvalMovieTable.test.tsx
git commit -m "feat: 单片明细表组件（懒加载展开、三组对照、动态表头）"
```

---

### Task 4: 集成进报告视图 + 全套验证

**Files:**
- Modify: `frontend/src/components/ClosedEvalReport.tsx`（Spearman 节后新增「单片明细」节）
- Test: `frontend/src/components/ClosedEvalReport.test.tsx`（`vi.mock('./ClosedEvalMovieTable')`）

**Interfaces:**
- Consumes: Task 3 的 `ClosedEvalMovieTable({ runId })`。

- [ ] **Step 1: 写失败测试**（`ClosedEvalReport.test.tsx` 顶部加 `vi.mock('./ClosedEvalMovieTable', () => ({ default: () => <div data-testid="movie-table" /> }))`；新增用例）

- `renders movie table section with runId`：渲染 `<ClosedEvalReport report={REPORT} runId="run-1" />` → `movie-table` 出现在 `spearman-section` 之后、`downloads-section` 之前。

- [ ] **Step 2: 跑测试验证 RED**

Run: `cd frontend && npx vitest run src/components/ClosedEvalReport.test.tsx`
Expected: FAIL —— 找不到 `movie-table`（节未渲染）

- [ ] **Step 3: 集成**

`ClosedEvalReport.tsx` import 组件，Spearman 节与失败影片节之间加：

```tsx
<div data-testid="movie-section">
  <Typography.Title level={5}>单片明细</Typography.Title>
  <ClosedEvalMovieTable runId={runId} />
</div>
```

- [ ] **Step 4: 全套验证**

Run: `cd frontend && npx vitest run && npx tsc -b`
Expected: 全绿 + 构建零错误
Run: `PYTHONPATH=src C:/code/research-topics/movie-tag-research/intelligent-tag-platform/.venv/Scripts/python.exe -m pytest`
Expected: 全绿（842+ 存量 + 新增）

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/ClosedEvalReport.tsx frontend/src/components/ClosedEvalReport.test.tsx
git commit -m "feat: 报告视图接入单片明细节"
```
