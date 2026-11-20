# 闭环任务评测实施计划（前端页签 + 干跑报告，DB 零写入）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给闭环生成建一套可重复的量化评测：管理员在前端页签选数据集/影片集起跑，后台干跑闭环管线（不写库），对 MovieTags genome gt 算 θ×K 网格的 P@K/R@K 等排序指标，报告落盘、历史可追溯。

**Architecture:** 三层——`eval/closed_ranking.py`（纯标准库指标装配）→ `closed_eval.py`（数据集发现 + 干跑运行器 + 报告组装 + 运行注册表）→ `api/routers/eval.py`（admin API）；执行器的 spec 组装抽成 `assemble_spec` 供评测与生产共用。前端 `api/eval.ts` + `pages/ClosedEval.tsx`（表单/进度/历史）+ `components/ClosedEvalReport.tsx`（报告视图）。

**Tech Stack:** Python / FastAPI / SQLAlchemy / pytest（后端）；React 19 + antd 6 + vitest（前端，零新依赖）。

**Spec:** `docs/superpowers/specs/2026-11-20-closed-loop-eval-design.md`（已批准；本计划从它论证，执行者两个都读）

## Global Constraints

- **DB 零写入红线**：评测代码路径不得 import/调用 `persist_tags`，不得写 media_asset / tag_generation_task / pre_asset_tag / tag_quality_evaluation / data/audit；唯一写盘位置是 `settings.eval_output_root`
- **eval/ 纯标准库红线**：`closed_ranking.py` 不得 import fastapi / numpy / scipy / 平台模块（Spearman 手写，含并列平均秩）
- `ClosedTagProducer` 及其行为零改动；执行器重构只做「抽公共」，既有测试全绿是回归网
- `/cache/MovieTags` 只读、`/cache/MovieNet` 不碰；测试中数据集根/输出根一律指向 tmp（不得扫 /cache/*）
- API 前缀 `/api/v1/eval/closed`；全部端点 `require_admin`（非 admin 403）；create_app 处统一挂 `get_current_user`
- 数值钉死：θ 默认 `(0.2, 0.4, 0.6, 0.8)`；K 默认 `(5, 10, 20, 40)`；试点 30 部 seed=20261120；并发默认 4；单片重试 2 次（共 3 次尝试）；**完成序**连续 5 片失败中止整轮；失败率 >10% 报告警示；run_id = `run-YYYYMMDD-HHMMSS-<4 hex>`
- 前端零新依赖（antd + 既有 `BarChart`）；null 数值一律渲染「暂无」
- 测试命令：后端 `python -m pytest tests/<file> -v`（仓库根执行）；每任务收尾跑整套 `python -m pytest -q` 全绿；前端 `cd frontend && npm test -- <文件名>`
- 注释/文档中文；模块头部 docstring 写「为什么」（沿用各文件惯例）

## Review Focus

spec 隐含但常规测试不会自动覆盖、最容易咬人的输入类/失败模式（每条挂到归属任务的测试）：

1. gt CSV 畸形（缺列/坏首行）→ 扫描**跳过该文件**而不是崩掉整个列表（Task 2 `test_scan_datasets_skips_invalid`）
2. 库∩gt 交集为空 → 起跑显式拒绝（400），绝不产出全 0 指标（Task 4 `test_intersection_empty_rejected`）
3. 干跑写库泄漏 → 全流程跑完资产六表仍为 0 行（Task 4 `test_db_zero_writes`）
4. 进程重启后 meta 残留 running → 历史列表标「中断」，不误报进行中（Task 5 `test_history_scans_disk_and_infers_interrupted`）
5. 取消后部分报告口径 → done<total 时 banner + 样本核算，防与全量横比（Task 5 `test_partial_run_banner`）
6. 活跃 run 双击起跑 → 409 + 按钮禁用，防双倍 token（Task 6 `test_start_busy_409`、Task 8 `test_start_disabled_while_active`）

---

### Task 1: 指标装配模块 `eval/closed_ranking.py`（纯标准库）

**Files:**
- Create: `src/tagplatform/eval/closed_ranking.py`
- Test: `tests/test_eval_closed_ranking.py`

**Interfaces:**
- Consumes: `tagplatform/eval/ranking.py` 原语（`precision_at_k` / `recall_at_k` / `ndcg_at_k` / `macro_average` / `bootstrap_ci`）
- Produces（后续任务依赖，签名钉死）:
  - `DEFAULT_THETAS: tuple[float, ...] = (0.2, 0.4, 0.6, 0.8)`；`DEFAULT_KS: tuple[int, ...] = (5, 10, 20, 40)`
  - `spearman(xs: Sequence[float], ys: Sequence[float]) -> float | None`
  - `@dataclass(frozen=True) ClosedSample`：`ranked: tuple[str, ...]`（按系统分降序的 tag_id）、`relevance: dict[str, float]`（gt 0–1 分，覆盖该影片全部交集标签）、`system_scores: dict[str, float]`（系统 Sf，仅产出标签）、`label: str = ""`
  - `random_precision_recall(n_candidates: int, n_relevant: int, k: int) -> tuple[float, float]`
  - `@dataclass GridPoint`：`theta: float; k: int; n_valid: int; precision/recall/ndcg: float | None; precision_ci/recall_ci/ndcg_ci: tuple[float, float] | None; precision_random/recall_random: float | None`
  - `@dataclass ClosedReport`：`n_total: int; n_valid: int; n_invalid: int; grid: list[GridPoint]; coverage: list[float]; coverage_min/coverage_median/coverage_max: float | None; spearman_macro: float | None; warnings: list[str]`
  - `evaluate_closed(samples: list[ClosedSample], thetas: Sequence[float] = DEFAULT_THETAS, ks: Sequence[int] = DEFAULT_KS) -> ClosedReport`

- [ ] **Step 1: 写失败测试** `tests/test_eval_closed_ranking.py`（模块 docstring 注明「纯标准库、可被脚本与未来离线工具独立复用」）

```python
def test_spearman_perfect_reversed_and_none():
    assert spearman([1, 2, 3], [10, 20, 30]) == 1.0
    assert spearman([1, 2, 3], [30, 20, 10]) == -1.0
    assert spearman([1, 1, 1], [1, 2, 3]) is None  # 零方差 → 算不出，None≠0

def test_spearman_ties_average_ranks():
    # 秩 xs=[1.5,1.5,3]、ys=[1,2,3] → Pearson-on-ranks = 1.5/√3
    assert spearman([1, 1, 2], [1, 2, 3]) == pytest.approx(0.866, abs=1e-3)

def test_grid_hand_case():
    # 10 标签：前 3 个 gt=0.9，其余 0.1；ranked 前 5 恰含前 3
    rel = {f"t{i}": (0.9 if i < 3 else 0.1) for i in range(10)}
    s = ClosedSample(ranked=("t0","t1","t2","t3","t4"), relevance=rel,
                     system_scores={"t0":1.0,"t1":0.9,"t2":0.8,"t3":0.7,"t4":0.6})
    r = evaluate_closed([s], thetas=(0.5,), ks=(5,))
    gp = r.grid[0]
    assert gp.precision == pytest.approx(0.6)   # 3/5
    assert gp.recall == pytest.approx(1.0)      # 3/3
    assert gp.precision_random == pytest.approx(0.3)  # r/n = 3/10
    assert gp.recall_random == pytest.approx(0.5)     # min(k,n)/n

def test_ndcg_identical_ranking_is_one():
    rel = {"a":0.9,"b":0.7,"c":0.5,"d":0.3}
    s = ClosedSample(ranked=("a","b","c","d"), relevance=rel, system_scores={})
    r = evaluate_closed([s], thetas=(0.4,), ks=(4,))
    assert r.grid[0].ndcg == pytest.approx(1.0)

def test_invalid_sample_excluded_not_averaged():
    good = ClosedSample(ranked=("t0",), relevance={"t0":0.9}, system_scores={"t0":1.0})
    bad = ClosedSample(ranked=(), relevance={}, system_scores={})  # 无交集
    r = evaluate_closed([good, bad], thetas=(0.5,), ks=(1,))
    assert (r.n_total, r.n_valid, r.n_invalid) == (2, 1, 1)
    assert r.grid[0].precision == pytest.approx(1.0)

def test_theta_empty_relevant_excluded_per_grid():  # spec §7：该 θ 下相关集空的影片不混进平均
    hot = ClosedSample(ranked=("t0","t1"), relevance={"t0":0.9,"t1":0.8}, system_scores={"t0":1.0,"t1":0.5})
    cold = ClosedSample(ranked=("t0",), relevance={"t0":0.3}, system_scores={"t0":1.0})  # 0.3 < θ → 该 θ 下无相关
    r = evaluate_closed([hot, cold], thetas=(0.5,), ks=(2,))
    assert r.grid[0].n_valid == 1
    assert r.grid[0].precision == pytest.approx(1.0)  # 只算 hot：前 2 含 2 相关


def test_empty_samples_none_not_zero():
    r = evaluate_closed([], thetas=(0.5,), ks=(5,))
    assert r.grid[0].precision is None  # 分母为 0 → None（None≠0 纪律）

def test_coverage_stats():
    samples = [...]  # 三样本 coverage = 0.5 / 0.25 / 1.0（relevance 4 键，ranked 2/1/4 条）
    r = evaluate_closed(samples, thetas=(0.5,), ks=(4,))
    assert (r.coverage_min, r.coverage_median, r.coverage_max) == (0.25, 0.5, 1.0)

def test_bootstrap_ci_constant_samples():
    # 5 个完全同样本 → 每格 CI 退化为 (值, 值)
    ...

def test_default_constants():
    assert DEFAULT_THETAS == (0.2, 0.4, 0.6, 0.8) and DEFAULT_KS == (5, 10, 20, 40)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_eval_closed_ranking.py -v`
Expected: FAIL（ModuleNotFoundError / ImportError: closed_ranking）

- [ ] **Step 3: 实现 `src/tagplatform/eval/closed_ranking.py`**

算法钉死（签名与测试不决定的部分）：
- `spearman`：两侧各自换平均秩（并列取均值），在秩上做 Pearson；任一侧秩方差为 0 → None；长度不等/空 → None
- `random_precision_recall`：不放回均匀抽样的解析期望 `E[P@K] = r/n`、`E[R@K] = min(k, n) / n`（n=0 或 r=0 → (0.0, 0.0)）
- `evaluate_closed`：逐 θ 先筛 `valid_θ = [s for s in samples if s.relevance and any(v >= θ for v in s.relevance.values())]`（**该 θ 下相关集空的影片不进该格平均**，`GridPoint.n_valid` 记实际参与数——spec §7「无效影片单独报」）；逐格——`precision_at_k/recall_at_k(ranked, relevance, k, relevant_grade=θ)` 在 valid_θ 上宏平均 + `bootstrap_ci`（复用 ranking 默认 n_boot=2000、seed）；`ndcg_at_k` 连续增益（valid_θ 上）；每格随机基线用 `random_precision_recall(len(relevance), r_θ, k)`（r_θ = 该片 relevance 值 ≥θ 的条数，逐片解析解再宏平均）；coverage = `len(ranked)/len(relevance)`；`spearman_macro` = 每片在 relevance 键上 `spearman([system_scores.get(t, 0.0)...], [relevance[t]...])` 过滤 None 后宏平均；`coverage_median` 用 `statistics.median`
- relevance 为空的样本（该影片无交集标签）不计入任何平均，只计 `n_invalid`；valid_θ 为空时格值为 None

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_eval_closed_ranking.py -v`
Expected: PASS 全绿

- [ ] **Step 5: 整套回归**

Run: `python -m pytest -q`
Expected: 全绿（新模块零平台依赖，不应影响任何既有测试）

- [ ] **Step 6: Commit**

```bash
git add src/tagplatform/eval/closed_ranking.py tests/test_eval_closed_ranking.py
git commit -m "feat: 闭环评测指标装配（θ×K 网格 + Spearman + 解析随机基线，纯标准库）"
```

---

### Task 2: 数据集发现与影片加载（`closed_eval.py` 第一部分 + settings）

**Files:**
- Create: `src/tagplatform/closed_eval.py`（本任务只写数据集层）
- Modify: `src/tagplatform/settings.py`（阶段 8 字段）
- Test: `tests/test_closed_eval.py`

**Interfaces:**
- Consumes: `content_io.load_content`（自动识别上游形态）、`schemas.ContentJSON`
- Produces:
  - Settings 新字段（阶段 8 注释块「闭环评测（2026-11）」）：`eval_dataset_root: str = "/cache/MovieTags"`、`eval_output_root: str = "data/eval/closed"`
  - `@dataclass DatasetInfo`：`id: str; gt_path: str; n_movies: int; n_tags: int; library_overlap: int; usable_movies: list[str]`
  - `scan_datasets(root: str, library_tag_ids: Collection[str]) -> list[DatasetInfo]`（按 id 排序；进程内缓存按 (path, mtime) 失效）
  - `load_gt(root: str, dataset_id: str) -> dict[str, dict[str, float]]`（{movieId: {tagId: relevance}}；未知 id → ValueError）
  - `load_movie_content(root: str, tt: str) -> ContentJSON`
  - `movie_entries(root: str, dataset_id: str) -> list[dict[str, str]]`（usable 影片的 `[{tt, title}]`，按 tt 排序；标题读 content.json 的 asset.title，进程内缓存）
  - `select_movies(usable: list[str], mode: str, custom: list[str] | None, *, size: int = 30, seed: int = 20261120) -> list[str]`（preset30 = `sorted(usable)` 后 `random.Random(seed).sample(..., min(size, len))`；custom 越权 id → ValueError 列出未知项；返回排序去重列表）
  - 常量 `PRESET_SIZE = 30`、`PRESET_SEED = 20261120`

- [ ] **Step 1: 写失败测试**（测试文件内置夹具 `_write_dataset(root)`：`gt/emotion.csv` 三列首行 `movieId,tagId,relevance`、3 影片 × 4 标签；`gt/bad.csv` 首行两列；`data/tt0000001|tt0000002/content.json` 上游形态含 `dialogues[].speak_id`，`data/tt0000003` 缺失）

```python
def test_scan_datasets_skips_invalid(root):  # Review Focus 1
    infos = scan_datasets(root, library_tag_ids={"t1", "t9"})
    assert [i.id for i in infos] == ["emotion"]
    assert (infos[0].n_movies, infos[0].n_tags, infos[0].library_overlap) == (3, 4, 2)

def test_scan_datasets_usable_excludes_missing_content(root):
    assert scan_datasets(root, set())[0].usable_movies == ["tt0000001", "tt0000002"]

def test_load_gt_unknown_dataset_raises(root):
    with pytest.raises(ValueError):
        load_gt(root, "nope")

def test_load_movie_content_renames_speak_id(root):
    content = load_movie_content(root, "tt0000001")
    assert content.content.scenes[0].dialogues[0].speaker == "character_001"

def test_movie_entries_titles(root):
    entries = movie_entries(root, "emotion")
    assert entries == [{"tt": "tt0000001", "title": "A Movie"},
                       {"tt": "tt0000002", "title": "B Movie"}]

def test_select_movies_preset_deterministic(root):
    a = select_movies(["x1","x2","x3"], "preset30")
    b = select_movies(["x3","x1","x2"], "preset30")
    assert a == b and len(a) == 3          # 不足 30 → 全取
    assert select_movies(["x1","x2"], "all") == ["x1", "x2"]
    with pytest.raises(ValueError, match="tt9999"):
        select_movies(["x1"], "custom", ["x1", "tt9999"])
```

- [ ] **Step 2: 确认失败**：`python -m pytest tests/test_closed_eval.py -v` → FAIL（ImportError: closed_eval）

- [ ] **Step 3: 实现**——`scan_datasets` 用 `csv` 标准库读首行与逐行计数（畸形首行/不可解析行 → 跳过该文件，模块 docstring 写明「宁缺勿崩」）；`load_movie_content` 读 JSON 后把 `content.scenes[*].dialogues[*].speak_id` 键改名 `speaker_id`（浅遍历、键存在才改），再 `load_content(json.dumps(...))`（上游形态自动识别转换）；settings 加两字段（带「换数据集根 = 改 .env」注释）

- [ ] **Step 4: 通过**：`python -m pytest tests/test_closed_eval.py -v` PASS

- [ ] **Step 5: 整套回归 + Commit**：`python -m pytest -q` 全绿；commit `feat: 闭环评测数据集发现（gt 扫描/影片加载/选片，settings 两字段）`

---

### Task 3: spec 组装公共化（执行器抽取 `assemble_spec`）

**Files:**
- Modify: `src/tagplatform/tasks/runner.py`（新增 `assemble_spec`，`run_generation_task` 改调用）
- Test: `tests/test_task_runner.py`（追加用例；既有用例零改动是回归网）

**Interfaces:**
- Produces:
  - `assemble_spec(session: Session, *, mode: str, content: ContentJSON, task_id: str, asset_id: str, run_id: str, policy_json: str = "") -> tuple[GenerationSpec, PromptTemplate | None]`（在 `tasks/runner.py`）：内部做 try_load_library → get_producer(mode) → policy 解析 → 非 builds_prompt 生产器取 `get_active_prompt(session, mode)` + `render_prompt(template, content=_render_content_text(content))` → 组装 GenerationSpec；返回 (spec, template)（builds_prompt 生产者 template=None）

- [ ] **Step 1: 写失败测试**（conftest `session` + `content_fixture`；先插激活模板行）

```python
def test_assemble_spec_closed_renders_active_template(session, content_fixture):
    session.add(PromptTemplate(mode="closed", version=1, template_text="{content}", is_active=True))
    session.commit()
    spec, template = assemble_spec(session, mode="closed", content=content_fixture,
                                   task_id="t1", asset_id="a1", run_id="r1")
    assert spec.library is not None and spec.task_id == "t1"
    assert "摘要" in spec.prompt_text and template.version == 1

def test_assemble_spec_policy_json_overrides_default(session, content_fixture):
    ...  # 同上插模板；policy_json='{"max_tags": 5}' → spec.policy.max_tags == 5
```

- [ ] **Step 2: 确认失败**（ImportError: assemble_spec）
- [ ] **Step 3: 实现**——把 `run_generation_task` 第 4/4b/5/6 步的组装段原样搬进 `assemble_spec`（**只搬不改**：渲染函数、库加载、policy 语义逐字不动），执行器改为调用；模块 docstring 补一行「assemble_spec 为执行器与闭环评测（closed_eval）共用」
- [ ] **Step 4: 通过**：`python -m pytest tests/test_task_runner.py -v`（新用例 PASS，既有全绿——行为零变化）
- [ ] **Step 5: 整套回归 + Commit**：`python -m pytest -q`；commit `refactor: 抽 assemble_spec 供执行器与闭环评测共用（行为不变）`

---

### Task 4: 干跑运行器（`ClosedEvalService` 核心循环）

**Files:**
- Modify: `src/tagplatform/closed_eval.py`（服务层主体）
- Test: `tests/test_closed_eval.py`（追加）

**Interfaces:**
- Consumes: Task 2 全部 + Task 3 `assemble_spec`、`get_producer("closed")`、conftest `MOCK_LLM` / `MOCK_EMBEDDER`
- Produces:
  - `class RunBusyError(RuntimeError)`；`class EvalValidationError(ValueError)`
  - `@dataclass MovieResult`：`tt: str; title: str; ok: bool; error: str | None; produced: list[dict]; elapsed_ms: int`（produced 行：`{tag_id, tag_name, sf, confidence, evidence_count}`）
  - `@dataclass RunState`：`run_id: str; status: str; done: int; total: int; failed: int; started_at: str; error: str | None = None`
  - `class ClosedEvalService(session_factory, llm, embedder, settings: Settings)`：
    - `start(dataset_id: str, movies: dict, thetas=DEFAULT_THETAS, ks=DEFAULT_KS, concurrency: int = 4, *, wait: bool = False) -> str`（返回 run_id；wait=True 供测试同步等完）
    - `get(run_id: str) -> RunState | None`；`cancel(run_id: str) -> bool`；`active_run_id() -> str | None`
    - 内部：`_run_movie(tt) -> MovieResult`（每片独立会话 → `assemble_spec(session, mode="closed", content=load_movie_content(...), task_id=f"eval-{run_id}", asset_id=tt, run_id=f"{run_id}:{tt}")` → `produce(spec, session, llm, embedder)` → 产出按 `(-sf, tag_id)` 排序成 produced 行；共尝试 3 次（RETRY_PER_MOVIE=2），异常兜底记 error）
    - 常量：`DEFAULT_CONCURRENCY = 4`、`RETRY_PER_MOVIE = 2`、`ABORT_CONSECUTIVE = 5`
  - 终态写盘（worker 主线程）：`meta.json`（run 参数、dataset {id, gt_path, gt_sha8=sha256 前 8 hex, n_rows}、library {version=library.version, n_dirs, n_tags}、prompt {mode:"closed", version}、llm_model/embedding_model、movies {mode, ids}、thetas/ks/concurrency、started_at/finished_at、status/done/total/failed）、`raw_results.json`（`[{tt, title, ok, error, produced, gt: {tag: score}, elapsed_ms}]`）、`progress.jsonl`（每片一行 `{tt, ok, produced, elapsed_ms, error?}`，锁内追加）

- [ ] **Step 1: 写失败测试**（沿用 `_write_dataset` 夹具；文件库 + `bind_engine`/`seed_database`（仿 conftest client fixture）+ 插激活 closed 模板；服务用 conftest `MOCK_LLM`/`MOCK_EMBEDDER`；`wait=True`）

```python
def _make_service(tmp_path, llm=MOCK_LLM): ...  # 文件 DB + 库 + 模板 + settings(eval 根指向 tmp)

def test_run_completes_and_writes_artifacts(svc, root):
    run_id = svc.start("emotion", {"mode": "all"}, wait=True)
    assert svc.get(run_id).status == "succeeded"
    d = Path(svc.settings.eval_output_root) / run_id
    assert (d / "meta.json").exists() and (d / "raw_results.json").exists()
    rows = json.loads((d / "raw_results.json").read_text(encoding="utf-8"))
    assert len(rows) == 2 and all(r["ok"] for r in rows)

def test_db_zero_writes(engine, svc, root):  # Review Focus 3
    svc.start("emotion", {"mode": "all"}, wait=True)
    with engine.connect() as con:
        for t in ("media_asset", "pre_asset_tag", "tag_generation_task", "asset_build_task"):
            assert con.execute(text(f"select count(*) from {t}")).scalar() == 0

def test_intersection_empty_rejected(svc, root):  # Review Focus 2
    with pytest.raises(EvalValidationError):
        svc.start("emotion", {"mode": "all"}, wait=True)  # 夹具：库标签与 gt 全不交

def test_single_movie_failure_records_and_continues(...):   # llm 包装器：仅 tt0000002 恒抛错
    ...  # status succeeded、failed=1、该行 ok=False 且 error 非空

def test_consecutive_failures_abort(...):  # llm 全抛错 + 6 片数据集
    ...  # status == "aborted"、done < total

def test_busy_run_rejects(...):  # 慢 llm（sleep 0.1/片）
    r1 = svc.start("emotion", {"mode": "all"})     # wait=False
    with pytest.raises(RunBusyError):
        svc.start("emotion", {"mode": "all"})
    svc.cancel(r1)

def test_cancel_keeps_completed_results(...):  # 慢 llm；轮询 get() 到 done>=1 后 cancel
    ...  # status == "cancelled"；raw_results 行数 < total；meta 记 done<total
```

- [ ] **Step 2: 确认失败**（ImportError: ClosedEvalService）
- [ ] **Step 3: 实现**——start 校验序：数据集存在（scan）→ 影片集（select_movies）→ 库加载（try_load_library，None → EvalValidationError「标签库未就绪」）→ 交集非空（库标签 ∩ gt 标签）→ Busy 检查 → 写 run 目录骨架 → 起 daemon 线程。worker：`ThreadPoolExecutor(concurrency)` 按 tt 序提交 `_run_movie`，`as_completed` 收敛——**完成序**连续 `ABORT_CONSECUTIVE` 失败 → `future.cancel()` 余量 + status=aborted；cancel 事件置位后不再提交/收敛即收尾（在途片跑完）；每片完成锁内追加 progress.jsonl 并更新注册表；终态（succeeded/cancelled/aborted）写 meta + raw_results（gt 向量并入）。run_id：`run-{now:%Y%m%d-%H%M%S}-{token_hex(2)}`。注册表与 cancel 走 `threading.Lock`。慢 llm 测试缝隙：测试包装 `MOCK_LLM` 子类在 complete 里 `time.sleep`
- [ ] **Step 4: 通过**：`python -m pytest tests/test_closed_eval.py -v` PASS
- [ ] **Step 5: 整套回归 + Commit**：`python -m pytest -q`；commit `feat: 闭环评测干跑运行器（重试/中止/取消/增量落盘，DB 零写入）`

---

### Task 5: 报告组装 + 历史扫描 + eval.md 口径

**Files:**
- Modify: `src/tagplatform/closed_eval.py`（`assemble_report` / `render_report_md` / `write_report` / `history`；worker 终态接入）
- Modify: `docs/eval.md`（新增「4. 闭环排序评测」一节）
- Test: `tests/test_closed_eval.py`（追加）

**Interfaces:**
- Consumes: Task 1 `evaluate_closed`、Task 4 `MovieResult`
- Produces:
  - `assemble_report(results: list[MovieResult], gt: dict[str, dict[str, float]], tag_universe: list[str], thetas, ks, *, meta: dict) -> dict`——report.json 结构：`banner`（status != succeeded 或 done<total → 「部分运行：指标仅覆盖完成影片，与全量口径不可直接横比」）、`sample_accounting {total, ok, failed, invalid, no_gt}`、`grid`（ClosedReport.grid 平铺 + CI）、`baselines.prior`（先验样本同机制算的 grid）、`coverage {min, median, max, values}`、`spearman {macro, per_movie}`、`failed_movies [{tt, error}]`、`warning`（失败率 >10% → 小样本警示文案，否则 None）、`calibre {llm_model, embedding_model, prompt_version, library_version, dataset_id, gt_sha8}`、`note`（银标准警示文案，eval.md §4 同口径）
  - 先验基线构造（钉死）：先验序 = tag_universe 按 **gt 全影片均值**降序（并列 tag_id 字典序）；每部 ok 影片用同一 ranked 构造 ClosedSample 喂 `evaluate_closed`
  - `render_report_md(report: dict) -> str`；`write_report(run_dir: Path, report: dict)`（report.json / report.md / metrics.csv——长表行 `theta,k,metric,macro,ci_low,ci_high,random_baseline`）
  - `history() -> list[dict]`：扫 `eval_output_root/*/meta.json`（根不存在 → []）；meta.status==running 且不在注册表 → 标 `interrupted`；按 started_at 降序；有 report.json 的行并入摘要 `{ndcg10, p10, r20, coverage_median}`（θ=0.4、K=10/20，无该档 → null）

- [ ] **Step 1: 写失败测试**

```python
def test_report_grid_matches_metrics_module():
    # 手构 MovieResult×2 + gt → assemble_report 的 grid[θ=0.5,k=5].precision
    # == evaluate_closed(同样本).grid[...].precision（同数）
def test_prior_baseline_present():
    # baselines.prior 非空且 precision == evaluate_closed(先验样本) 同值
def test_universe_filtering_no_gt_counted():  # spec §5 保护原则
    # 产出行含一个 tag_universe 外的标签：该行不计入 ranked（P 值与剔除后相同）、
    # sample_accounting.no_gt == 1
def test_partial_run_banner():        # Review Focus 5
    # meta status="cancelled" → banner 含「部分运行」；ok < total
def test_failure_rate_warning():
    # 10 片 2 失败 → warning 非空；0 失败 → warning is None
def test_report_md_and_csv_written():
    # write_report 后三文件存在；md 含 θ 表头与银标准警示；csv 行数 == 格数×指标数
def test_history_scans_disk_and_infers_interrupted():  # Review Focus 4
    # 手写两个 run 目录（其一 meta.status="running"）→ history: 该行 status=="interrupted"、
    # 降序、有 report.json 的行带摘要；输出根不存在 → []
def test_service_end_to_end_writes_report(svc):
    # wait=True 跑完 → report.json 存在且含 grid/baselines/calibre/note 键
```

- [ ] **Step 2: 确认失败** → **Step 3: 实现**（worker 终态统一调 `assemble_report` + `write_report`；样本构造：ok 影片 `ranked` = produced 行中 **tag_universe 内的条目**（防御性再排 `(-sf, tag_id)`；交集外产出条目剔除、计入 `sample_accounting.no_gt`——spec §5 保护原则，白给 precision/冤枉 recall 都不要）、`relevance` = gt 影片向量限 tag_universe、`system_scores` = {tag_id: sf}；eval.md §4 写口径五条：θ 语义=gt 二值化档、None≠0、随机+先验基线必读、覆盖率封顶 R、部分运行不横比——与既有三节同文风）
- [ ] **Step 4: 通过**：`python -m pytest tests/test_closed_eval.py -v`
- [ ] **Step 5: 整套回归 + Commit**：`python -m pytest -q`；commit `feat: 闭环评测报告组装（θ×K/基线/覆盖率/部分口径）与历史扫描 + eval.md §4`

---

### Task 6: settings 装配 + admin API 路由

**Files:**
- Create: `src/tagplatform/api/routers/eval.py`
- Modify: `src/tagplatform/api/__init__.py`（`get_closed_eval` 依赖 + create_app 构建 + include）
- Modify: `README.md`（环境变量表补 `TAGPLT_EVAL_DATASET_ROOT` / `TAGPLT_EVAL_OUTPUT_ROOT` 两行；页面清单补「闭环评测（admin）」）
- Modify: `tests/conftest.py`（client fixture 的 Settings 增 `eval_dataset_root=str(tmp_path/"eval-ds")`、`eval_output_root=str(tmp_path/"eval-out")`——注释同 dataset_root 的隔离理由）
- Test: `tests/test_api_eval.py`

**Interfaces:**
- Consumes: Task 2/4/5 全部
- Produces（路由契约，全部 `Depends(require_admin)`）：
  - `GET /api/v1/eval/closed/datasets` → `list[DatasetInfoOut]`（`{id, gt_path, n_movies, n_tags, library_overlap, usable_movies: int}`——usable_movies 输出为**数量**）
  - `GET /api/v1/eval/closed/datasets/{dataset_id}/movies` → `list[MovieRefOut]`（`{tt, title}`）
  - `POST /api/v1/eval/closed/runs`：body `StartRunRequest{dataset: str, movies: {mode: Literal["preset30","all","custom"], ids: list[str] | None}, thetas: list[float] | None, ks: list[int] | None, concurrency: int | None}`（None → 服务端默认）→ `{run_id}`；`EvalValidationError` → 400；未知 dataset → 404；`RunBusyError` → 409（detail 含活跃 run_id）
  - `GET /runs` → `list[RunSummaryOut]`（history 行 + 摘要）；`GET /runs/{run_id}` → `RunState` 形（404 未知）；`POST /runs/{run_id}/cancel` → `{ok: bool}`；`GET /runs/{run_id}/report` → report.json（404 无报告）；`GET /runs/{run_id}/files/{name}` → FileResponse（**白名单**：report.md / metrics.csv / raw_results.json / meta.json / progress.jsonl，白名单外 404）
  - `api/__init__.py`：`get_closed_eval(request) -> ClosedEvalService`（同 get_executor 模式）；create_app 在 `app.state.llm` 之后构建 `app.state.closed_eval = ClosedEvalService(app.state.session_factory, app.state.llm, app.state.embedder, settings)`；`app.include_router(eval.router, dependencies=protected_deps)`

- [ ] **Step 1: 写失败测试**（client fixture + 测试内往 `client.app.state.settings.eval_dataset_root` 写夹具数据集、插激活 closed 模板（经 `app.state.session_factory`）；非 admin 用例仿 `test_api_prompts_admin.py`：建 reviewer → 新 TestClient 登录）

```python
def test_datasets_endpoint_admin_only(client):
    _seed_dataset_and_prompt(client)
    r = client.get("/api/v1/eval/closed/datasets")
    assert r.status_code == 200 and r.json()[0]["id"] == "emotion"
    # reviewer → 403（独立登录态断言）

def test_run_end_to_end(client):
    _seed_dataset_and_prompt(client)
    run_id = client.post("/api/v1/eval/closed/runs",
                         json={"dataset": "emotion", "movies": {"mode": "all"}}).json()["run_id"]
    for _ in range(100):  # 轮询到终态（mock llm 毫秒级）
        st = client.get(f"/api/v1/eval/closed/runs/{run_id}").json()
        if st["status"] != "running": break
        time.sleep(0.1)
    assert st["status"] == "succeeded" and st["done"] == st["total"]
    rep = client.get(f"/api/v1/eval/closed/runs/{run_id}/report")
    assert rep.status_code == 200 and "grid" in rep.json()
    assert client.get(f"/api/v1/eval/closed/runs/{run_id}/files/report.md").status_code == 200
    assert any(row["run_id"] == run_id for row in client.get("/api/v1/eval/closed/runs").json())

def test_start_busy_409(client):  # Review Focus 6：换慢 llm 后双发
    ...
def test_files_whitelist_404(client):
    # files/whatever.txt → 404
def test_unknown_dataset_404(client): ...
```

- [ ] **Step 2: 确认失败**（404 路由不存在）→ **Step 3: 实现**（pydantic 校验 422 自动；错误映射集中在 router 层 try/except）
- [ ] **Step 4: 通过**：`python -m pytest tests/test_api_eval.py -v`
- [ ] **Step 5: 整套回归 + Commit**：`python -m pytest -q`；commit `feat: 闭环评测 admin API（8 端点 + app 装配 + conftest 隔离）`

---

### Task 7: 前端 API 模块 `api/eval.ts`

**Files:**
- Create: `frontend/src/api/eval.ts`、`frontend/src/api/eval.test.ts`

**Interfaces:**
- Consumes: `apiFetch`（client.ts）
- Produces（类型 + 函数，后续两任务依赖）：
  - 类型：`DatasetInfo{id, gt_path, n_movies, n_tags, library_overlap, usable_movies: number}`、`MovieRef{tt, title}`、`MovieSelection{mode: 'preset30'|'all'|'custom', ids?: string[]}`、`StartRunRequest{dataset, movies, thetas?, ks?, concurrency?}`、`RunStatus{run_id, status, done, total, failed, started_at, error?}`、`RunSummary{run_id, started_at, dataset_id, status, done, total, failed, ndcg10, p10, r20, coverage_median}`（摘要字段 `number | null`）、`ClosedEvalReport{banner?, sample_accounting, grid: GridRow[], baselines, coverage, spearman, failed_movies, warning?, calibre, note}`（GridRow 对齐 report.json 格）
  - 函数：`listDatasets() / listMovies(datasetId) / startRun(req) / listRuns() / getRun(runId) / cancelRun(runId) / getReport(runId)` + `fileUrl(runId, name): string`（纯路径拼接 `/api/v1/eval/closed/runs/${runId}/files/${name}`）

- [ ] **Step 1: 写失败测试**（vi.mock client 的 apiFetch，路径分发断言 method/body；fileUrl 断言字符串；同 `api/metrics.test.ts` 模式）
- [ ] **Step 2: 确认失败** → **Step 3: 实现**（纯薄封装，无逻辑）→ **Step 4: 通过** `cd frontend && npm test -- eval.test`
- [ ] **Step 5: 前端全量 + Commit**：`cd frontend && npm test`；commit `feat: 闭环评测前端 API 模块`

---

### Task 8: 页面骨架（表单/进度/历史）+ 路由菜单

**Files:**
- Create: `frontend/src/pages/ClosedEval.tsx`、`frontend/src/pages/ClosedEval.test.tsx`
- Modify: `frontend/src/App.tsx`（AdminRoute 内挂 `path="eval-closed"`）
- Modify: `frontend/src/pages/Layout.tsx`（`ADMIN_MENU_ITEMS` 追加 `{ key: '/eval-closed', label: '闭环评测' }`；头部注释「九项」改「十项」）
- Modify: `frontend/src/App.test.tsx`（**菜单数断言 9 → 10**，补 `闭环评测` menuitem 断言；reviewer 7 不变）——不改这个既有用例会假红

**Interfaces:**
- Consumes: Task 7 全部
- Produces: `ClosedEval` 页组件（默认导出）：挂载时 `listDatasets()` + `listRuns()`；表单（数据集 Select——选项 label `${id}（交集 ${library_overlap}）`、影片集 Radio+custom 时 Select multiple showSearch（选项来自 `listMovies`）、θ/K 逗号分隔 Input（placeholder 显示默认档）、并发 InputNumber 默认 4）；提交 → `startRun` → `setInterval` 2s 轮询 `getRun` 到终态（卸载/终态清定时器）→ Progress（done/total）+ 取消按钮（`cancelRun`）；历史 Table（列：时间/数据集/状态 Tag/完成/失败/nDCG@10/P@10/R@20/覆盖中位，null → 暂无）；**活跃 run 时起跑按钮 disabled + 提示**；行点击 → `getReport`（结果传给 Task 9 的报告组件位）

- [ ] **Step 1: 写失败测试**（vi.mock `../api/eval`；含 `test_start_disabled_while_active`——Review Focus 6 的前端半边：mock getRun 返回 running → 按钮禁用；提交断言 startRun 收到默认 θ/K 数组；轮询推进 running→succeeded；历史表行渲染、暂无 占位）
- [ ] **Step 2: 确认失败** → **Step 3: 实现** → **Step 4: 通过** `cd frontend && npm test -- ClosedEval.test`
- [ ] **Step 5: 前端全量（App.test 十项菜单过）+ Commit**：`cd frontend && npm test`；commit `feat: 闭环评测页签（表单/进度/历史，admin 菜单第十项）`

---

### Task 9: 报告视图组件（含 BarChart）

**Files:**
- Create: `frontend/src/components/ClosedEvalReport.tsx`、`frontend/src/components/ClosedEvalReport.test.tsx`
- Modify: `frontend/src/pages/ClosedEval.tsx`（历史行点击/跑完后渲染 `<ClosedEvalReport report={...} />`）

**Interfaces:**
- Consumes: Task 7 `ClosedEvalReport` 类型、既有 `BarChart`（`BarItem{label, value, max?, extra?}`）
- Produces: `ClosedEvalReport({ report }: { report: ClosedEvalReport })`——视图自上而下：① Alert.warning（banner 存在时）+ Alert.warning（warning 存在时）② 汇总 Statistic 卡（nDCG@10 / P@10 / R@20 / 覆盖中位 / Spearman 宏；从 report.grid 取——k 不在本次档位 → 暂无）③ θ×K Table（列：θ / K / P（±CI）/ R（±CI）/ nDCG（±CI）/ 随机 P / 随机 R）④ 基线对照 Table（行：系统 / 先验 / 随机；列：P@10 / R@20 / nDCG@10）⑤ 覆盖分布 BarChart（5 桶 `[0,0.2)…[0.8,1.0]` 计数）+ min/中位/max 文本 ⑥ Spearman 宏值 + 每片 Collapse 表 ⑦ 失败影片列表（tt+error，无则不渲染）⑧ 下载链接（report.md / metrics.csv / raw_results.json，href 用 `fileUrl`）⑨ Alert.info 银标准 note + calibre 小字。null 一律「暂无」

- [ ] **Step 1: 写失败测试**（构造 report 夹具：banner 有/无两份、grid 含 θ=0.4&k=10 行、coverage values 命中各桶、failed_movies 一行；断言汇总卡数值、表格行数、`bar-row-*` testid、下载 href、部分运行 banner 文案、暂无 占位）
- [ ] **Step 2: 确认失败** → **Step 3: 实现** → **Step 4: 通过** `cd frontend && npm test -- ClosedEvalReport.test`
- [ ] **Step 5: 前端全量 + Commit**：`cd frontend && npm test`；commit `feat: 闭环评测报告视图（θ×K 表/基线对照/覆盖分布/下载）`

---

## 执行后（非本计划任务）

部署到 9.15.154.212 沿既有流程（deploy-remote 工具链 + 服务器 .env 增 `TAGPLT_EVAL_DATASET_ROOT=/cache/MovieTags`），起跑试点 30 部验证墙钟预算——这两步属运维会话，不在代码计划内。
