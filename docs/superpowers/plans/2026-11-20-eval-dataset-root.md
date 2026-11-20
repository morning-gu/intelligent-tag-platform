# 评测数据集根路径可配置（页面级）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** admin 在「闭环评测」页切换数据集根（/cache/MovieTags ↔ /cache/MovieTagsV2），服务端进程内 override + 起跑快照隔离，重启回落 .env 默认。

**Architecture:** `ClosedEvalService` 加 `dataset_root_override`（app 级单例，POST 变更）+ `effective_root` 属性；`start()` 起跑把生效根快照进 job，`_run_movie` 用快照加载（运行中切根不污染在跑 run）；router 加 GET/POST dataset-root 两端点；前端页顶输入+应用。

**Tech Stack:** FastAPI + pydantic（后端）；React + AntD + vitest（前端）。零新依赖、零迁移。

**Spec:** `docs/superpowers/specs/2026-11-20-eval-dataset-root-design.md`（已批准）

## Global Constraints

- **run 隔离红线**：在跑 run 的影片加载必须用起跑时快照根（spec 设计节）
- admin-only：新端点沿用 `require_admin`
- DB 零写入（本功能不触碰任何表）
- 分支 `feat/eval-dataset-root`，**基点 `feat/closed-loop-eval`**（PR #27 在途）；PR base=feat/closed-loop-eval（#27 合入后 GitHub 自动重定向 main）
- 工作树测试命令：`PYTHONPATH=src <主检出>/.venv/Scripts/python.exe -m pytest`（管道取退出码须 PIPESTATUS）；前端 `cd frontend && npm test`（负载 flake 规则：失败文件单跑过=flake 不拦截，勿与后端套件并发跑）
- **部署等当前评测 run 终态**（重启杀 run；监视器在途）

## Review Focus

（spec 隐含但无任务测试直接覆盖、最可能咬人的输入类——逐条钉进所属任务）

1. 空字符串 root → 422 形状拒绝（Task 1 pydantic min_length）
2. root 是**文件**非目录 → 400（Task 1 Step 1 用例）
3. 目录存在但扫描零有效数据集（gt 全坏）→ 400（Task 1 Step 1 用例）
4. 运行中 POST 切根污染在跑 run → 快照隔离（Task 2 Step 1 用 `_GateLLM` 钉时序）
5. 无 override/重启后 → GET 回落 settings 默认且 `is_default=true`（Task 1 Step 1 首用例）；前端 localStorage 预填仅提示、真相以 GET 为准（Task 3 Step 1）

---

### Task 0: 分支与文档落库

**Files:**
- Create: 分支 `feat/eval-dataset-root`（自 `feat/closed-loop-eval` 切出，在本工作树 `git checkout -b`）
- Commit: `docs/superpowers/specs/2026-11-20-eval-dataset-root-design.md` + 本计划文件

- [ ] **Step 1: 切分支**：在 closed-loop-eval 工作树 `git checkout -b feat/eval-dataset-root`（工作树从 feat/closed-loop-eval 移动到新分支，PR #27 分支不受影响）
- [ ] **Step 2: Commit 文档**：`git add docs/superpowers/specs/2026-11-20-eval-dataset-root-design.md docs/superpowers/plans/2026-11-20-eval-dataset-root.md && git commit -m "docs: 评测数据集根路径可配置 spec 与实施计划"`

### Task 1: 服务 override + GET/POST 端点 + 读路径走 effective_root

**Files:**
- Modify: `src/tagplatform/closed_eval.py`（`ClosedEvalService.__init__` 加 override 属性；加 `effective_root` property；`start()` 481 行 `root = self.effective_root`）
- Modify: `src/tagplatform/api/routers/eval.py`（2 端点 + 2 模型；`list_datasets`/`list_dataset_movies` 改读 `service.effective_root`）
- Test: `tests/test_api_eval.py`（追加用例；复用 `_write_dataset` 与 `client` fixture 模式）

**Interfaces:**
- Produces（Task 2/3 依赖）:
  - `ClosedEvalService.dataset_root_override: str | None`（公开属性，router 直接赋值）
  - `ClosedEvalService.effective_root: str`（property = override ?? settings.eval_dataset_root）
  - `GET /api/v1/eval/closed/dataset-root` → `{"root": str, "is_default": bool}`
  - `POST /api/v1/eval/closed/dataset-root` body `{"root": str}` → `{"root": str}`；400/422 语义见测试
  - `SetDatasetRootRequest(BaseModel)`: `root: str`（`min_length=1`）；`DatasetRootOut(BaseModel)`: `root: str, is_default: bool`

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_api_eval.py`，沿用文件既有 client/夹具惯例）：

```python
def test_dataset_root_default_and_switch():
    # 默认态：GET 返回 settings 根、is_default=True
    r = client.get(f"{_BASE}/dataset-root")
    assert r.status_code == 200
    body = r.json()
    assert body["root"] == str(client.app.state.settings.eval_dataset_root)
    assert body["is_default"] is True
    # 应用有效第二根（夹具另写一个迷你数据集目录）→ 200，GET 反映且 is_default=False，
    # GET /datasets 列表变为新根的数据集
    ...
    # 空字符串 → 422；不存在路径 → 400 带原因；文件（非目录）→ 400；目录在但零有效数据集 → 400
    ...
    # 二次 POST 相同 root → 200 幂等
```

（用例内细则：第二根用 `_write_dataset` 写到 tmp 另一目录；400 断言 `detail` 含原因关键词；非 admin 403 沿用文件内 reviewer 用例模式补一条）

- [ ] **Step 2: 跑测试确认失败**：`pytest tests/test_api_eval.py -v` 新用例 FAIL（404 路由不存在）
- [ ] **Step 3: 实现**——`closed_eval.py`：`__init__` 末尾 `self.dataset_root_override: str | None = None`；property `effective_root`（docstring 一行：进程内 override，重启回落 settings）；`start()` 的 `root = self.settings.eval_dataset_root` 改 `root = self.effective_root`（override 为 None 时行为零变化）。`eval.py`：两个 pydantic 模型 + 两端点（GET 无参返回 effective_root/is_default；POST 校验序 = `Path(root).is_dir()` → `scan_datasets(root, set())` 非空，任一不过 `HTTPException(400, detail=...)`，通过则 `service.dataset_root_override = root` 并返回 `{"root": root}`）；`list_datasets`/`list_dataset_movies` 的 `settings.eval_dataset_root` 换 `service.effective_root`（两函数签名加 `service: ClosedEvalService = Depends(get_closed_eval)`）
- [ ] **Step 4: 跑测试确认通过**：`pytest tests/test_api_eval.py -v` PASS
- [ ] **Step 5: 整套回归 + Commit**：`PYTHONPATH=src <主检出>/.venv/Scripts/python.exe -m pytest -q` 全绿；commit `feat: 闭环评测数据集根可配置——服务 override 与 GET/POST 端点`

### Task 2: 起跑快照——运行中切根不影响在跑 run

**Files:**
- Modify: `src/tagplatform/closed_eval.py`（job 对象加 `dataset_root` 字段；`pool.submit(self._run_movie, ...)` 增传根；`_run_movie(self, run_id, tt, dataset_root)` 内 `load_movie_content(dataset_root, tt)`）
- Test: `tests/test_closed_eval.py`（追加用例）

**Interfaces:**
- Consumes: Task 1 的 `dataset_root_override`/`effective_root`
- Produces: `_run_movie` 新签名（run_id, tt, dataset_root）——内部方法，无外部消费

- [ ] **Step 1: 写失败测试**（`tests/test_closed_eval.py` 追加；复用 `_write_dataset` 与文件内服务夹具模式）：

```python
def test_root_switch_mid_run_uses_snapshot(tmp_path, ...):
    # 双根夹具：rootA/rootB 同 dataset_id 同 tt，content 场景文本分别含
    # "MARKER_A"/"MARKER_B"（B 的 gt 可与 A 相同——只断加载来源）
    # 记录型+闸门 LLM（仿 test_api_eval._GateLLM，complete 里记 prompt 并可 hold）
    # 服务 start(dataset, {"mode": "custom", "ids": [tt]}) 于 rootA → hold 住首调用
    # 期间 service.dataset_root_override = str(rootB)
    # release → wait 终态 → 记录的 prompt 含 MARKER_A、不含 MARKER_B
```

- [ ] **Step 2: 跑测试确认失败**：预期 FAIL——prompt 含 MARKER_B（现行代码读运行时 settings）
- [ ] **Step 3: 实现**——start() 构建 job 处加 `dataset_root=root`（起跑已解析的局部变量，天然快照）；`ThreadPoolExecutor` 提交处 `_run_movie(job.run_id, tt)` → 增传 `job.dataset_root`；`_run_movie` 签名加第三参并替换 787 行 `self.settings.eval_dataset_root`。docstring 补一行「根在起跑快照，运行中切 override 不影响本 run」
- [ ] **Step 4: 跑测试确认通过**：`pytest tests/test_closed_eval.py -v` PASS
- [ ] **Step 5: 整套回归 + Commit**：全量 pytest 绿；commit `feat: 闭环评测起跑快照数据集根——运行中切根不污染在跑 run`

### Task 3: 前端——api 函数与页面路径输入/应用

**Files:**
- Modify: `frontend/src/api/eval.ts`（`DatasetRootInfo` 接口 + `getDatasetRoot()`/`setDatasetRoot(root)`）
- Modify: `frontend/src/pages/ClosedEval.tsx`（页顶「数据集根路径」Input + 应用按钮 + 当前生效展示；应用成功刷新数据集列表）
- Test: `frontend/src/pages/ClosedEval.test.tsx`（追加用例）

**Interfaces:**
- Consumes: Task 1 的 GET/POST 端点契约（`{root, is_default}` / `{root}`）
- Produces: `getDatasetRoot(): Promise<DatasetRootInfo>`、`setDatasetRoot(root: string): Promise<{root: string}>`（POST，400 时 apiFetch 抛错带 detail——沿用 client.ts 既有错误形态）

- [ ] **Step 1: 写失败测试**（`ClosedEval.test.tsx` 追加，vi.mock `../api/eval` 沿用文件既有 mock 模式）：
  - 渲染即调 `getDatasetRoot`，「当前生效」处显示返回 root
  - Input 值（localStorage 有存值且 GET is_default=true 时预填存值）
  - 点应用 → `setDatasetRoot` 收到输入值 → 成功后 `listDatasets` 被再次调用、成功 message
  - `setDatasetRoot` reject（detail=目录不存在）→ 错误 message 显示 detail
- [ ] **Step 2: 跑测试确认失败**：`cd frontend && npm test -- ClosedEval.test` 新用例 FAIL
- [ ] **Step 3: 实现**——`eval.ts` 两函数 + 接口；`ClosedEval.tsx` 页顶行（Form.Item 布局沿用页面既有表单排布）：`Input`（受控 state，初始 = localStorage 值 ?? GET root）+ `Button 应用` + 旁注 `当前生效: {root}`；应用 handler：调 `setDatasetRoot` → 成功 message.success + 重拉 `getDatasetRoot`（更新生效展示）+ 重拉 `listDatasets` + localStorage 存值；失败 message.error(err detail)
- [ ] **Step 4: 跑测试确认通过**：`npm test -- ClosedEval.test` PASS
- [ ] **Step 5: 前端全量 + tsc + Commit**：`npm test` 全绿（flake 规则见 Global Constraints）+ `npx tsc --noEmit` 0 错；commit `feat: 闭环评测页数据集根路径输入与应用`

### 收尾（实现完成后）

- 全量门禁：后端 pytest + 前端 npm test + tsc + `npm run build`
- push `feat/eval-dataset-root` → PR（base=feat/closed-loop-eval，标题/描述沿用 spec 摘要）
- **部署等当前评测 run 终态**（监视器通知后）：tar（closed_eval.py + eval.py + dist 重建）→ 重启 → 冒烟（GET dataset-root / POST 切换 / 页面查看）
