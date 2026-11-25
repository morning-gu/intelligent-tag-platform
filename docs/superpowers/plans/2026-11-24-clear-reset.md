# 数据清空与资产删除（clear-reset）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 标签管理页与内容管理页新增五个清空/删除操作（①单内容标签 ②全部内容标签 ③标签库 ④单内容资产 ⑤全部资产）+ 计数预览，服务重跑实验的库级重置。

**Architecture:** 级联删除下沉两个 services 模块（`tags/reset.py`、`content/reset.py`，dry_run 同一实现供 preview 复用），orchestration 装配守卫（409）与 404，7 个新端点分布在 content/tags router（①④⑤ 对齐既有 `/assets` 族）；前端一个共享 `ClearConfirmModal`（输入"清空"确认 + 非零键计数预览）由两页接线，危险按钮仅 admin 渲染。

**Tech Stack:** FastAPI + SQLAlchemy（服务层内部 commit 模式）· React 19 + antd 5 + vitest/RTL

**Spec:** `docs/superpowers/specs/2026-11-24-clear-reset-design.md`（已批准）

**Branch:** `refactor/clear-reset`，自 `origin/main`（fetch 后取最新并记录实际基点；本 worktree 内 `git switch -c refactor/clear-reset origin/main`）；切分支后首个提交 = spec + plan 两文档

## Global Constraints

- **契约只增**：7 个新端点；既有端点、响应模型、路径零改动
- **无 DB schema 变更**：无迁移，alembic 停在 0018
- **分层红线**：`services/tags/reset.py`、`services/content/reset.py` 不得 import fastapi；HTTP 错误（404/409/422）在 orchestration 层抛
- **权限**：7 端点全部 `require_admin`（含 preview）
- **不碰**：视频源文件、评测磁盘产物、`user_account`、`tag_import_task`、`tag_change_log`；不写 change_log
- **counts 九键固定**（缺省 0 不省略）：`pre_asset_tag, asset_tag, generation_tasks, quality_evaluations, asset_build_task, media_assets, scenes, tag_define, tag_catalog`；`media_assets`（顶层行）与 `scenes` 不重叠；`generation_tasks` 含被删的 queued 任务
- **守卫**：仅 **running** 生成/构建任务（①④按资产闭包、②③⑤全库）→ 409；③ 另加 **processing** 导入任务 → 409；queued 不设防、随清空删除
- **路由注册顺序**：tags router 中 `/clear-preview` 注册在 `/{node_id}` 之前（照 `/export` 先例）
- **后端测试命令**：`PYTHONPATH=src "C:/code/research-topics/movie-tag-research/intelligent-tag-platform/.venv/Scripts/python.exe" -m pytest -q`（于 worktree 根执行）
- **前端测试命令**：`cd frontend && npm test`

## Review Focus

1. **清空中途失败不留半清库**（服务层末尾单 commit，异常即回滚）→ Task 1 `test_clear_library_wipes_all_keeps_imports_and_changelog`（全链数据一次清空无 IntegrityError 且逐表断言空）
2. **preview 与 DELETE counts 漂移**（同一删除半径两种口径）→ Task 2 `test_preview_matches_delete_counts`（同数据先 preview 后 DELETE，九键逐项相等）
3. **多级 parent 链闭包删除漏后代** → Task 1 `test_asset_closure_walks_deep_parent_chain`（三行链 a→b→c，closure 含全部三代、删除后代先于父级）
4. **孤儿 queued 任务被守卫误挡（永久 409 死锁）** → Task 2 `test_queued_tasks_deleted_not_blocked`（queued 行存在 → ② 200 且 counts.generation_tasks 含之）
5. **③/⑤ 之后既有接口空库可用不 500** → Task 2 `test_library_clear_keeps_api_usable`（③ 后 `GET /api/v1/tags` 200 且 `GET /api/v1/assets` 200；⑤ 后 `GET /api/v1/tags` 200）

---

### Task 1: services 层级联原语与守卫查询

**Files:**
- Create: `src/tagplatform/services/tags/reset.py`
- Create: `src/tagplatform/services/content/reset.py`
- Test: `tests/test_clear_reset_services.py`

**Interfaces:**
- Consumes: `capabilities/storage/models.py` 既有 ORM 模型
- Produces（Task 2 依赖，签名钉死）:
  - `tags/reset.py`：`CLEAR_COUNT_KEYS: tuple[str, ...]`（九键）；`clear_tag_data(session, asset_ids: Collection[str] | None = None, *, dry_run: bool = False) -> dict[str, int]`（返回九键全量 dict，填 pre_asset_tag/asset_tag/generation_tasks/quality_evaluations；dry_run 只数不删不 commit）；`clear_library(session, *, dry_run: bool = False) -> dict[str, int]`（前四键 + tag_define/tag_catalog）
  - `content/reset.py`：`asset_closure(session, asset_id: str) -> list[str] | None`（顶层+全部后代，未知 → None）；`delete_assets(session, asset_ids: Collection[str] | None = None, *, dry_run: bool = False) -> dict[str, int]`（内部先闭包展开；前四键 + asset_build_task/media_assets/scenes）；`active_task_counts(session, asset_ids: Collection[str] | None = None) -> dict[str, int]`（键：`running_generation`/`running_build`/`processing_import`；前两者按闭包过滤、None=全库；processing_import 恒全库）
- 删除顺序（FK 子先父后）：pre_asset_tag → asset_tag → tag_quality_evaluation → tag_generation_task → asset_build_task → media_asset（后代先）→（③）tag_define → tag_catalog；`delete(synchronize_session=False)`，行数取先行的 COUNT 查询（dry_run 与实删共用同一计数实现）
- 已核事实（queued 行被删后 runner 健壮，spec §3.3 验证项闭合）：`run_generation_task` 对缺行/非 queued 抛 ValueError（execute.py:65），`TaskExecutor._run` 吞异常记日志不 crash（runner.py:89-98）——服务层删除 queued 行对 runner 无损，无需额外代码

- [ ] **Step 1: 写失败测试**

`tests/test_clear_reset_services.py`（`session` fixture；数据用 ORM 直插——夹具常量与断言值）：

```python
def _seed(s):
    # 资产：m1(movie) + s1/s2(scene, parent=m1) + m2(movie, 裸行无任何关联数据)
    # m1 全链：TagGenerationTask(g1, m1, closed, succeeded)
    #          TagQualityEvaluation(g1, completed, model="m", prompt_version="v",
    #                                labels=[], scores={}, frame_paths={})
    #          PreAssetTag×2（approved+tag_action / pending+tag_drama, target m1）
    #          AssetTag×1（tag_action, "动作"）
    #          AssetBuildTask(b1, m1, succeeded)
    ...

def test_clear_tag_data_all_wipes_four_tables_keeps_library_and_assets(session): ...
    # clear_tag_data(session) → {pre:2, asset_tag:1, tasks:1, quality:1, 其余0}
    # 断言四表空、tag_define/tag_catalog/media_asset 原行数不变
def test_clear_tag_data_scoped_to_asset_closure(session): ...
    # clear_tag_data(session, ["m1"]) → m1 行删、m2 幸存；库与资产全幸存
def test_clear_tag_data_dry_run_counts_without_deleting(session): ...
    # dry_run=True 计数与实删相等且四表行数不变
def test_queued_task_rows_inside_delete_radius(session): ...
    # 插 status="queued" 任务行 → clear_tag_data 计入 generation_tasks 并删除
def test_clear_library_wipes_all_keeps_imports_and_changelog(session): ...
    # 夹具另插 TagImportTask(completed) + TagChangeLog 行 → 幸存；
    # 九键含 tag_catalog=5、tag_define=7（session fixture 预置库）；
    # tag_change_log/tag_import_task 行数不变；无 IntegrityError
def test_delete_assets_scoped_deletes_closure_and_related(session): ...
    # delete_assets(session, ["m1"]) → media_assets=1、scenes=2、build=1、
    # tag 数据四键同 ①；m2 幸存；media_asset 查 m1/s1/s2 皆 None
def test_delete_assets_all_keeps_library(session): ...
    # media_asset 全空、九键全填、tag_define/tag_catalog 原行数不变
def test_asset_closure_walks_deep_parent_chain(session): ...
    # 链 a(movie)→b(parent=a)→c(parent=b)：closure=["a","b","c"]；
    # delete_assets(["a"]) 后三行皆 None（后代先删无 FK 异常）
def test_asset_closure_unknown_returns_none(session): ...
def test_active_task_counts_scopes_and_ignores_queued(session): ...
    # m1 插 running 生成 + queued 生成，m2 插 running 构建，插 processing 导入：
    # active_task_counts(session, ["m1"]) == {running_generation:1, running_build:0, processing_import:1}
    # active_task_counts(session) == {running_generation:1, running_build:1, processing_import:1}
    # queued 不计入
```

- [ ] **Step 2: 运行验证失败**

Run: `PYTHONPATH=src "C:/code/research-topics/movie-tag-research/intelligent-tag-platform/.venv/Scripts/python.exe" -m pytest -q tests/test_clear_reset_services.py`
Expected: FAIL（ModuleNotFoundError: tagplatform.services.tags.reset）

- [ ] **Step 3: 实现两个 services 模块**

模块 docstring 陈述分层红线（不 import fastapi）与 spec §1/§3.2 引用；`content/reset.py` import `clear_tag_data`/`CLEAR_COUNT_KEYS` 自 `tags/reset.py`（跨域先例：ingest→library）。闭包 BFS 按 `parent_id` 展开；实删路径末尾 `session.commit()`、dry_run 不 commit。

- [ ] **Step 4: 运行验证通过**

Run: 同 Step 2 命令
Expected: PASS（10 用例全绿）

- [ ] **Step 5: 全量后端回归**

Run: `PYTHONPATH=src "C:/code/research-topics/movie-tag-research/intelligent-tag-platform/.venv/Scripts/python.exe" -m pytest -q`
Expected: 959 既有 + 10 新增 = 969 全绿

- [ ] **Step 6: Commit**

```bash
git add src/tagplatform/services/tags/reset.py src/tagplatform/services/content/reset.py tests/test_clear_reset_services.py
git commit -m "feat(clear-reset): services 层级联删除原语与守卫计数（五操作共用，dry_run 供预览）"
```

### Task 2: orchestration 装配 + 7 个 API 端点

**Files:**
- Modify: `src/tagplatform/orchestration/_shared.py`（追加 ClearCountsOut/ClearResultOut/ClearPreviewOut）
- Modify: `src/tagplatform/orchestration/content.py`（追加 4 个 DELETE 装配 + clear_preview）
- Modify: `src/tagplatform/orchestration/tags.py`（追加 clear_library 端点装配 + library_preview）
- Modify: `src/tagplatform/api/routers/content.py`（4 DELETE + 1 GET preview）
- Modify: `src/tagplatform/api/routers/tags.py`（1 DELETE + 1 GET preview；`/clear-preview` 注册在 `/{node_id}` 前）
- Test: `tests/test_clear_reset_api.py`

**Interfaces:**
- Consumes: Task 1 全部函数（签名见上）
- Produces（Task 3/4 的 API 契约）:
  - `DELETE /api/v1/assets/{asset_id}/tags` → `ClearResultOut{ok, counts}`
  - `DELETE /api/v1/content/tags` → `ClearResultOut`
  - `DELETE /api/v1/tags` → `ClearResultOut`
  - `DELETE /api/v1/assets/{asset_id}` → `ClearResultOut`
  - `DELETE /api/v1/assets` → `ClearResultOut`
  - `GET /api/v1/content/clear-preview?scope=asset-tags|all-tags|asset|all-assets&asset_id=…` → `ClearPreviewOut{scope, counts}`（asset 类 scope 缺 asset_id → 422；未知资产 → 404；scope 非法 → 422，`Query(pattern=…)`）
  - `GET /api/v1/tags/clear-preview` → `ClearPreviewOut`（单 scope 免参——spec 表格的 `?scope=library` 参数冗余，Ruling 记台账）
  - 响应模型（orchestration/_shared.py）：

```python
class ClearCountsOut(BaseModel):
    pre_asset_tag: int = 0
    asset_tag: int = 0
    generation_tasks: int = 0
    quality_evaluations: int = 0
    asset_build_task: int = 0
    media_assets: int = 0
    scenes: int = 0
    tag_define: int = 0
    tag_catalog: int = 0

class ClearResultOut(BaseModel):
    ok: bool
    counts: ClearCountsOut

class ClearPreviewOut(BaseModel):
    scope: str
    counts: ClearCountsOut
```

- 守卫（orchestration 内、删除前）：`active_task_counts` 的 running_generation/running_build > 0 → `HTTPException(409, "有 N 个生成/构建任务正在执行，请先等待其完成")`（N=两键之和）；③ 另查 processing_import > 0 → `HTTPException(409, "有 N 个标签导入任务正在执行，请先等待其完成")`
- ①④ asset 未知：`asset_closure` 返回 None → 404（复用既有 404 文案风格）
- ①④ 的作用域 = `asset_closure(session, asset_id)` 展开（与 spec §1 一致）

- [ ] **Step 1: 写失败测试**

`tests/test_clear_reset_api.py`（`client` fixture；数据经 `client.app.state.session_factory()` 直插，共用 `_seed`/`_seed_guard` 帮助函数，形状同 Task 1 夹具；③ 幸存断言另插 `TagImportTask(task_id="imp-ok", file_name="f.xlsx", status="completed")` 与 `TagChangeLog(tag_id="tag_action", action="create")`）：

```python
def test_clear_asset_tags_endpoint(): ...       # ① DELETE /api/v1/assets/m1/tags → 200
    # counts == {pre:2, asset_tag:1, tasks:1, quality:1, 其余 0}；DB 断言 m1 四表空、m2 幸存
def test_clear_all_content_tags_endpoint(): ... # ② → 200；库与资产幸存（行数断言）
def test_clear_library_endpoint(): ...          # ③ → 200；tag_catalog=5、tag_define=7；
    # imp-ok 与 changelog 行幸存
def test_delete_asset_endpoint(): ...           # ④ → counts {…, build:1, media_assets:1, scenes:2}
def test_delete_all_assets_endpoint(): ...      # ⑤ → media_asset 空、库幸存
def test_preview_matches_delete_counts(): ...   # scope=all-assets 先 GET 再 DELETE，九键相等
def test_preview_asset_scopes(): ...            # asset-tags 带 asset_id → 200；缺 asset_id → 422；
    # scope=library → 422（content preview 枚举外）
def test_tags_preview_endpoint(): ...           # GET /api/v1/tags/clear-preview → {scope:"library", counts}
def test_unknown_asset_404(): ...               # ① ④ 各一：DELETE /api/v1/assets/nope/… → 404
def test_running_tasks_409(): ...               # m1 插 running 生成 → ① 409；
    # 全库 running 构建 → ② 409、⑤ 409；detail 文案断言（"生成/构建任务正在执行"）
def test_processing_import_409_library_only(): ...  # 插 processing 导入 → ③ 409（"标签导入任务正在执行"）；
    # ② 仍 200（导入不涉 content 域）
def test_queued_tasks_deleted_not_blocked(): ...    # 插 queued 生成任务 → ② 200 且
    # counts.generation_tasks == 预置+1；行已删
def test_library_clear_keeps_api_usable(): ...  # ③ 后 GET /api/v1/tags → 200（items 空）、
    # GET /api/v1/assets → 200；⑤ 后 GET /api/v1/tags → 200
def test_admin_only_403(): ...                  # 建 reviewer（POST /api/v1/users）+ 换登录 →
    # 7 端点逐一 403
def test_requires_login_401(): ...              # 新 TestClient(client.app) 不登录 → 7 端点逐一 401
```

- [ ] **Step 2: 运行验证失败**

Run: `PYTHONPATH=src "C:/code/research-topics/movie-tag-research/intelligent-tag-platform/.venv/Scripts/python.exe" -m pytest -q tests/test_clear_reset_api.py`
Expected: FAIL（404：新端点不存在）

- [ ] **Step 3: 实现 orchestration 装配 + 路由**

content router 端点签名（同步 def，`user: UserAccount = Depends(require_admin)` + `session = Depends(get_session)`）；tags router 的 `GET /clear-preview` 与 `DELETE ""` 注册在 `/{node_id}`/`/{tag_id}` 相关既有路由之前（字面量先于参数）。409/404/422 全在 orchestration 函数内抛。

- [ ] **Step 4: 运行验证通过**

Run: 同 Step 2 命令
Expected: PASS（15 用例全绿）

- [ ] **Step 5: 全量后端回归（含既有 DELETE /tags/{tag_id} 与 GET /assets 路由不受新路由干扰）**

Run: `PYTHONPATH=src "C:/code/research-topics/movie-tag-research/intelligent-tag-platform/.venv/Scripts/python.exe" -m pytest -q`
Expected: 969 + 15 = 984 全绿

- [ ] **Step 6: Commit**

```bash
git add src/tagplatform/orchestration/_shared.py src/tagplatform/orchestration/content.py src/tagplatform/orchestration/tags.py src/tagplatform/api/routers/content.py src/tagplatform/api/routers/tags.py tests/test_clear_reset_api.py
git commit -m "feat(clear-reset): 五个清空/删除端点与计数预览（admin 专属，running/processing 守卫，queued 随删）"
```

### Task 3: 前端 clear API 模块 + ClearConfirmModal

**Files:**
- Create: `frontend/src/api/clear.ts`
- Create: `frontend/src/components/ClearConfirmModal.tsx`
- Test: `frontend/src/api/clear.test.ts`、`frontend/src/components/ClearConfirmModal.test.tsx`

**Interfaces:**
- Consumes: Task 2 的 API 契约（路径与响应形状）
- Produces（Task 4 依赖，签名钉死）:

```ts
// clear.ts
export interface ClearCounts { pre_asset_tag: number; asset_tag: number;
  generation_tasks: number; quality_evaluations: number; asset_build_task: number;
  media_assets: number; scenes: number; tag_define: number; tag_catalog: number }
export interface ClearResult { ok: boolean; counts: ClearCounts }
export type ClearPreviewScope = 'asset-tags' | 'all-tags' | 'asset' | 'all-assets' | 'library'
export interface ClearPreview { scope: string; counts: ClearCounts }
export function fetchClearPreview(scope: ClearPreviewScope, assetId?: string): Promise<ClearPreview>
    // 'library' → GET /api/v1/tags/clear-preview；其余 → GET /api/v1/content/clear-preview?scope=…[&asset_id=…]
export function clearAssetTags(assetId: string): Promise<ClearResult>   // DELETE /api/v1/assets/{id}/tags
export function clearAllContentTags(): Promise<ClearResult>             // DELETE /api/v1/content/tags
export function clearTagLibrary(): Promise<ClearResult>                 // DELETE /api/v1/tags
export function deleteAsset(assetId: string): Promise<ClearResult>      // DELETE /api/v1/assets/{id}
export function deleteAllAssets(): Promise<ClearResult>                 // DELETE /api/v1/assets

// ClearConfirmModal.tsx
export interface ClearOpConfig {
  title: string
  description: string
  previewScope: ClearPreviewScope
  assetId?: string
  execute: () => Promise<ClearResult>
  onDone?: () => Promise<void> | void
  onExport?: () => void
}
interface ClearConfirmModalProps { open: boolean; onClose: () => void; config: ClearOpConfig | null }
```

- 行为：open 时拉 preview → 只渲染**非零键**计数清单（键中文名常量 `CLEAR_COUNT_LABELS`：待审核标签/正式挂载/生成任务/质量评估/构建任务/内容资产/场景/标签定义/标签目录）；Input 输入恰为 `清空` 才点亮 danger 确认按钮；确认 → `execute()` → 成功后 `message.success`（非零键中文摘要）→ `await onDone?.()` → `onClose()`；`ApiError`（含 409）→ 弹窗内 Alert 透出 detail；preview 失败 → 独立 Alert、弹窗仍可确认（描述文案不依赖 preview）；`onExport` 提供时渲染"先导出备份"链接（click 调用之）

- [ ] **Step 1: 写失败测试**

`clear.test.ts`（mock `apiFetch`，7 用例）：每个函数断言请求路径与 `method: 'DELETE'`；`fetchClearPreview` 断言 query 拼装（library 走 tags 路径免参、asset-tags 带 asset_id）。

`ClearConfirmModal.test.tsx`（mock `../../api/clear` 的 `fetchClearPreview`；`apiFetch` 整体 mock 不必——组件只 import clear.ts）：渲染 `config` + `open`，断言：
1. 非零计数键中文名出现（零键不出现）
2. 输入错误字 → 确认按钮 disabled；输入 `清空` → enabled
3. 点击确认 → `config.execute` 调用一次 → `onDone` 调用 → `onClose` 调用（Modal 消失）
4. `execute` reject `new ApiError(409, '有 2 个生成/构建任务正在执行，请先等待其完成')` → Alert 文案出现、弹窗不关
5. `fetchClearPreview` reject → 计数区错误 Alert，输入 `清空` 后确认仍可点击
6. `onExport` 提供时"先导出备份"链接渲染且 click 调用

- [ ] **Step 2: 运行验证失败**

Run: `cd frontend && npm test -- src/api/clear.test.ts src/components/ClearConfirmModal.test.tsx`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现 clear.ts + ClearConfirmModal.tsx**

Modal 用 antd `Modal` + `Input` + `Alert` + `Typography`；执行中 loading 态防双击；`config === null || !open` 直接返回 null。计数键序遍历 `Object.keys` 按 `ClearCounts` 字面序。

- [ ] **Step 4: 运行验证通过**

Run: 同 Step 2 命令
Expected: PASS

- [ ] **Step 5: 前端全量回归**

Run: `cd frontend && npm test`
Expected: 260 既有 + 新增全绿

- [ ] **Step 6: Commit**

```bash
git add frontend/src/api/clear.ts frontend/src/api/clear.test.ts frontend/src/components/ClearConfirmModal.tsx frontend/src/components/ClearConfirmModal.test.tsx
git commit -m "feat(clear-reset): 前端 clear API 模块与共享确认弹窗（输入清空确认 + 非零计数预览）"
```

### Task 4: 两页接线 + 角色门控

**Files:**
- Modify: `frontend/src/pages/content-management/AssetList.tsx`
- Modify: `frontend/src/pages/tag-management/TagCatalog.tsx`
- Test: `frontend/src/pages/content-management/AssetList.test.tsx`（追加用例）、`frontend/src/pages/tag-management/TagCatalog.test.tsx`（追加用例）

**Interfaces:**
- Consumes: Task 3 的 `ClearOpConfig`/`ClearConfirmModal` 与 clear.ts 五函数；`AuthedOutlet`（`import { type AuthedOutlet } from '../Layout'`）
- Produces: 无（UI 终点）

- 接线要点：
  - 两页 `const outlet = useOutletContext<AuthedOutlet>() as AuthedOutlet | undefined`，`const isAdmin = outlet?.user?.role === 'admin'`（既有测试裸 Routes 无 context → 不渲染新按钮，既有断言零改动）
  - AssetList：`columns` 在 isAdmin 时追加"操作"列（`fixed: 'right'`；仅 `record.asset_type !== 'scene'` 渲染 [清空标签][删除] 两个 danger link 按钮，按钮 onClick 内 `e.stopPropagation()`——行 onClick 会导航详情）；工具栏 extra Space 追加 [清空全部标签][删除全部资产] danger 按钮；页面级 `const [clearOp, setClearOp] = useState<ClearOpConfig | null>(null)` + `<ClearConfirmModal open={clearOp !== null} onClose={() => setClearOp(null)} config={clearOp} />`；四组 config 的 description 按上文风格（④⑤ 注明"视频源文件不删除，可随时重新导入"），`onDone: loadAssets`
  - TagCatalog：工具栏 Space（"标签导出"旁）追加 [清空标签库] danger 按钮（isAdmin）；config `onExport: () => void exportCatalog()`（既有函数复用）、`onDone: () => { void selectDirectory(undefined) }`（清空后回根目录重载）

- [ ] **Step 1: 写失败测试**

两文件追加（admin 渲染走父 Route `element` 内 `<Outlet context={{ user }} />` 包装，`renderPage(user)` 帮助函数；`vi.mock('../../api/clear')` 桩五函数 + `fetchClearPreview`）：

```tsx
// AssetList.test.tsx
it('admin：movie 行渲染操作按钮、场景行不渲染')          // 展开 movie/场景分组断言
it('非 admin：无操作列、无工具栏危险按钮')
it('行内清空标签：打开弹窗输入清空确认后调 DELETE 并刷新列表')
  // 点按钮不触发行导航（location 不变）；clearAssetTags 桩断言入参 m1；
  // 成功后 fetchAssets 再次调用（apiFetch GET /assets 二次）
it('工具栏清空全部标签：确认流同上（clearAllContentTags 桩）')
it('删除资产按钮：弹窗描述含"视频源文件不删除"')
it('409 透出：execute 桩 reject ApiError(409, ...) → 弹窗 Alert 出现')
// TagCatalog.test.tsx
it('admin：工具栏渲染清空标签库按钮、非 admin 不渲染')
it('清空标签库确认流：调 clearTagLibrary、成功后回根目录（fetchTagCatalog 以 root 重载）')
it('弹窗渲染先导出备份链接且 click 触发导出桩')
```

- [ ] **Step 2: 运行验证失败**

Run: `cd frontend && npm test -- src/pages/content-management/AssetList.test.tsx src/pages/tag-management/TagCatalog.test.tsx`
Expected: FAIL（新用例红：按钮/弹窗不存在）

- [ ] **Step 3: 实现两页接线**（按上文接线要点）

- [ ] **Step 4: 运行验证通过**

Run: 同 Step 2 命令
Expected: PASS（既有 + 新增全绿）

- [ ] **Step 5: 前端全量回归 + 构建**

Run: `cd frontend && npm test && npm run build`
Expected: 全绿 + tsc/vite build 通过

- [ ] **Step 6: Commit**

```bash
git add frontend/src/pages/content-management/AssetList.tsx frontend/src/pages/content-management/AssetList.test.tsx frontend/src/pages/tag-management/TagCatalog.tsx frontend/src/pages/tag-management/TagCatalog.test.tsx
git commit -m "feat(clear-reset): 内容/标签两页危险操作接线（admin 门控、行级与全量清空、409 透出）"
```

---

## 终末验收（whole-branch review 后）

- 后端全量 + 前端全量 + `npm run build` 三绿
- Review Focus 五项各有对应测试且绿
- spec §2 反目标复核：无 schema 变更（`git diff origin/main --stat` 无 alembic/versions）；既有端点零改动（`tests/test_api_prompts_admin` 等契约锁原样）
