# 闭环评测管线追踪（funnel trace）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 闭环评测干跑顺带把管线解剖数据结构化落盘（漏斗计数/逐候选踪迹/漏检丢失环节/每批 LLM 原始输出），单片明细可归因"标签死在哪一层"。

**Architecture:** `ProduceTrace` 被动收集器挂进 closed producer 的六个既有丢弃点（可选参数，`trace=None` 走旧路径）；eval 服务三处落盘（llm_raw 每片文件 / progress.jsonl 增量行 / raw_results 正典行）；读侧纯函数派生计数与 misses 交叉；API 透出 + 新 llm-raw 下载端点；前端明细区扩展。

**Tech Stack:** Python 3.x + FastAPI/pydantic（后端）、React 18 + antd v6 + vitest（前端）、pytest。

**Spec:** `docs/superpowers/specs/2026-11-26-closed-eval-funnel-design.md`（含两处拍板：dir_asset 剔除不入 trace、重试原始输出只存成功份）

## Global Constraints

- DB 零写入红线：评测产物全落 `eval_output_root` 本 run 目录。
- 生产路径零行为变化：`execute.py` 调 produce 不传 trace；`trace=None` 时 producer 逐字节旧行为。
- None≠0「暂无」纪律：老 run 无 trace 键 → 读侧 None/null/404，宁缺勿崩不 500。
- movie_breakdown 分层红线：只用标准库，不 import fastapi/numpy/scipy。
- 候选 outcome 枚举五值：`kept | unmentioned | gate | unmountable | m_cap`（dir_asset 剔除属召回口径，不入 trace）。
- 重试批次的 llm_raw `content` = 最终成功那次输出；llm_raw 不进 raw_results、不进 API 载荷。
- resolve 同 tag_id 重复调用末次为准；`resolve` 未注册 tag_id 为 no-op。
- tt 进文件名/URL 前过白名单正则 `^[A-Za-z0-9_-]{1,64}$`，且必须存在于 raw_results 行。
- 提交信息中文 conventional 前缀（feat/fix/docs/test），每任务至少一个 commit。

## Review Focus

1. **tt 路径注入**：`llm_raw/{tt}.json` 文件名与 URL path 参数，tt 含 `/`、`..`、空、超长 → 拒绝（写侧跳过、API 404），绝不越界不 500。测试 → Task 2 Step（`_write_llm_raw` 跳过非法 tt）+ Task 4 Step（traversal/不存在/缺文件三态 404）。
2. **老 run 全链路「暂无」**：raw 行无 trace 键 → summary funnel null、detail trace null、misses lost_at null、llm-raw 404、前端说明文案。测试 → Task 3（None 传递）+ Task 4（老 run 404）+ Task 5（文案与暂无断言）。
3. **失败片/失败尝试无半截数据**：produce 抛错 → 该片 trace 不落、llm_raw 无文件；重试成功只留末次成功 attempt 的 trace。测试 → Task 2（失败注入 + 一次失败后成功的有状态 LLM 桩）。
4. **resolve 终态覆盖**：同 tag 跨层重复 resolve（先中间态后终局）→ 快照只信末次；未注册 id 的 resolve 不炸。测试 → Task 1（`ProduceTrace` 单元）。
5. **生产路径零漂移**：同输入 trace=None 与不带参数两次 produce 输出全等；既有全量测试回归绿。测试 → Task 1（双跑全等）+ Task 6（全量双栈回归）。

---

### Task 1: ProduceTrace 采集器 + producer 采集点 + 协议签名兼容

**Files:**
- Create: `src/tagplatform/services/generation/trace.py`
- Modify: `src/tagplatform/services/generation/base.py:140-148`（协议签名）
- Modify: `src/tagplatform/services/generation/closed/producer.py`（六个采集点）
- Modify: `src/tagplatform/services/generation/open/producer.py:37-42`（接受并忽略）
- Test: `tests/test_generation_closed.py`（文末新增「管线追踪」节）

**Interfaces:**
- Produces（后续任务依赖的精确形状）:
  - `ProduceTrace.add_candidates(rows: list[TraceCandidate])`、`note_batch(dir_id: str, batch: int, n: int, parse_retried: bool, raw: str)`、`note_fabricated(tag_id: str)`、`set_max_tags(m: int)`、`resolve(tag_id: str, outcome: str, *, applicability: int | None = None, sf: float | None = None)`、`snapshot() -> dict`、`raw_rows() -> list[dict]`
  - `snapshot()` 返回：`{"max_tags": int, "candidates": [{"tag_id","tag_name","dir_id","recall","batch","outcome","applicability","sf"}], "batches": [{"dir_id","batch","n","parse_retried"}], "fabricated": [str]}`；outcome None 归一成 `"unmentioned"`
  - `raw_rows()` 返回：`[{"dir_id","batch","n","parse_retried","content"}]`（content = 成功份全文）
  - 协议：`TagProducer.produce(self, spec, session, llm, embedder, trace=None)`；`OpenTagProducer.produce` 尾部加 `trace=None`（忽略）

- [ ] **Step 1: 写失败测试（producer 端到端 + 单元）**

在 `tests/test_generation_closed.py` 文末加节，测试名与断言：

```python
# ---------------------------------------------------------------- 管线追踪（funnel trace）

def test_produce_trace_full_funnel(session, content_fixture):
    # 复用 _seed_library/_build_spec/_ScriptedLLM/_evi 夹具：单目录 4 候选，
    # 判定给 3 条（A=5 保留、A=5 落选 m_cap、A=3 门控）+ 1 条不提
    # + 1 条编造 id tag_999（A=5）；max_tags=1：同目录两条可挂载，次优者落 m_cap
    trace = ProduceTrace()
    out = ClosedTagProducer().produce(spec, session, llm, MOCK_EMBEDDER, trace=trace)
    snap = trace.snapshot()
    by_id = {c["tag_id"]: c for c in snap["candidates"]}
    assert by_id["tag_001"]["outcome"] == "kept"
    assert by_id["tag_001"]["sf"] is not None and by_id["tag_001"]["applicability"] == 5
    assert by_id["tag_002"]["outcome"] == "gate"      # A=3
    assert by_id["tag_003"]["outcome"] == "unmentioned"  # 模型没提
    assert by_id["tag_004"]["outcome"] == "m_cap"     # 挂载成但没选上
    assert snap["fabricated"] == ["tag_999"]          # 编造 id
    assert snap["batches"][0]["n"] == 4 and snap["max_tags"] == 1
    assert len(out) == 1                              # 行为不变：只出 1 条

def test_produce_trace_unmountable_outcome(session, content_fixture):
    # 复用 test_closed_produce_unmountable_dropped_before_selection 的库/scope
    # 夹具（角色 scope 标签在多角色内容上挂载失败）：该 tag outcome == "unmountable"
    ...
def test_produce_trace_none_matches_no_trace(session, content_fixture):
    # 同一 _ScriptedLLM 两次：produce(..., trace=trace) 与 produce(...) 输出列表全等
    ...
def test_produce_trace_snapshot_rules():
    t = ProduceTrace()
    t.add_candidates([TraceCandidate(tag_id="a", tag_name="A", dir_id="d",
                                     recall=0.5, batch=0)])
    t.resolve("a", "gate", applicability=3)
    t.resolve("a", "kept", applicability=5, sf=0.9)   # 末次为准
    t.resolve("ghost", "gate")                        # 未注册 no-op
    t.note_fabricated("x"); t.note_fabricated("x")    # 清单不去重
    snap = t.snapshot()
    assert snap["candidates"][0]["outcome"] == "kept" and snap["candidates"][0]["sf"] == 0.9
    assert snap["fabricated"] == ["x", "x"]
    # 未 resolve 的候选归一 unmentioned
def test_produce_trace_raw_rows_success_only(session, content_fixture):
    # 自定义 LLM 桩：某目录首次返回不可解析文本、重试返回合法 JSON
    # → note_batch 记 parse_retried=True 且 content == 成功那份全文
    ...
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_generation_closed.py -k trace -v`
Expected: FAIL（`ModuleNotFoundError: tagplatform.services.generation.trace`）

- [ ] **Step 3: 实现 `trace.py` + 三处 producer 改动**

`trace.py`：`TraceCandidate` dataclass + `ProduceTrace`（内部 `dict[tag_id, TraceCandidate]` 按 add 序、`_batches`/`_fabricated`/`_max_tags` 列表态；`snapshot()` 深拷贝出 dict，outcome None → `"unmentioned"`）。
`base.py` 协议 produce 加 `trace=None`（`TYPE_CHECKING` 下 import，trace.py 不 import generation 任何模块，无环）。
`closed/producer.py` 采集点（每处 `if trace is not None:` 守卫，逻辑零改动）：
- `set_max_tags(spec.policy.max_tags)`（候选分组前）；
- 分批切片处 `add_candidates`（batch 序 = `start // batch_size`，recall = `candidate.score`）；
- 批解析 break 后 `note_batch(dir_id, start // batch_size, len(batch), attempt > 0, resp.content)`（注意放在 parse 循环成功 break 之后，`parse_retried = attempt == 1`）；
- 门控分支 `resolve(it["tag_id"], "gate", applicability=it["applicability"])`；
- 编造分支 `note_fabricated(it["tag_id"])`；
- 挂载失败分支 `resolve(it["tag_id"], "unmountable", applicability=..., sf=sf)`（sf/it 在作用域内）；
- 全局选择后：`selected` 集 `resolve(..., "kept", applicability=it["applicability"], sf=sf)`，`mounted` 减 `selected` 的差集 `resolve(..., "m_cap", ...)`。
`open/producer.py`：签名尾部加 `trace=None`，docstring 注明「接受并忽略，开放侧语义未实现」。

- [ ] **Step 4: 跑新测试 + 既有回归**

Run: `python -m pytest tests/test_generation_closed.py tests/test_generation_open.py tests/test_prompt_registry.py -v`
Expected: 全 PASS（既有测试零改动零失败 = 行为中性证据）

- [ ] **Step 5: Commit**

```bash
git add src/tagplatform/services/generation/trace.py src/tagplatform/services/generation/base.py \
  src/tagplatform/services/generation/closed/producer.py src/tagplatform/services/generation/open/producer.py \
  tests/test_generation_closed.py
git commit -m "feat: ProduceTrace 采集器与 closed producer 六采集点（trace=None 零行为变化）"
```

### Task 2: eval 服务落盘三处 + MovieResult.trace

**Files:**
- Modify: `src/tagplatform/services/eval/service.py`（`MovieResult:126-143`、`_run_movie:651-685`、`_record_result:687-702`、`_write_artifacts:704-735`）
- Test: `tests/test_closed_eval.py`（文末新增「管线追踪落盘」节）

**Interfaces:**
- Consumes: Task 1 的 `ProduceTrace`（`from tagplatform.services.generation.trace import ProduceTrace`）、`snapshot()`/`raw_rows()` 形状。
- Produces（Task 3 依赖）: raw_results.json 行新增 `"trace"` 键 = snapshot() dict（非 None 时才写入；失败片/老 run 无该键）；`llm_raw/{tt}.json` = raw_rows() 的 JSON；`MovieResult` 增 `trace: dict | None = None`（带默认值，既有构造点不动）。

- [ ] **Step 1: 写失败测试**

```python
# ---------------------------------------------------------------- 管线追踪落盘

def test_run_writes_trace_and_llm_raw(svc, root):
    run_id = svc.start("emotion", {"mode": "all"}, wait=True)
    rows = _raw_rows(svc, run_id)
    for r in rows:                                   # 成功片：trace 键 + 结构
        assert {"max_tags", "candidates", "batches", "fabricated"} <= set(r["trace"])
        assert all(c["outcome"] in ("kept", "unmentioned", "gate",
                                    "unmountable", "m_cap")
                   for c in r["trace"]["candidates"])
    lines = (_run_dir(svc, run_id) / "progress.jsonl").read_text(
        encoding="utf-8").splitlines()
    assert all("trace" in json.loads(line) for line in lines)   # 增量行带 trace
    raw = json.loads((_run_dir(svc, run_id) / "llm_raw" / "tt0000001.json")
                     .read_text(encoding="utf-8"))
    assert raw and all({"dir_id", "batch", "n", "parse_retried", "content"}
                       <= set(e) for e in raw)

def test_failed_movie_has_no_trace(eval_setup):
    # 失败注入桩（同文件既有 failed-run 测试的 raising LLM 模式）：
    # 失败片 raw 行无 trace 键、llm_raw/ 目录下无该 tt 文件
    ...
def test_retry_keeps_only_last_success(eval_setup):
    # 有状态 LLM：每片首次 complete 抛错、第二次委托 MOCK_LLM
    # → 该片 trace 正常（末次成功 attempt 的 snapshot，非 None）
    ...
def test_write_llm_raw_skips_unsafe_tt(tmp_path):
    # 单元直调 _write_llm_raw：tt="../evil" 与 tt="" → 不落任何文件不抛错
    ...
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_closed_eval.py -k "trace or unsafe" -v`
Expected: FAIL（`KeyError: 'trace'` / AttributeError）

- [ ] **Step 3: 实现服务层**

`MovieResult` 尾部加 `trace: dict | None = None`。
`_run_movie`：attempt 循环内每次 `sink = ProduceTrace()`；成功分支 `trace = sink.snapshot()` 并在返回前调 `self._write_llm_raw(job, tt, sink.raw_rows())`；异常分支 `trace = None`；`MovieResult(..., trace=trace)`。
`_write_llm_raw(self, job, tt, rows)`：模块常量 `_TT_FILENAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")`；不过正则或 rows 为空 → 直接 return；`(run_dir / "llm_raw").mkdir(exist_ok=True)` 后 `_dump_json`（run_dir 由 `self.settings.eval_output_root / job.run_id` 现算，不持锁——每片独立文件）。
`_record_result`：`line` 增 `"trace": result.trace`（非 None 时）。
`_write_artifacts`：`rows` 行构造增 `"trace": r.trace`（非 None 时才写键）。

- [ ] **Step 4: 跑新测试 + 既有回归**

Run: `python -m pytest tests/test_closed_eval.py tests/test_movie_breakdown.py -v`
Expected: 全 PASS（`test_run_completes_and_writes_artifacts` 的 produced 五键断言是行内子集口径，行级新键不冲突）

- [ ] **Step 5: Commit**

```bash
git add src/tagplatform/services/eval/service.py tests/test_closed_eval.py
git commit -m "feat: 闭环干跑三处落盘 trace 与 llm_raw 每片文件（失败片/非法 tt 不落）"
```

### Task 3: movie_breakdown 读侧派生（funnel_counts + lost_at）

**Files:**
- Modify: `src/tagplatform/services/eval/movie_breakdown.py`
- Test: `tests/test_movie_breakdown.py`（文末新增节）

**Interfaces:**
- Consumes: raw 行 `"trace"` 键（Task 2 的 snapshot 形状）。
- Produces（Task 4 依赖）:
  - `funnel_counts(trace: dict | None) -> dict | None`，键：`{"n_recall","n_mentioned","n_kept","n_gate","n_unmountable","n_m_cap","n_fabricated","n_batches","parse_retries","max_tags"}`（n_mentioned = applicability 非 None 的候选数；parse_retries = batches 里 parse_retried 计数）
  - `summarize_movies` 行增 `"funnel"` 键（counts dict 或 None，恒写键）
  - `breakdown_movie` detail 增 `"trace"` 键：`{"counts": ..., "candidates": [...行原样], "batches": [...], "fabricated": [...], "max_tags": int}` 或 None
  - misses 行增 `"lost_at"`：`"unrecalled" | outcome枚举 | None`（tag 不在 trace 候选 → `"unrecalled"`；在 → 其 outcome；outcome 为 `"kept"` 的不自洽防御 → None；trace None → None）

- [ ] **Step 1: 写失败测试**

```python
# ---------------------------------------------------------------- 管线追踪读侧

def test_funnel_counts_channels():
    # 手工构造 trace 候选行（每 outcome 各 N 条 + fabricated 2 + batches 含 1 次重试）
    # → 各键计数精确对账；trace=None → None
    ...
def test_summarize_rows_carry_funnel():
    # 有 trace 的行 funnel 为计数 dict；无 trace 的老行 funnel 为 None；排序键不变
    ...
def test_breakdown_trace_passthrough_and_lost_at():
    # 三象限：miss 的 tag 不在候选 → "unrecalled"；在且 outcome=gate → "gate"；
    # 无 trace（老 run）→ None；防御：outcome="kept" 却在 misses → None
    # detail["trace"]["counts"] 与 funnel_counts 同数、candidates 原样透传
    ...
def test_breakdown_failed_movie_trace_none():
    # 失败片 detail trace=None、misses 各行 lost_at=None
    ...
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_movie_breakdown.py -v`
Expected: FAIL（`funnel_counts` 未定义）

- [ ] **Step 3: 实现读侧纯函数**

`funnel_counts` 纯派生；`summarize_movies` 的 out.append 增 `"funnel": funnel_counts(row.get("trace"))`；`breakdown_movie` 在成功片分支组装 `detail["trace"]`（row.get("trace") 为 dict 才组装，否则 None）并在 misses 构造处交叉 `lost_at`。标准库 only，不改既有键。

- [ ] **Step 4: 跑测试**

Run: `python -m pytest tests/test_movie_breakdown.py tests/test_closed_eval.py -v`
Expected: 全 PASS

- [ ] **Step 5: Commit**

```bash
git add src/tagplatform/services/eval/movie_breakdown.py tests/test_movie_breakdown.py
git commit -m "feat: 漏斗计数读侧派生与 misses 丢失环节交叉（funnel_counts/lost_at）"
```

### Task 4: API 响应模型扩展 + llm-raw 端点

**Files:**
- Modify: `src/tagplatform/orchestration/eval.py`（响应模型区 + 新用例函数）
- Modify: `src/tagplatform/api/routers/eval.py`（新路由，挂 217 行 movies 明细路由之后）
- Test: `tests/test_api_eval.py`（既有闭环 movies 端点测试旁新增）

**Interfaces:**
- Consumes: Task 3 的行键 `funnel`/`trace`/`lost_at`；`_movies_read`（复用其守卫与 404 口径）。
- Produces（Task 5 依赖）:
  - `MovieSummaryRowOut.funnel: FunnelCountsOut | None`、`MissTagOut.lost_at: str | None`、`MovieDetailOut.trace: MovieTraceOut | None`（pydantic 嵌套自动校验，dict 直灌）
  - 新模型：`FunnelCountsOut`（Task 3 十键全 int）、`TraceBatchOut{dir_id,batch,n,parse_retried}`、`TraceCandidateOut{tag_id,tag_name|None,dir_id,recall|None,batch|None,outcome,applicability|None,sf|None}`、`MovieTraceOut{counts,candidates,batches,fabricated,max_tags}`
  - `get_run_movie_raw_file(settings, run_id, tt) -> FileResponse`；路由 `GET /runs/{run_id}/movies/{tt}/llm-raw`（前端全路径 `/api/v1/eval/closed/runs/{run_id}/movies/{tt}/llm-raw`）

- [ ] **Step 1: 写失败测试**

```python
# ---------------------------------------------------------------- llm-raw 与 trace 键

def test_movies_endpoints_carry_trace_fields(client, ...):
    # 既有 run 目录夹具扩展：raw 行带 trace → GET /runs/{id}/movies 行有 funnel 键、
    # GET /runs/{id}/movies/{tt} 有 trace 键且 misses 行有 lost_at；
    # 老 run 夹具（无 trace）→ funnel/trace/lost_at 全 null
    ...
def test_llm_raw_happy_path(client, ...):
    # llm_raw/{tt}.json 存在 → 200、Content-Disposition filename={tt}.json、体为 JSON
    ...
def test_llm_raw_guards(client, ...):
    # run 越界 run_id / tt 含斜杠或点号 / tt 不在 raw 行 / 文件缺失 → 全 404 不 500
    ...
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_api_eval.py -k "trace or llm_raw" -v`
Expected: FAIL（404 / 键缺失）

- [ ] **Step 3: 实现 orchestration 用例 + 路由**

`orchestration/eval.py`：四个新 Out 模型 + 三处字段增补；`get_run_movie_raw_file`：`_run_dir` 守卫 → tt 过 `_TT_FILENAME_RE`（同 Task 2 常量，可 import 或复制并注释同源）→ `_movies_read` 取行（复用其 404 口径）→ tt 在行内 → `(run_dir / "llm_raw" / f"{tt}.json").is_file()` → `FileResponse(..., filename=f"{tt}.json")`；任一不过 404「该 run 无此影片的原始输出」。
`routers/eval.py`：`@router.get("/runs/{run_id}/movies/{tt}/llm-raw")` 挂在 movies 明细路由后，透传 settings/run_id/tt，模式同 `get_run_file`。

- [ ] **Step 4: 跑测试 + 全 API 回归**

Run: `python -m pytest tests/test_api_eval.py -v`
Expected: 全 PASS（既有 movies 端点测试的行键断言为子集口径不受新键影响）

- [ ] **Step 5: Commit**

```bash
git add src/tagplatform/orchestration/eval.py src/tagplatform/api/routers/eval.py tests/test_api_eval.py
git commit -m "feat: 评测 API 透出 funnel/trace/lost_at 与 llm-raw 下载端点（三重守卫 404）"
```

### Task 5: 前端类型 + 明细视图扩展

**Files:**
- Modify: `frontend/src/api/eval.ts`（类型 + `movieRawUrl`）
- Modify: `frontend/src/components/ClosedEvalMovieTable.tsx`
- Test: `frontend/src/components/ClosedEvalMovieTable.test.tsx`（夹具扩展 + 新用例）

**Interfaces:**
- Consumes: Task 4 响应模型形状（TS 类型一一对应）。
- Produces:
  - `interface FunnelCounts`（十键 number）、`interface TraceCandidate/BatchEvent/MovieTrace`、`MovieSummaryRow.funnel: FunnelCounts | null`、`MissTag.lost_at: string | null`、`MovieDetail.trace: MovieTrace | null`
  - `movieRawUrl(runId: string, tt: string): string`（纯路径拼接，同 `fileUrl` 模式：`/api/v1/eval/closed/runs/${runId}/movies/${tt}/llm-raw`）
  - `DetailGroups` 增 `runId: string` prop（下载链接需要）

- [ ] **Step 1: 写失败测试（vitest，mock 模块边界同既有模式）**

```tsx
// 夹具扩展：SUMMARY 行加 funnel；DETAIL 加 trace（candidates 含五种 outcome 各一）
// 与 misses.lost_at；另备无 trace 的老 run 明细夹具
it('主表漏斗列：召回→提及→产出 三数渲染，无 trace 行「暂无」', ...)
it('明细漏斗条渲染五计数与次要计数（门控/挂载/截断/编造/批次/上限）', ...)
it('踪迹表按结局筛选：Select 选 gate → 只剩门控行；null 数值「暂无」', ...)
it('漏检表丢失环节列：未召回/门控丢弃 标签正确，null → 暂无', ...)
it('trace 为 null 的老 run 明细：渲染说明文案、无踪迹表/下载链接', ...)
it('下载 404：fetch mock 404 → message.error 透出，不崩视图', ...)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend && npx vitest run src/components/ClosedEvalMovieTable.test.tsx`
Expected: FAIL（类型键缺失 / testid 找不到）

- [ ] **Step 3: 实现**

`api/eval.ts`：类型 + `movieRawUrl`。
`ClosedEvalMovieTable.tsx`：
- 主表「相关数」列后加「漏斗」列：`funnel` 渲染 `` `召回 ${n_recall} → 提及 ${n_mentioned} → 产出 ${n_kept}` ``，null → `暂无`，`title` 属性带全链计数；
- `DetailGroups({ detail, runId })`，trace 非 null 时渲染（`data-testid="detail-funnel"` 一行统计、`data-testid="detail-trace"` 踪迹表 + 结局 Select 筛选、下载按钮 fetch+blob→`message.error` 404 路径）；null 时 `data-testid="detail-trace-absent"` 说明文案「本 run 早于管线追踪功能，无解剖数据」；
- 结局/丢失环节中文映射常量（`STATUS_LABELS` 同模式）：`kept=保留、unmentioned=未提及、gate=门控丢弃、unmountable=挂载失败、m_cap=上限截断、unrecalled=未召回`；
- misses 表加「丢失环节」列；
- θ 门/代际守卫/懒加载缓存逻辑零改动（trace 随 detail 一次拉回）。

- [ ] **Step 4: 跑组件测试 + 前端全量**

Run: `cd frontend && npx vitest run`
Expected: 全 PASS

- [ ] **Step 5: Commit**

```bash
git add frontend/src/api/eval.ts frontend/src/components/ClosedEvalMovieTable.tsx \
  frontend/src/components/ClosedEvalMovieTable.test.tsx
git commit -m "feat: 闭环明细漏斗条/踪迹表/丢失环节列与 llm-raw 下载（老 run 暂无口径）"
```

### Task 6: docs/eval.md 口径节 + 全量回归

**Files:**
- Modify: `docs/eval.md`（新增「管线追踪」节）

**Interfaces:**
- Consumes: 全部前序任务的落盘/读侧形状（文档引用实际键名）。

- [ ] **Step 1: 写文档节**

内容五段：outcome 五值语义与丢失环节口径（含「未召回」覆盖 dir_asset 剔除、召回数=过滤后）；三处落盘位置与 llm_raw 端点路径；重试批次只存成功份的拍板；老 run「暂无」全链路纪律；snapshot/raw_rows 的 JSON 形状示例。

- [ ] **Step 2: 全量双栈回归**

Run: `python -m pytest -q` 与 `cd frontend && npx vitest run`
Expected: 双栈全绿（331+ 既有 + 新增全 PASS）

- [ ] **Step 3: Commit**

```bash
git add docs/eval.md
git commit -m "docs: eval.md 管线追踪口径节（outcome 语义/落盘位置/暂无纪律）"
```
