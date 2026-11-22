# 前端页面合一 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 10 项平铺菜单收敛为 7 模块页面（工作台吸收质量评估、标签管理/配置管理 tab 容器化），旧路由重定向保书签。

**Architecture:** 纯前端路由层改动——新建两个 tab 容器组件收编既有页面组件（组件代码零改动），重写工作台为总览看板，Layout 菜单 10→7，四个旧路由以 `Navigate` 重定向。API 端点契约零变化。

**Tech Stack:** React 19 + react-router v7 + antd 6 + vitest + @testing-library/react。

**Spec:** `docs/superpowers/specs/2026-11-22-frontend-page-consolidation-design.md`

## Global Constraints

- **API 契约零变化**：不改动 `frontend/src/api/**` 任何文件与任何后端代码；全部端点、请求、响应结构冻结
- **九个业务组件文件零改动**：`TagCatalog.tsx` / `TagImports.tsx` / `Users.tsx` / `ModelConfig.tsx` / `ReviewCenter.tsx` / `GenerationTasks.tsx` / `ClosedEval.tsx` / `AssetList.tsx` / `AssetDetail.tsx` 原样收编，只换挂载容器
- tab 状态同步 URL query（`?tab=`），刷新与深链不丢状态；非法 `?tab=` 值回落默认 tab 不崩溃
- 旧路由重定向（保书签）：`/quality→/`、`/imports→/tags?tab=imports`、`/users→/config?tab=users`、`/model-config→/config?tab=model`
- 终态菜单七项（命名对齐用户 7 模块、顺序同此列表）：工作台 `/`、标签管理 `/tags`、内容管理 `/assets`、内容打标 `/tasks`、审核中心 `/review`、闭环评测 `/eval-closed`（admin）、配置管理 `/config`（admin）；普通用户 5 项（前五）
- 测试门槛：`cd frontend && npm test` 全绿；后端零改动不重跑
- 分支自 `refactor/four-layer` @ f31787c 堆叠：`git checkout -b refactor/page-consolidation`
- 每步顺带更新所改文件的头部注释（App.tsx / Layout.tsx 注释描述菜单结构，是活文档）

## Review Focus

1. **旧书签深链失效**：收藏的 `/imports` 直达后必须落在标签管理的导入 tab（非 404/工作台）→ Task 2 集成测试
2. **tab 刷新丢状态**：`/tags?tab=imports` 直接访问（非点击）激活的是导入批次 tab → Task 2 容器测试
3. **admin 门禁回归**：reviewer 走旧书签 `/users`，经重定向链后仍被 AdminRoute 拦回工作台，不白屏 → Task 3 集成测试
4. **`?tab=` 非法值**：`/tags?tab=garbage` 不崩溃、回落默认"标签库" tab → Task 2 容器测试
5. **指标端点部分失败**：工作台三指标端点任一 reject → 错误 Alert 降级（沿用现质量评估页行为），不白屏 → Task 1 测试

---

### Task 1: 工作台吸收质量评估（总览页）

**Files:**
- Modify: `frontend/src/pages/Dashboard.tsx`（重写为总览）
- Delete: `frontend/src/pages/review/Quality.tsx`、`frontend/src/pages/review/Quality.test.tsx`
- Modify: `frontend/src/App.tsx:74-91`（删 `quality` 路由，加重定向）
- Modify: `frontend/src/pages/Layout.tsx:18-26`（删"质量评估"菜单项）
- Test: `frontend/src/pages/Dashboard.test.tsx`、`frontend/src/App.test.tsx`

**Interfaces:**
- Consumes: `api/metrics` 的 `fetchMetricsSummary` / `fetchConfidenceDistribution` / `fetchTagDirCoverage` / `formatPercent` / `formatDuration` 与 `BarChart` 组件（均既有）
- Produces: `Dashboard`（default export、无 props）终态总览页：三组指标卡 + 置信度分布图 + 目录覆盖图 + 快捷入口（"进入审核"→`/review`、"创建任务"→`/tasks`）；路由表不再含 `/quality` 页面

- [ ] **Step 1: 建分支**

```bash
git checkout -b refactor/page-consolidation
```

Expected: `refactor/page-consolidation`（基于 f31787c）

- [ ] **Step 2: 写失败测试（Dashboard.test.tsx 重写）**

以 `Quality.test.tsx` 的三端点 mock 分发夹具为底，重写 `Dashboard.test.tsx` 断言：
- 三组卡片字段渲染（待审核标签/任务成功率/证据覆盖率/平均耗时；审核状态 通过/驳回/合并；生成质量/任务统计）
- 两张 BarChart 的数据行渲染（置信度分布、目录覆盖）
- 率/占比字段 null → "暂无"
- 快捷入口：`进入审核` 点击 → 路由到 `/review`；`创建任务` 点击 → 路由到 `/tasks`
- 单端点 reject（如 confidence 端点）→ 错误 Alert 出现、不抛错
- App.test.tsx：菜单断言十项→九项（删"质量评估"项及其断言行）

- [ ] **Step 3: 运行确认失败**

Run: `cd frontend && npx vitest run src/pages/Dashboard.test.tsx src/App.test.tsx`
Expected: FAIL（Dashboard 仍是四卡旧形态）

- [ ] **Step 4: 实现**

`Dashboard.tsx` 主体 = 现 `Quality.tsx` 的渲染结构原样搬入（组件名与注释改为总览语义），底部加快捷入口按钮组（Button + useNavigate）；错误处理沿用现形态（任一端点失败 → 页面级 Alert）。删除 `Quality.tsx` / `Quality.test.tsx`。`App.tsx` 删 `quality` 路由与 Quality import，加 `<Route path="quality" element={<Navigate to="/" replace />} />`。`Layout.tsx` 删"质量评估"项。更新两文件头注释。

- [ ] **Step 5: 运行确认通过**

Run: `cd frontend && npx vitest run src/pages/Dashboard.test.tsx src/App.test.tsx`
Expected: PASS

- [ ] **Step 6: 全量 + 提交**

Run: `cd frontend && npm test`
Expected: 全绿（Quality 用例已并入 Dashboard.test）

```bash
git add -A && git commit -m "feat: 工作台吸收质量评估为总览页，删 /quality 独立路由"
```

### Task 2: 标签管理一页两 tab

**Files:**
- Create: `frontend/src/pages/tag-management/TagManagement.tsx`
- Modify: `frontend/src/App.tsx`（`tags` 路由换容器、`imports` 路由改重定向）
- Modify: `frontend/src/pages/Layout.tsx`（"标签库"→"标签管理"、删"导入任务"项）
- Test: 新建 `frontend/src/pages/tag-management/TagManagement.test.tsx`、`frontend/src/App.test.tsx`

**Interfaces:**
- Consumes: `TagCatalog` / `TagImports`（同目录既有组件，default export、无 props）
- Produces: `TagManagement`（default export、无 props）；tab 键 `'catalog'`（标签库，默认）| `'imports'`（导入批次，URL `?tab=imports`）；路由 `/tags` → TagManagement；重定向 `/imports` → `/tags?tab=imports`

- [ ] **Step 1: 写失败测试**

`TagManagement.test.tsx`（MemoryRouter 挂载，apiFetch 整体 mock 为永不落定桩）断言：
- 默认激活"标签库" tab（`role="tab"` 选中态）
- `initialEntries={['/tags?tab=imports']}` → 激活"导入批次" tab
- `initialEntries={['/tags?tab=garbage']}` → 回落"标签库" tab，不抛错
- 点击"导入批次" tab → URL 变为 `/tags?tab=imports`（assert location.search）

App.test.tsx 集成用例：admin `initialEntries={['/imports']}` → 断言落点渲染出导入页特征内容（TagImports 的页面标识性文案）且标签管理路由生效；菜单断言九项→八项（"标签管理"在、"导入任务"不在）

- [ ] **Step 2: 运行确认失败**

Run: `cd frontend && npx vitest run src/pages/tag-management/TagManagement.test.tsx src/App.test.tsx`
Expected: FAIL（TagManagement 不存在）

- [ ] **Step 3: 实现**

`TagManagement.tsx`：`useSearchParams` 读 `?tab=`，白名单校验（仅 `'catalog'|'imports'`，非法/缺省 → `'catalog'`）；antd `Tabs` `activeKey` + `onChange` 写回 searchParams（默认 tab 清除参数）；items 两项 children 挂 `TagCatalog` / `TagImports`。`App.tsx`：`tags` 路由 element 换 `TagManagement`，删 `imports` 页面路由，加 `<Route path="imports" element={<Navigate to="/tags?tab=imports" replace />} />`。`Layout.tsx` 菜单"标签库"改名"标签管理"、删"导入任务"项。更新头注释。

- [ ] **Step 4: 运行确认通过**

Run: `cd frontend && npx vitest run src/pages/tag-management/TagManagement.test.tsx src/App.test.tsx`
Expected: PASS

- [ ] **Step 5: 全量 + 提交**

Run: `cd frontend && npm test`
Expected: 全绿

```bash
git add -A && git commit -m "feat: 标签管理收编标签库与导入批次为单页双 tab，/imports 重定向"
```

### Task 3: 配置管理一页两 tab（admin）

**Files:**
- Create: `frontend/src/pages/config/ConfigManagement.tsx`
- Modify: `frontend/src/App.tsx`（AdminRoute 下加 `config` 容器路由；`users`/`model-config` 改重定向）
- Modify: `frontend/src/pages/Layout.tsx`（ADMIN 菜单"用户管理"+"模型配置"→"配置管理"）
- Test: 新建 `frontend/src/pages/config/ConfigManagement.test.tsx`、`frontend/src/App.test.tsx`

**Interfaces:**
- Consumes: `Users` / `ModelConfig`（同目录既有组件，default export、无 props）
- Produces: `ConfigManagement`（default export、无 props）；tab 键 `'users'`（用户，默认）| `'model'`（模型与 Prompt，URL `?tab=model`）；路由 `/config`（AdminRoute 内）→ ConfigManagement；重定向 `/users`→`/config?tab=users`、`/model-config`→`/config?tab=model`

- [ ] **Step 1: 写失败测试**

`ConfigManagement.test.tsx` 断言：
- 默认激活"用户" tab
- `initialEntries={['/config?tab=model']}` → 激活"模型与 Prompt" tab
- `?tab=garbage` → 回落"用户" tab
- 点击"模型与 Prompt" → URL 含 `tab=model`

App.test.tsx 集成用例（改写现有 `/users` 三例）：
- admin `['/config']` → 整树渲染出用户管理页特征内容
- admin `['/model-config']` → 重定向后渲染出模型配置特征内容且"模型与 Prompt" tab 激活
- reviewer `['/users']` → 经重定向链 `/users`→`/config?tab=users`→AdminRoute 拦回 → 落在工作台，不白屏
- 菜单断言：admin 七项（"配置管理"在、"用户管理"/"模型配置"不在）、普通用户五项

- [ ] **Step 2: 运行确认失败**

Run: `cd frontend && npx vitest run src/pages/config/ConfigManagement.test.tsx src/App.test.tsx`
Expected: FAIL（ConfigManagement 不存在）

- [ ] **Step 3: 实现**

`ConfigManagement.tsx`：同 Task 2 模式（白名单 `'users'|'model'`，缺省/非法 → `'users'`），items 两项 children 挂 `Users` / `ModelConfig`。`App.tsx`：AdminRoute 内加 `<Route path="config" element={<ConfigManagement />} />`，删 `users`/`model-config` 页面路由，加两条 `Navigate` 重定向。`Layout.tsx`：ADMIN_MENU_ITEMS 收敛为"闭环评测"+（`/config`）"配置管理"。更新头注释。

- [ ] **Step 4: 运行确认通过**

Run: `cd frontend && npx vitest run src/pages/config/ConfigManagement.test.tsx src/App.test.tsx`
Expected: PASS

- [ ] **Step 5: 全量 + 提交**

Run: `cd frontend && npm test`
Expected: 全绿

```bash
git add -A && git commit -m "feat: 配置管理收编用户与模型配置为单页双 tab，/users /model-config 重定向"
```

### Task 4: 菜单终态命名与顺序 + 收尾回归

**Files:**
- Modify: `frontend/src/pages/Layout.tsx:18-33`（命名+顺序终态）
- Test: `frontend/src/App.test.tsx`

**Interfaces:**
- Consumes: Task 1-3 的路由与菜单中间态
- Produces: 终态菜单（Global Constraints 七项列表的命名与顺序）；`npm run build`（tsc -b）通过的分支终态

- [ ] **Step 1: 写失败测试（App.test.tsx 终态断言）**

菜单用例改为断言七项**按序**（`getAllByRole('menuitem')` 的 name 序列）：工作台、标签管理、内容管理、内容打标、审核中心、闭环评测、配置管理；普通用户五项（前五）。测试名同步（"十项"→"七项"）。

- [ ] **Step 2: 运行确认失败**

Run: `cd frontend && npx vitest run src/App.test.tsx`
Expected: FAIL（菜单仍是"内容资产"/"生产任务"与旧顺序）

- [ ] **Step 3: 实现**

`Layout.tsx`：BASE_MENU_ITEMS 按终态顺序改名（"内容资产"→"内容管理"、`/assets`；"生产任务"→"内容打标"、`/tasks`）并排序列 工作台/标签管理/内容管理/内容打标/审核中心；ADMIN_MENU_ITEMS 为 闭环评测/配置管理。更新头注释。App.tsx 头注释同步终态。

- [ ] **Step 4: 运行确认通过**

Run: `cd frontend && npx vitest run src/App.test.tsx`
Expected: PASS

- [ ] **Step 5: 全量 + 类型检查 + 构建冒烟 + 提交**

Run: `cd frontend && npm test && npm run build`
Expected: 测试全绿；`tsc -b` 与 vite build 无错误

```bash
git add -A && git commit -m "feat: 菜单终态对齐七模块命名与顺序"
```
