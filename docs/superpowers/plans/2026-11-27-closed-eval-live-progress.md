# 闭环评测实时进度明细实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 闭环评测运行中/中断的 run 可打开「进度明细视图」——运行横幅 + 整块逐片明细表（θ 门、整体漏斗、逐片 trace、llm-raw 下载）2s 轮询边跑边长，转终态自动切报告视图。

**Architecture:** 照抄开放评测 live.json 模式：收敛循环每完成一片把已完成 results + job.gt 组装成与 raw_results.json 严格同形的行集，tmp+replace 原子写 `live.json`；`_movies_read` 回落序 raw_results → live.json → 404（detail/llm-raw 经同一读侧自动随动）；终态写 raw 后删 live。前端页面既有 2s 轮询驱动 `refreshTick` prop 让明细表重拉。

**Tech Stack:** Python 3.x + FastAPI（后端）、React 18 + antd v6 + vitest（前端）、pytest。

**Spec:** `docs/superpowers/specs/2026-11-27-closed-eval-live-progress-design.md`（已批准；含三拍板：看什么=逐片结果+指标、入口=独立视图、interrupted 也进明细）

## Global Constraints

- DB 零写入红线：live.json 只写 `eval_output_root/<run_id>/`（评测产物全磁盘纪律）。
- 生产路径零改动：producer/execute.py 不动；本轮只动 eval 服务写侧、读侧、前端。
- 报告端点不动：`get_run_report` 仍只读 report.json（聚合指标/banner 留终态）。
- live 行集与 raw_results.json **严格同形**：两处共用单一行构造 helper（`_result_rows`），键序/展开保持不动。
- live 发布失败只记日志（`logger.exception`），不影响评测终态与产物（open 同款容错）。
- 原子写：`live.json.tmp` + `replace`；终态在 raw 落盘后 `unlink(missing_ok=True)` 删 live。
- None≠0「暂无」：老 run / 无 live.json 的中断 run → movies 仍 404（现有口径不回退）。
- 前端轮询纪律：refreshTick 重拉**带当前已选 θ 参数**、不重置 θ/展开态/明细缓存（行只增不改）；重拉失败静默（旧表保留，初次失败才出 error Alert）。
- 后端测试命令一律从 worktree 根：`PYTHONPATH=src python -m pytest ...`（worktree 共享主 checkout 的 venv，editable 解析到主 src——缺 PYTHONPATH 会静默测错代码）；前端 `cd frontend && npx vitest run ...`。
- 提交信息中文 conventional 前缀（feat/test/docs），每任务至少一个 commit。

## Review Focus

1. **起跑空窗**：起跑后首片完成前 live.json=`[]` → movies 200 + `rows==[]`（不 404 不 500——进度明细从 t=0 可开）。测试 → T1 Step 7。
2. **写序窗口优先级**：raw_results.json 与 live.json 并存（终态写序瞬窗）→ raw 胜出，部分数据不污染终态口径。测试 → T1 Step 7（funnel null 断言区分两源）。
3. **损坏/截断 live**：非法 JSON → 按缺失容错 404（不 500——读侧宁缺勿崩）。测试 → T1 Step 7。
4. **前端轮询容错**：live 轮询中 getRunMovies 失败 → 旧表保留、无 error Alert（下一 tick 静默重试）。测试 → T2 Step 1。
5. **用户交互不被轮询冲掉**：live 视图已选 θ 门 + 展开行在 refreshTick 重拉后保留（重拉带当前 θ 参数、不收起展开）。测试 → T2 Step 1。

---

### Task 1: 后端——live.json 增量发布 + 读侧回落

**Files:**
- Modify: `src/tagplatform/services/eval/service.py`（start 骨架 381-392 / `_converge` 挂点 655-657 / `_record_result` 后新增两方法 740-758 / `_write_artifacts` 760-795）
- Modify: `src/tagplatform/orchestration/eval.py:396-430`（`_movies_read`）
- Modify: `docs/eval.md`（产物清单 + 新节「实时进度明细」）
- Test: `tests/test_closed_eval.py`（文末新节「实时进度 live.json」）、`tests/test_api_eval.py`（llm-raw 节后新节「实时进度明细（live.json 回落）」）

**Interfaces:**
- Consumes: 既有 `_dump_json`（report.py 导入）、`_EvalJob.gt`、`MovieResult` 字段（tt/title/ok/error/produced/elapsed_ms/trace）。
- Produces（T2 依赖的行为契约，非代码接口）: `GET /runs/{id}/movies` 及 `/{tt}`、`/{tt}/llm-raw` 对「有 live.json 无 raw_results.json」的 run 照常 200 出数（行集同形 → summarize/funnel/trace/守卫全链路不变）；空 live.json → 200 `rows==[]`。

- [ ] **Step 1: 写失败测试（服务层——live.json 生命周期）**

在 `tests/test_closed_eval.py` 文末加节。门 LLM 制造稳定观测窗（gate 第二片：第一片自由完成 → live 停留 1 行直到放行）：

```python
# ---------------------------------------------------------------- 实时进度 live.json


class _GateLLM(MockLLMClient):
    """门 LLM：指定影片的调用阻塞至事件置位（其余照常）——live.json
    中途观测的确定性缝隙。gate 第二片，让第一片的完成态可被稳定轮询到。"""

    def __init__(self, tt: str, event: threading.Event):
        super().__init__()
        self._suffix = f":{tt}"
        self._event = event

    def complete(self, messages, run_id):
        if run_id.endswith(self._suffix):
            assert self._event.wait(10), "gate 10s 未放行"
        return super().complete(messages, run_id)


def _read_live(service, run_id) -> list | None:
    try:
        return json.loads((_run_dir(service, run_id) / "live.json")
                          .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _await_live(service, run_id, n_rows, timeout=10.0):
    """轮询 live.json 直到行数 ≥ n_rows（gate 保证窗口稳定不闪失）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = _read_live(service, run_id)
        if isinstance(rows, list) and len(rows) >= n_rows:
            return rows
        time.sleep(0.01)
    pytest.fail(f"live.json 未在 {timeout}s 内达到 {n_rows} 行")


def test_live_json_lifecycle(eval_setup):
    """生命周期：起跑同步写空集 → 每完成一片增量发布（与 raw_results 同形：
    gt 并入、produced 五键、成功片带 trace 键）→ 终态删除（raw 为正典、
    tmp 无残留）。concurrency=1 + gate 第二片。"""
    gate = threading.Event()
    svc = eval_setup(llm=_GateLLM("tt0000002", gate))
    run_id = svc.start("emotion", {"mode": "all"}, concurrency=1)
    assert _read_live(svc, run_id) == []          # start 返回即有（不依赖调度）
    rows = _await_live(svc, run_id, 1)
    assert rows[0]["tt"] == "tt0000001"
    assert rows[0]["gt"] == {"t1": 0.9, "t2": 0.4, "t7": 0.1, "t9": 0.8}
    assert all(set(p) == {"tag_id", "tag_name", "sf", "confidence",
                          "evidence_count"} for p in rows[0]["produced"])
    assert "trace" in rows[0]                     # MOCK 成功片带 trace
    gate.set()
    _await_terminal(svc, run_id)
    d = _run_dir(svc, run_id)
    assert not (d / "live.json").exists() and not (d / "live.json.tmp").exists()
    raw = _raw_rows(svc, run_id)
    assert len(raw) == 2 and all(r["ok"] for r in raw)


def test_live_publish_failure_tolerated(eval_setup, monkeypatch):
    """live 发布失败只记日志：run 照常 succeeded、终态产物完整（旁路产物
    容错口径——spec）。只对 live.json.tmp 注入 OSError。"""
    import tagplatform.services.eval.service as svc_mod
    real_dump = svc_mod._dump_json

    def _boom(path, payload):
        if str(path).endswith("live.json.tmp"):
            raise OSError("磁盘满（注入）")
        return real_dump(path, payload)

    monkeypatch.setattr(svc_mod, "_dump_json", _boom)
    svc = eval_setup()
    run_id = svc.start("emotion", {"mode": "all"}, wait=True)
    assert svc.get(run_id).status == "succeeded"
    d = _run_dir(svc, run_id)
    assert (d / "raw_results.json").exists()
    assert len(_raw_rows(svc, run_id)) == 2
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=src python -m pytest tests/test_closed_eval.py -k "live" -v`
Expected: FAIL（`_read_live` 返回 None → `== []` 断言失败）。`json`/`threading`/`time` 均已在文件头 import，无需新增。

- [ ] **Step 3: 实现写侧（service.py）**

- 新增静态方法 `_result_rows(job: _EvalJob, results: list[MovieResult]) -> list[dict]`：把 `_write_artifacts` 785-788 的行构造**原样上提**（`{"tt", "title", "ok", "error", "produced", "gt": job.gt.get(r.tt, {}), "elapsed_ms", **({"trace": r.trace} if r.trace is not None else {})}`，键序与 trace 条件展开不动），docstring 注明「raw_results.json / live.json 共用行构造（同形红线）」。
- 新增 `_publish_live(self, job: _EvalJob, results: list[MovieResult]) -> None`：`_dump_json(run_dir / "live.json.tmp", self._result_rows(job, results))` → `temporary.replace(run_dir / "live.json")`；整体 try/except `Exception` → `logger.exception("live.json 发布失败 %s", job.run_id)`（docstring：增量发布部分行集，worker 单线程调用无锁竞争，发布失败不拖垮评测）。
- start() 骨架块（meta 落盘后、线程启动前）：`_dump_json(run_dir / "live.json", [])`（空集——读侧 t=0 起统一口径）。
- `_converge` 657 行 `self._record_result(job.run_id, result)` 之后加 `self._publish_live(job, results)`（与 progress.jsonl 同位——停止后到达的完成片照发）。
- `_write_artifacts`：rows 构造换 `rows = self._result_rows(job, results)`；`_dump_json(run_dir / "raw_results.json", rows)` 之后加 `(run_dir / "live.json").unlink(missing_ok=True)`（终态清理，注释注明 raw 优先级已盖过 live、删是为目录语义干净）。

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONPATH=src python -m pytest tests/test_closed_eval.py -v`
Expected: PASS（38 旧 + 2 新）。

- [ ] **Step 5: 写失败测试（API 层——读侧回落）**

在 `tests/test_api_eval.py` 的 llm-raw 节后加节（复用 `_movies_rows`/`_trace_rows`/`_FUNNEL`/`_write_llm_raw` 夹具）：

```python
# ---------------------------------------------------------------- 实时进度明细（live.json 回落）


def _write_live_run(client, *, run_id="run-live", live_rows=None, raw_rows=None,
                    live_text=None):
    """手写一个运行中形态的 run：meta(running) + live.json；raw_rows 给出
    则另写 raw_results.json（优先级用例）；live_text 给出则原样写（损坏用例）。"""
    out = Path(client.app.state.settings.eval_output_root)
    d = out / run_id
    d.mkdir(parents=True)
    (d / "meta.json").write_text(
        json.dumps({"run_id": run_id, "status": "running", "done": 0, "total": 2,
                    "failed": 0, "dataset": {"id": "emotion"},
                    "thetas": [0.2, 0.4, 0.6], "ks": [5, 10, 20],
                    "started_at": "2026-11-27T10:00:00+00:00"},
                   ensure_ascii=False), encoding="utf-8")
    if live_text is not None:
        (d / "live.json").write_text(live_text, encoding="utf-8")
    elif live_rows is not None:
        (d / "live.json").write_text(
            json.dumps(live_rows, ensure_ascii=False), encoding="utf-8")
    if raw_rows is not None:
        (d / "raw_results.json").write_text(
            json.dumps(raw_rows, ensure_ascii=False), encoding="utf-8")
    return d


def test_movies_falls_back_to_live(client):
    """运行中 run（仅 live.json 无 raw）：movies 回落出数（失败置顶 + trace
    夹具十键）、θ 门 query 照常 400、detail 回落、llm-raw 守卫随动（tt 在
    live 行 + 文件在 → 200）。"""
    _seed_dataset_and_library(client)
    d = _write_live_run(client, live_rows=_trace_rows())
    r = client.get(f"{_BASE}/runs/run-live/movies")
    assert r.status_code == 200
    body = r.json()
    assert [row["tt"] for row in body["rows"]] == ["tt0000002", "tt0000001"]
    assert body["rows"][1]["funnel"] == _FUNNEL
    bad = client.get(f"{_BASE}/runs/run-live/movies", params={"theta_p": 0.3})
    assert bad.status_code == 400
    detail = client.get(f"{_BASE}/runs/run-live/movies/tt0000001").json()
    assert detail["trace"]["counts"] == _FUNNEL
    _write_llm_raw(d)
    r2 = client.get(f"{_BASE}/runs/run-live/movies/tt0000001/llm-raw")
    assert r2.status_code == 200 and 'filename="tt0000001.json"' in \
        r2.headers["content-disposition"]


def test_movies_raw_priority_and_empty_and_corrupt_live(client):
    """raw 与 live 并存 → raw 胜出（live 的 trace 被盖 → funnel null）；空
    live=[] → 200 rows=[]（起跑态不 404）；损坏 live → 按缺失容错 404。"""
    _seed_dataset_and_library(client)
    # 优先级：raw 行无 trace → funnel null（若误读 live 会是 _FUNNEL dict）
    _write_live_run(client, run_id="run-prio", live_rows=_trace_rows(),
                    raw_rows=_movies_rows())
    body = client.get(f"{_BASE}/runs/run-prio/movies").json()
    assert all(row["funnel"] is None for row in body["rows"])
    # 空集：起跑态
    _write_live_run(client, run_id="run-empty", live_rows=[])
    empty = client.get(f"{_BASE}/runs/run-empty/movies")
    assert empty.status_code == 200 and empty.json()["rows"] == []
    # 损坏：按缺失 → 404（不 500）
    _write_live_run(client, run_id="run-corrupt", live_text='{"rows": [')
    corrupt = client.get(f"{_BASE}/runs/run-corrupt/movies")
    assert corrupt.status_code == 404 and "无单片明细" in corrupt.json()["detail"]
```

- [ ] **Step 6: 跑测试确认失败**

Run: `PYTHONPATH=src python -m pytest tests/test_api_eval.py -k "live" -v`
Expected: FAIL（movies 404——回落未实现）。

- [ ] **Step 7: 实现读侧（orchestration/eval.py `_movies_read`）**

raw 读取段（413-422）换成回落循环（镜像 open 报告端点 776 的 `for name in (...)` 模式）：

```python
    rows = None
    # 回落序：raw_results.json（终态正典）→ live.json（运行中增量发布的
    # 部分行集——实时进度明细：与 raw 同形，detail/llm-raw 经本读侧
    # 自动随动）；各源按缺失/截断/形状容错，首个合法源胜出。
    for name in ("raw_results.json", "live.json"):
        try:
            candidate = json.loads((run_dir / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(candidate, list) and all(
                isinstance(r, dict) and "tt" in r for r in candidate):
            rows = candidate
            break
    if rows is None:
        raise HTTPException(status_code=404,
                            detail=f"该 run 无单片明细（未完成或产物缺失）：{run_id!r}")
```

docstring 增一行说明回落序（原「运行中 run 与 meta→raw 写序非原子窗口的既定容错」句保留）。

- [ ] **Step 8: 跑测试确认通过 + 后端相关面回归**

Run: `PYTHONPATH=src python -m pytest tests/test_api_eval.py tests/test_closed_eval.py tests/test_movie_breakdown.py -v`
Expected: PASS（含既有 `test_movies_run_dir_guard` 等 404 口径用例——老 run 无 live 不回退）。

- [ ] **Step 9: docs/eval.md 增「实时进度明细」节**

产物清单加 `live.json`；五事实：起跑写空集 / 每片完成原子增量发布（同 raw 形）/ 终态 raw 落盘后删除、中断残留即部分明细 / 读侧回落序 raw→live→404（detail 与 llm-raw 随动）/ 前端 running/interrupted 行点击与活跃卡入口（视图行为下一任务交付，本节先留一句「前端入口见下」）。commit 与 Step 8 后一并进行。

- [ ] **Step 10: Commit**

```bash
git add src/tagplatform/services/eval/service.py src/tagplatform/orchestration/eval.py tests/test_closed_eval.py tests/test_api_eval.py docs/eval.md
git commit -m "feat: 闭环评测实时进度——live.json 增量发布与读侧回落"
```

（若按写侧/读侧分两 commit：Step 4 后先提 `feat: 闭环评测 live.json 增量发布（写侧）`，Step 9 后提读侧+docs。）

---

### Task 2: 前端——进度明细视图（路由 + 横幅 + refreshTick 轮询）

**Files:**
- Modify: `frontend/src/components/ClosedEvalMovieTable.tsx:447`（签名）、`474-496` 后（新增 refresh 效果）
- Modify: `frontend/src/pages/closed-eval/ClosedEval.tsx`（state 185-201 / 轮询 232-258 / openReport 447-459 / 活跃卡 606-629 / 历史表 onRow 641-651 / 视图渲染 654-658）
- Test: `frontend/src/components/ClosedEvalMovieTable.test.tsx`（新用例）、`frontend/src/pages/closed-eval/ClosedEval.test.tsx`（新用例 + 模块级 mock 明细表）

**Interfaces:**
- Consumes: T1 的端点行为契约（live 回落）；既有 `getRunMovies(runId, thetaArgs?)`、`RunStatus`/`RunSummary` 字段（run_id/status/done/total/failed）、`STATUS_LABELS`/`handleCancel`/`refreshRuns`。
- Produces: `ClosedEvalMovieTable` 组件签名 `{ runId: string; refreshTick?: number }`——`refreshTick` 变化即带当前 θ 参数静默重拉（不重置 θ/展开/明细缓存，失败静默）；`undefined` 时行为与旧版逐字节相同。

- [ ] **Step 1: 写失败测试（明细表 refreshTick）**

`ClosedEvalMovieTable.test.tsx` 追加两用例（复用既有 SUMMARY 夹具与 `mockedGetRunMovies`）：

```tsx
it('refreshTick 变化即重拉：带当前 θ 参数、展开与 θ 选择保留、旧表不闪', async () => {
  // mockedGetRunMovies 首次回 SUMMARY；切 θ_p 后 rerender refreshTick 变化
  // → 第二次调用参数带 { p: 0.4 }；第二次回 SUMMARY（行不变）→ 展开的
  // 明细行不收起、getRunMovieDetail 不重复请求（缓存命中）
})
it('refreshTick 重拉失败静默：旧表保留、无 error Alert、无 theta-refetch-error', async () => {
  // mockedGetRunMovies 第二次 reject → payload 仍是首次值、
  // screen.queryByTestId('summary-error') 之类不存在
})
```

（断言要点已列全；`rerender` 用法同既有「runId 切换」回归用例 518-524 行模式。）

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend && npx vitest run src/components/ClosedEvalMovieTable.test.tsx`
Expected: FAIL（`refreshTick` prop 不存在——rerender 不触发第二次 getRunMovies）。

- [ ] **Step 3: 实现明细表 refreshTick**

- 签名：`export default function ClosedEvalMovieTable({ runId, refreshTick }: { runId: string; refreshTick?: number }): JSX.Element`。
- 挂载效果后新增效果（依赖 `[refreshTick, runId]`）：`refreshTick === undefined` 时 no-op；否则 `genRef.current += 1` 取代际，`getRunMovies(runId, thetaArgs)` 成功且代际匹配 → `setPayload(p)`，失败**静默**（注释：轮询失败旧表保留，下一 tick 重试；初次失败由挂载效果出 summaryError——两效果职责分离）。**不**清 θ/展开/明细缓存（行只增不改；θ 未变缓存仍有效）。θ 参数用既有 `thetaArgs` memo（用户已选门在轮询中保留）。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd frontend && npx vitest run src/components/ClosedEvalMovieTable.test.tsx`
Expected: PASS。

- [ ] **Step 5: 写失败测试（页面——路由/横幅/轮询/自动切换）**

`ClosedEval.test.tsx`：模块级加 `vi.mock('../../components/ClosedEvalMovieTable', () => ({ default: vi.fn(() => <div data-testid="movie-table-stub" />) }))`（真实组件经 api mock 拉数，页面层只需断言挂载与 props；`vi.mocked(ClosedEvalMovieTable)` 取 `mock.calls.at(-1)[0]` 断言 runId/refreshTick）。文件头注释补两行本轮覆盖说明。新用例（复用 `mockIdleApis`、RUNS 夹具、fake timers 轮询模式 531-560）：

```tsx
it('running 行点击 → 进度明细视图：横幅（进行中·已完成 X/Y·每 2 秒自动刷新）+ 明细表挂载', async () => {
  // listRuns 一行 running（done 1/total 3）→ 挂载发现活跃（getRun 接管轮询）
  // → 点 running 行 → live-detail 卡片在、movie-table-stub 在、
  // 明细表 props.runId === 该 run
})
it('轮询驱动 refreshTick：advance 2s → getRun 再调、明细表末次 props.refreshTick 自增', async () => {
  // fake timers：两次 advanceTimersByTimeAsync(2000) → refreshTick 0→1→2
})
it('轮询推进终态 → 进度明细自动切报告视图（live 收起、getReport 调用、报告渲染）', async () => {
  // getRun 首次 running、第二次 succeeded；getReport mockResolvedValue(REPORT)
  // → live-detail 不在、报告卡在
})
it('interrupted 行点击 → 进度明细（横幅「已中断」、不轮询：advance 2s 无新 getRun、refreshTick 恒 undefined）', async () => {
  // 复用 RUNS 夹具的 interrupted 行（挂载不接管轮询——无活跃 run）
})
it('活跃卡「查看进度明细」按钮 → 进度明细视图；报告与明细互斥（明细开着点终态行 → 报告替换）', async () => {
  // activeRun 卡片按钮可点（按钮名宽容正则 /查看进度明细/）；
  // 互斥：开明细 → 点 succeeded 行 → 报告卡在、live-detail 不在
})
```

- [ ] **Step 6: 跑测试确认失败**

Run: `cd frontend && npx vitest run src/pages/closed-eval/ClosedEval.test.tsx`
Expected: FAIL（live-detail 不存在）。

- [ ] **Step 7: 实现页面进度明细视图**

- **state/refs**（report state 旁）: `const [liveRun, setLiveRun] = useState<{ runId: string } | null>(null)`、`const liveRunRef = useRef<{ runId: string } | null>(null)`（轮询闭包读最新值）、`const [liveTick, setLiveTick] = useState(0)`。
- **openLive/closeLive**（`useCallback`，零依赖只碰 setter/ref）: `openLive(runId)` 置 ref+state 并 `setReport(null)`（视图互斥）；`closeLive()` 清两者。`openReport` 改 `useCallback` 并在成功路径前加 `closeLive()`；`handleDelete` 的收起口径补 live（`liveRunRef.current?.runId === runId` 时 closeLive）。
- **轮询 tick 改造**（`beginPolling` 闭包内）: else 分支 `setActiveRun(st)` 后加 `if (liveRunRef.current?.runId === runId) setLiveTick((n) => n + 1)`；终态分支加 `if (liveRunRef.current?.runId === runId) { setLiveTick((n) => n + 1); closeLive(); void openReport(runId) }`（末次刷新让终态 raw 全量入表后收起切报告；getReport 404 写序窗口沿用既有「报告生成中，稍后重试」提示——`beginPolling` deps 补 `openReport`）。
- **onRow 路由**: `row.status === 'running' || row.status === 'interrupted'` → `openLive(row.run_id)`，其余 `void openReport(row.run_id)`（原样）。
- **活跃卡**: `extra` 换 `Space`——「查看进度明细」（`onClick={() => openLive(activeRun.run_id)}`）+ 原取消按钮。
- **横幅派生**: `const liveBanner = liveRun === null ? null : liveRun.runId === activeRun?.run_id ? activeRun : runs.find((r) => r.run_id === liveRun.runId) ?? null`。
- **视图渲染**（report 块后）:

```tsx
{liveRun !== null && (
  <Card title={`进度明细：${liveRun.runId}`} data-testid="live-detail">
    {liveBanner !== null && (
      <Typography.Text>
        {`${STATUS_LABELS[liveBanner.status] ?? liveBanner.status} · 已完成 ${liveBanner.done ?? '—'} / ${liveBanner.total ?? '—'}`}
        {liveBanner.status === 'running' ? ' · 每 2 秒自动刷新' : ''}
      </Typography.Text>
    )}
    <ClosedEvalMovieTable
      runId={liveRun.runId}
      refreshTick={liveRun.runId === activeRun?.run_id ? liveTick : undefined}
    />
  </Card>
)}
```

（interrupted 打开时 refreshTick 恒 undefined——不轮询；卡片内竖向间距用既有 `Space direction="vertical"` 或容器 gap 模式。）

- [ ] **Step 8: 跑测试确认通过 + 前端全量回归**

Run: `cd frontend && npx vitest run src/pages/closed-eval/ClosedEval.test.tsx` → PASS；
Run: `cd frontend && npx vitest run` → 全绿（明细表 mock 不影响报告视图既有断言——其不断言表内部）。

- [ ] **Step 9: docs/eval.md 补视图行为 + Commit**

「实时进度明细」节补前端行为句（三入口/互斥/终态自动切报告/不轮询口径）。

```bash
git add frontend/src/components/ClosedEvalMovieTable.tsx frontend/src/pages/closed-eval/ClosedEval.tsx frontend/src/components/ClosedEvalMovieTable.test.tsx frontend/src/pages/closed-eval/ClosedEval.test.tsx docs/eval.md
git commit -m "feat: 闭环评测进度明细视图——路由/横幅/refreshTick 轮询与终态自动切报告"
```

---

## 收尾（双栈全量回归）

- [ ] `PYTHONPATH=src python -m pytest -q` 与 `cd frontend && npx vitest run` 双栈全绿后，进入 finishing-a-development-branch。
