# 闭环评测 run 生命周期完善 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (用户已选 Native 直执行). Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 评测 run 历史可运营：透出结束时间、支持删除终态 run，并根治「评测并发占死 DB 连接池拖垮平台 API」的事故。

**Architecture:** 三处后端小改（closed_eval.py 会话释放/时间透出/删除方法 + router 新端点 + closed.py 清 print）加一处前端改动（历史表列 + 删除按钮 + 搜索双匹配）。closed producer 的 produce 契约签名不动（eval 传 None，正式执行器照常传真会话）。

**Tech Stack:** FastAPI + SQLAlchemy（QueuePool）+ antd v6 + vitest。

**Spec:** `docs/superpowers/specs/2026-11-21-eval-run-lifecycle-design.md`（已批准）。

## Global Constraints

- 分支 `feat/eval-run-lifecycle`（base a849782，已切好）；不要动 `feat/closed-loop-eval`（PR #30 开着）。
- 后端测试命令（cwd=worktree 根）：`PYTHONPATH=src C:/code/research-topics/movie-tag-research/intelligent-tag-platform/.venv/Scripts/python.exe -m pytest <目标> -v`。
- 前端测试命令：`cd frontend && npx vitest run src/pages/ClosedEval.test.tsx`；flake 规则：整套失败但该文件隔离跑通过 = 已知负载 flake，不阻断（不与后端测试并发跑）。
- 评测 DB 零写入红线不变：删除只允许动 `settings.eval_output_root` 下本 run 目录。
- run_id 一切路径拼接必须先 resolve+relative_to 守卫（`..`/绝对路径/分隔符变体按不存在处理）。
- 前端 null 口径统一「暂无」（None≠0）；admin 端点全 `require_admin`。
- 提交信息中文 conventional（`feat:`/`fix:`/`test:`）。

## Review Focus

1. **池饿死回归**：LLM 调用期间持有 DB 会话（事故根源，任何并发都会复现）——Task 1 红测 `test_llm_calls_hold_zero_db_sessions` 钉死。
2. **终态落盘窄窗口删 run**：注册表 status 已翻终态但 worker 还在写盘（active 位未释放）——Task 3 服务层守卫用 `_active_run_id`（不是 status），API 测试对 409→200 用短轮询重试钉死该语义。
3. **run_id 路径越界**：`../outside` 形态直删文件系统任意目录——Task 3 `test_delete_path_traversal_returns_false` 钉死。
4. **报告视图开着时删除该 run**：报告卡片悬空展示已删 run——Task 4 测试钉死（删除后卡片收起）。
5. **搜索大小写**：用户输入大写 `TT0000002` 应命中（filterOption 小写化）——Task 4 测试钉死。

---

### Task 1: 池饿死根治——produce 全程零 DB 会话 + print 清理

**Files:**
- Modify: `src/tagplatform/closed_eval.py:787-819`（`_run_movie`）
- Modify: `src/tagplatform/generation/closed.py:125,131`（两个 print）
- Test: `tests/test_closed_eval.py`

**Interfaces:**
- Consumes: 现有 `eval_setup` 夹具工厂（`_make(llm=, dataset_root=)`，test_closed_eval.py:289）。
- Produces: 无新对外接口；`_make` 增可选参 `session_factory=None`（缺省现行为）。

- [ ] **Step 1: 扩 `_make` 并写红测（LLM 调用期间零开放会话）**

`eval_setup` 的 `_make` 签名加 `session_factory=None`（None 时维持 `make_session_factory(engine)`），加进 test_closed_eval.py：

```python
class _CountingSessionFactory:
    """会话计数工厂（池饿死回归探针）：包装真实工厂，进出 with 计数。"""

    def __init__(self, inner):
        self._inner = inner
        self.open_sessions = 0

    def __call__(self):
        return _CountingSession(self._inner(), self)


class _CountingSession:
    """计数会话：属性透传内层（assemble_spec 的 get_active_prompt 可用）。"""

    def __init__(self, inner, factory):
        self._inner = inner
        self._factory = factory

    def __enter__(self):
        self._factory.open_sessions += 1
        return self._inner

    def __exit__(self, *exc):
        self._factory.open_sessions -= 1
        return self._inner.__exit__(*exc)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _SessionProbeLLM(MockLLMClient):
    """LLM 桩：complete() 时记开放会话数（violation 计数，不中断跑）。"""

    def __init__(self, factory):
        super().__init__()
        self._factory = factory
        self.violations = 0

    def complete(self, messages, run_id):
        if self._factory.open_sessions > 0:
            self.violations += 1
        return super().complete(messages, run_id)


def test_llm_calls_hold_zero_db_sessions(eval_setup, engine):
    """池饿死事故回归（2026-11-21 run-081903，并发 16 > 池 5+10）：
    LLM 调用期间不得持有 DB 会话——produce 含全部 LLM 调用（分钟级），
    会话横跨它就会在高并发下占死 QueuePool、拖垮全部平台 API。"""
    from tagplatform.db import make_session_factory

    factory = _CountingSessionFactory(make_session_factory(engine))
    llm = _SessionProbeLLM(factory)
    service = eval_setup(llm=llm, session_factory=factory)
    run_id = service.start("emotion", {"mode": "all"}, wait=True)
    assert service.get(run_id).status == "succeeded"
    assert service.get(run_id).failed == 0
    assert llm.violations == 0
```

- [ ] **Step 2: 跑红**

Run: `PYTHONPATH=src <主检出>/.venv/Scripts/python.exe -m pytest tests/test_closed_eval.py::test_llm_calls_hold_zero_db_sessions -v`
Expected: FAIL（`assert llm.violations == 0` 不成立——当前代码会话横跨 produce，violations ≥ 1；若 failed>0 先查探针是否被 except 吞，但 violations 计数不依赖异常路径）。

- [ ] **Step 3: 修 `_run_movie`——assemble 后立即还会话，produce 传 None**

`with self._session_factory() as session:` 块收窄到 load+assemble；`produce` 挪到块外，第二参传 `None` 并加注释（设计文档方案 A：closed producer 不消费 session；若未来改用会话将 AttributeError 显式失败）。重试循环/`_produced_rows`/返回值结构不变。

- [ ] **Step 4: 跑绿 + 全量后端**

Run: `pytest tests/test_closed_eval.py::test_llm_calls_hold_zero_db_sessions -v` → PASS；再 `pytest tests/test_closed_eval.py -v` 全绿。

- [ ] **Step 5: 删 closed.py:125/131 两个 print（调试残留）**

直接删除两行（prompt/response 全文打印——全量 run 写 GB 级日志；组级 debug logger 已存在于 :137）。

- [ ] **Step 6: 门禁 + 提交**

Run: `pytest tests/test_generation_closed.py tests/test_llm_client.py -v` 全绿（producer 行为无回归）。
Commit: `fix: 评测 produce 全程零 DB 会话（池饿死根治）+ 清 print 调试残留`

### Task 2: finished_at 透出（服务层 + API 层）

**Files:**
- Modify: `src/tagplatform/closed_eval.py`（RunState :379-389 / _worker :698-723 / history 行 :651-661）
- Modify: `src/tagplatform/api/routers/eval.py`（RunStateOut :114 / RunSummaryOut :127 / `_run_state_out` :180 / get_run 兜底 :287）
- Test: `tests/test_closed_eval.py`、`tests/test_api_eval.py`

**Interfaces:**
- Produces: `RunState.finished_at: str | None = None`（终态时刻 _now_iso，运行中 None）；history() 行键 `finished_at`（meta.get）；`RunStateOut.finished_at` / `RunSummaryOut.finished_at: str | None = None`。meta.json 终态写盘已有 finished_at（:855），不改。

- [ ] **Step 1: 写红测（服务层）**

```python
def test_finished_at_on_terminal_state_and_history(svc):
    """终态后：get() 快照与 history() 行都带 finished_at（ISO 可解析）。"""
    run_id = svc.start("emotion", {"mode": "all"}, wait=True)
    state = svc.get(run_id)
    assert state.finished_at is not None
    datetime.fromisoformat(state.finished_at)  # ISO 形态
    row = next(r for r in svc.history() if r["run_id"] == run_id)
    assert row["finished_at"] is not None
```

（`from datetime import datetime` 补 import。）

- [ ] **Step 2: 跑红**

Run: `pytest tests/test_closed_eval.py::test_finished_at_on_terminal_state_and_history -v`
Expected: FAIL（AttributeError: finished_at——RunState 无此字段）。

- [ ] **Step 3: 实现**

RunState 加字段 `finished_at: str | None = None`；`_worker` 在终态锁内（`state.status = status` 处）一并 `state.finished_at = _now_iso()`；history() 行加 `"finished_at": meta.get("finished_at"),`；router 两个 Out 模型加 `finished_at: str | None = None`、`_run_state_out` 搬运、get_run 磁盘兜底行搬运 `row["finished_at"]`。

- [ ] **Step 4: API 红转绿（并入既有用例断言）**

`test_api_eval.py` 的 `test_run_end_to_end` 加：终态 body `finished_at` 非 None；历史行 `mine["finished_at"]` 非 None。`test_get_run_disk_only_fallback` 的 meta（无 finished_at 键）断言 `body["finished_at"] is None`（老 run 兼容）。
Run: `pytest tests/test_api_eval.py -v` 全绿。

- [ ] **Step 5: 全量 + 提交**

Run: `pytest tests/test_closed_eval.py tests/test_api_eval.py -v` 全绿。
Commit: `feat: 评测 run 结束时间全链路透出（RunState/history/API 模型）`

### Task 3: 删除终态 run（服务层 + DELETE 端点）

**Files:**
- Modify: `src/tagplatform/closed_eval.py`（新 `RunActiveError` 异常 + `delete` 方法；`import shutil`）
- Modify: `src/tagplatform/api/routers/eval.py`（DELETE 端点；import RunActiveError）
- Test: `tests/test_closed_eval.py`、`tests/test_api_eval.py`

**Interfaces:**
- Consumes: RunBusyError 定义处（closed_eval.py 顶部异常区）作为新异常的同款位置与导出方式。
- Produces: `RunActiveError(Exception)`（运行中/worker 收尾中不可删）；`ClosedEvalService.delete(run_id: str) -> bool`（True 已删；False 不存在；RunActiveError 活跃中）；`DELETE /api/v1/eval/closed/runs/{run_id}` → 200 `{"ok": true}` / 409（活跃，detail 带原因）/ 404（不存在或已删）。

- [ ] **Step 1: 写红测（服务层 5 例）**

```python
def test_delete_terminal_run_removes_disk_and_registry(svc):
    run_id = svc.start("emotion", {"mode": "all"}, wait=True)
    _drain_active(svc)  # worker 完全收尾（active 位释放）
    assert svc.delete(run_id) is True
    assert svc.get(run_id) is None
    assert not _run_dir(svc, run_id).exists()
    assert all(r["run_id"] != run_id for r in svc.history())


def test_delete_active_run_refused(eval_setup):
    """运行中不可删（active 位守卫，含终态落盘窄窗口）。"""
    gate = _RecordingGateLLM()
    gate.hold()
    service = eval_setup(llm=gate)
    run_id = service.start("emotion", {"mode": "all"})
    try:
        with pytest.raises(RunActiveError):
            service.delete(run_id)
        assert _run_dir(service, run_id).exists()
    finally:
        gate.release()
    _await_terminal(service, run_id)
    _drain_active(service)


def test_delete_unknown_returns_false(svc):
    assert svc.delete("run-nope") is False


def test_delete_interrupted_disk_residual(svc):
    """进程死亡残留（磁盘 status=running、注册表无）= interrupted，可删。"""
    run_id = svc.start("emotion", {"mode": "all"}, wait=True)
    with svc._lock:
        svc._runs.pop(run_id)
    assert svc.delete(run_id) is True
    assert not _run_dir(svc, run_id).exists()


def test_delete_path_traversal_returns_false(svc):
    assert svc.delete("../outside") is False
```

（import 区补 `RunActiveError`。）

- [ ] **Step 2: 跑红**

Run: `pytest tests/test_closed_eval.py -k "delete" -v`
Expected: FAIL（`RunActiveError` 不存在 → ImportError）。

- [ ] **Step 3: 实现服务层 `delete`**

守卫序（全部在 `self._lock` 内判）：run_dir 先 resolve+relative_to eval_output_root（越界 → False，与 router `_run_dir` 404 同口径）；`self._active_run_id == run_id` → `RunActiveError`（**用 active 位不用 status 位**——worker 写盘收尾期 status 已终态但 active 未释放）；锁内清 `_runs`/`_cancel_events` 并记 `existed = 注册表有 or run_dir 存在`；锁外 `shutil.rmtree`（目录存在才删）。返回 `existed`。

- [ ] **Step 4: 跑绿 + API 红测**

Run: `pytest tests/test_closed_eval.py -k "delete" -v` → PASS。
test_api_eval.py 新增（并入 admin-only 列表第 11 项 `lambda: client.delete(f"{_BASE}/runs/{run_id}")`）：

```python
def test_delete_run_endpoint(client):
    _seed_dataset_and_library(client)
    run_id = _start_run(client).json()["run_id"]
    _await_terminal(client, run_id)
    _await_report(client, run_id)
    # 报告落盘后 active 位可能还有毫秒级释放窗口：短轮询到 200
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        r = client.delete(f"{_BASE}/runs/{run_id}")
        if r.status_code == 200:
            break
        time.sleep(0.01)
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert client.get(f"{_BASE}/runs/{run_id}").status_code == 404
    assert client.delete(f"{_BASE}/runs/{run_id}").status_code == 404  # 重复删 → 404
    assert all(row["run_id"] != run_id
               for row in client.get(f"{_BASE}/runs").json())


def test_delete_active_run_409(client):
    _seed_dataset_and_library(client)
    gate = _GateLLM()
    gate.hold()
    service = client.app.state.closed_eval
    original_llm = service._llm
    service._llm = gate
    try:
        run_id = _start_run(client).json()["run_id"]
        assert client.delete(f"{_BASE}/runs/{run_id}").status_code == 409
    finally:
        gate.release()
        service._llm = original_llm
    _await_terminal(client, run_id)
```

- [ ] **Step 5: 实现 router DELETE 端点**

`@router.delete("/runs/{run_id}")`，`require_admin` + `get_closed_eval` 依赖；`try: ok = service.delete(run_id)` 映射：`RunActiveError` → 409（detail=str(exc)）、`ok False` → 404（detail 同 GET 口径 `f"评测 run 不存在：{run_id!r}"`）、成功 → `{"ok": True}`。路由 docstring 注明只动 eval_output_root、DB 零写入红线不变。

- [ ] **Step 6: 全量 + 提交**

Run: `pytest tests/test_closed_eval.py tests/test_api_eval.py -v` 全绿。
Commit: `feat: 评测终态 run 删除（服务层 + DELETE 端点，运行中 409）`

### Task 4: 前端——结束时间列 / 删除按钮 / tt+标题双匹配

**Files:**
- Modify: `frontend/src/api/eval.ts`（RunStatus/RunSummary + finished_at；`deleteRun`）
- Modify: `frontend/src/pages/ClosedEval.tsx`（列、操作列、filterOption、handleDelete）
- Test: `frontend/src/pages/ClosedEval.test.tsx`

**Interfaces:**
- Consumes: Task 2 的 `finished_at: string | null` 字段；Task 3 的 `DELETE /runs/{run_id}`（200 `{ok:true}`/409/404）。
- Produces: `deleteRun(runId: string): Promise<{ ok: boolean }>`。

- [ ] **Step 1: 写红测（3 项：结束时间列、删除流、tt 搜索）**

夹具 `RUNS_HISTORY` 三行加 `finished_at`（succeeded `'2026-11-19T11:30:00'`、cancelled `'2026-11-18T10:05:00'`、interrupted 保持 `null`）——**同时**既有用例「行渲染状态 Tag」中 interrupted 行的 `getAllByText('暂无')).toHaveLength(8)` 同步改 9（fixture 加 null 字段后该行多一个「暂无」）。mock 清单加 `deleteRun: vi.fn()`（mock 模块与 `vi.mocked` 同款）。

```tsx
it('历史表渲染结束时间列：有值显示，null → 暂无', async () => {
  mockIdleApis()
  render(<ClosedEval />)
  const okRow = (await screen.findByText('2026-11-19 11:30:00')).closest('tr')!
  expect(okRow).toHaveTextContent('2026-11-19 11:30:00')
  // interrupted 行 finished_at null → 第 9 个「暂无」（原 8 个 +1）
  const interruptedRow = screen.getByText('已中断').closest('tr')!
  expect(within(interruptedRow).getAllByText('暂无')).toHaveLength(9)
})

it('删除终态 run：Popconfirm 确认 → deleteRun + 刷新历史 + 报告卡片收起', async () => {
  mockIdleApis()
  mockedGetReport.mockResolvedValue(REPORT)
  mockedDeleteRun.mockResolvedValue({ ok: true })
  render(<ClosedEval />)
  await screen.findByText('2026-11-19 10:00:00')
  // 先打开该 run 的报告（行点击），再删它
  fireEvent.click(within(screen.getByText('2026-11-19 10:00:00').closest('tr')!).getByText('已成功'))
  expect(await screen.findByText(/评测报告：run-20261119-100000-ab12/)).toBeInTheDocument()
  fireEvent.click(within(screen.getByText('2026-11-19 10:00:00').closest('tr')!).getByRole('button', { name: /删\s*除/ }))
  fireEvent.click(await screen.findByRole('button', { name: /确\s*定/ }))
  await waitFor(() => expect(mockedDeleteRun).toHaveBeenCalledWith('run-20261119-100000-ab12'))
  await waitFor(() => expect(mockedListRuns).toHaveBeenCalledTimes(2))
  expect(screen.queryByText(/评测报告：run-20261119-100000-ab12/)).not.toBeInTheDocument()
})

it('运行中行的删除按钮禁用', async () => {
  mockActiveRunApis()
  render(<ClosedEval />)
  expect(await screen.findByText(/已完成\s*8\s*\/\s*30/)).toBeInTheDocument()
  // 历史表无 run_id 列，按状态 Tag 定位运行行
  const row = screen.getByText('进行中').closest('tr')!
  expect(within(row).getByRole('button', { name: /删\s*除/ })).toBeDisabled()
})

it('搜索按 tt 编号命中（大小写不敏感）', async () => {
  mockIdleApis()
  mockedListMovies.mockResolvedValue(MOVIES)
  render(<ClosedEval />)
  await screen.findByText('2026-11-19 10:00:00')
  await selectDataset('genome-scores（交集 83）')
  fireEvent.click(screen.getByRole('radio', { name: '自定义' }))
  await waitFor(() => expect(mockedListMovies).toHaveBeenCalledWith('genome-scores'))
  const search = screen.getByRole('combobox', { name: '影片清单' })
  fireEvent.mouseDown(search)
  fireEvent.change(search, { target: { value: 'TT0000002' } })  // 大写 + tt 编号
  await waitFor(() => expect(screen.getByTitle('高山下的花环')).toBeInTheDocument())
  expect(screen.queryByTitle('铁道游击队')).not.toBeInTheDocument()
  expect(screen.queryByTitle('芙蓉镇')).not.toBeInTheDocument()
})
```

（注：「运行中行的删除按钮禁用」用例按「进行中」状态 Tag 定位——历史表没有 run_id 列；`mockActiveRunApis` 桩的历史只有 running 行。）

- [ ] **Step 2: 跑红**

Run: `cd frontend && npx vitest run src/pages/ClosedEval.test.tsx`
Expected: 4 个新用例 FAIL（结束时间列缺、删除按钮缺、tt 搜索无结果）；既有用例全绿（其 8→9 断言已在 Step 1 同步更新）。

- [ ] **Step 3: 实现 api/eval.ts**

`RunStatus`/`RunSummary` 在 `started_at` 后加 `finished_at: string | null`；新增 `deleteRun`（`apiFetch<{ ok: boolean }>(..., { method: 'DELETE' })`，注释写 409/404 语义与 ApiError 透出）。

- [ ] **Step 4: 实现 ClosedEval.tsx**

- 模块级 `columns` 的「时间」列后插「结束时间」列（`dataIndex: 'finished_at'`，null → 暂无，formatTime——与「时间」列同款 render）。
- 组件内构造「操作」列并 `[...columns, 操作列]` 合并（需要 `refreshRuns`/`setReport` 闭包）：每行 `Popconfirm`（title「删除该 run 及其产物？」，与 ReviewCenter 同款确认交互）内 `Button danger size="small"`「删除」，`disabled={!TERMINAL_STATUSES.has(r.status)}`（Popconfirm 同 disabled）；按钮 `onClick={(e) => e.stopPropagation()}`（防触发行点击开报告）。
- `handleDelete(runId)`：`await deleteRun(runId)` → `message.success` → 若 `report?.runId === runId` 则 `setReport(null)` → `void refreshRuns()`；catch → `message.error`（409「运行中不可删」等 detail 透出）。
- 影片 Select：删 `optionFilterProp="label"`，加 `filterOption`——`input.trim().toLowerCase()` 对 `String(option?.value ?? '')` 与 `String(option?.label ?? '')` 各 includes（空输入恒 true）。

- [ ] **Step 5: 跑绿 + 前端全量**

Run: `cd frontend && npx vitest run src/pages/ClosedEval.test.tsx` 全绿；再 `npx vitest run`（flake 规则见 Global Constraints）。
Commit: `feat: 评测页结束时间列/终态 run 删除/选片搜索 tt+标题双匹配`

- [ ] **Step 6: 构建前端产物（部署预备，不入库）**

Run: `cd frontend && npm run build`
Expected: dist 产出、无 TS 报错（dist 为 gitignore，部署时随包带上）。

---

## 收尾（不在任务内，执行完计划后）

- 全量后端 + 前端套件绿 → fable 全分支复审（executing-plans 终审流程）。
- 部署到 9.15.154.212（tar 增量 + stop/start），用服务器上 `run-20261121-081903-b065`（interrupted 僵尸 run）做删除功能的首个真实验证，随后用户重跑全量 131 部评测（并发可保持 16——池已根治）。
