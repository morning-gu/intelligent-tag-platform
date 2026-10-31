# 影视智能标签平台 阶段 3（审核后台与多人审核闭环）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付完整多人审核闭环：账号登录、审核操作（含批量与并发冲突保护）、审核中心/内容详情/工作台等 8 页 React+AntD 前端、视频流式播放与时间戳证据定位（反失败承诺二）、审核推导版质量指标。

**Architecture:** 后端延续阶段 1/2 分层（新 auth/review/metrics 模块挂 FastAPI 薄层，全 API 加会话认证）；前端 `frontend/` 独立 Vite + React + TS + AntD SPA，dev 经 Vite proxy `/api` → uvicorn（无 CORS 需求）。前端数据获取用自写轻量 hook，不引 react-query；分布图用纯 CSS 条形（不引图表库）。

**Tech Stack:** 后端零新增依赖（密码 pbkdf2_hmac、会话 HMAC cookie 均 stdlib）；前端 Node ≥18 + Vite + React 18 + TypeScript + Ant Design 5 + react-router-dom 6 + Vitest + @testing-library/react。

**Spec:** `docs/superpowers/specs/2026-10-31-intelligent-tag-platform-design.md`（§7 审核后台、§10 反失败承诺二、§11 阶段 3）

基点：main @ `330b3dd`（阶段 1+2 已合并，130 tests）。**已拍板**：① rejected 入 upsert 跳过集（FINALIZED_STATUSES = approved/rejected/merged）；② 首管理员 = 应用启动引导（user 表空时创建 admin，密码取 `TAGPLT_ADMIN_PASSWORD`，默认 `admin123`，README 注明改密）。

范围说明：设计文档 8 个后台页面中**模型配置中心属阶段 4**（配置存储 + Prompt 版本管理）；本阶段第 8 个工作面为账号/用户管理（阶段 3 账号体系所需）。

## Global Constraints

- 沿用既有约定：src 布局、`TAGPLT_` 前缀、标识符英文注释中文、核心包不 import fastapi（auth/review/metrics 业务逻辑在核心包、路由在 api/）、git 每任务一次提交、测试 `.venv/Scripts/python -m pytest`。
- **第 10 张表 `user_account`**：`user_id TEXT PK`（`user_`+uuid4().hex[:8]）、`username TEXT NOT NULL UNIQUE`、`password_hash TEXT NOT NULL`、`role TEXT NOT NULL("admin"|"reviewer")`、`created_at DATETIME`。迁移 `0003_user_accounts.py`。密码 `hashlib.pbkdf2_hmac("sha256", pwd.encode(), salt(16B), 200_000)`，存储格式 `pbkdf2$200000${salt_hex}${hash_hex}`。
- 会话：HMAC-SHA256 自签 cookie `tagplatform_session`，载荷 `{"user_id","role","exp"}`（JSON base64 + "." + HMAC 签名 hex），密钥 `TAGPLT_SESSION_SECRET`（默认 `dev-secret`，README 警告生产必改）；有效期 7 天；解析失败/过期 → 未认证。
- 认证范围：`/api/v1/*` 全部要求登录（`get_current_user` 依赖），`/health` 除外；`GET /api/v1/prompts` 等只读也需登录；管理动作（用户管理）仅 admin。
- 前端：目录 `frontend/`（git 提交，`node_modules/`、`dist/` 进 .gitignore）；npm；dev 命令 `npm run dev`（proxy `/api`→`http://127.0.0.1:8000`）；测试 `npm test`（vitest run）。
- 阶段 3 不做：CORS 中间件（dev 走 proxy）、上传占位（阶段 4）、Prompt 编辑（阶段 4）、P/R/F1（有真值后）、Playwright E2E（收尾手动冒烟覆盖）。
- rejected 拍板落地：`results.py` 的 `FINALIZED_STATUSES = ("approved", "rejected", "merged")`（重命名语义：人工已定集），persist 测试补 rejected 保留用例。
- review_record 写入：动作 approve/reject/modify/merge 均写一行（record_id=`rev_`+uuid8、result_id、action、reviewer_id、before_json/after_json、comment、created_at）。
- 乐观并发：审核动作只允许作用于 `review_status == "pending"` 的行；行已非 pending → `409 {"detail": "该标签已被处理，请刷新"}`。
- 时间戳显式赋值（沿用）；80/422/409/401 错误约定沿用。

## Review Focus

1. **并发审核冲突**：A、B 两审核员先后提交同一行 → 后者 409 + 前端提示刷新（不静默覆盖）。→ B4 测试 `test_review_conflict_409` + F3 组件测试。
2. **未登录/会话过期**：受保护 API 无 cookie → 401；前端 401 拦截跳登录页。→ B2 测试 + F1 拦截器测试。
3. **视频缺失/Range 异常**：asset 无 video_uri 或文件不存在 → 404；Range 语法非法（`bytes=abc`）→ 忽略 Range 回 200 全量（或 416，取实现一致者并测试钉死）。→ B6 测试。
4. **批量审核部分冲突**：ids 中部分已非 pending → 逐条独立处理，响应 per-item 结果数组（不做整体回滚——审核幂等）。→ B4 `test_batch_review_partial_conflict`。
5. **modify 非法值**：空 tag_name/超长 → 422。→ B4 测试。

## 文件结构总览

```
src/tagplatform/
├── auth/__init__.py、auth/password.py、auth/session.py、auth/service.py   # B1/B2
├── review/__init__.py、review/service.py                                   # B4 审核动作+并发+记录
├── metrics.py                                                              # B7
├── api/routers/auth.py、review.py、video.py、metrics.py、users.py          # B2/B4/B5/B6/B7
├── api/depends.py        # get_current_user / require_admin（B2）
├── results.py            # 修改：FINALIZED_STATUSES + rejected（B3）
├── api/routers/generation.py、content.py  # 修改：响应加 id 字段（B3）
└── db/models.py + alembic/versions/0003_user_accounts.py                   # B1
frontend/
├── package.json、vite.config.ts、tsconfig.json、index.html
├── src/main.tsx、App.tsx（路由+布局）、api/client.ts（fetch 封装+401 拦截）
├── src/pages/Login.tsx、Layout.tsx+菜单（F1）
├── src/pages/Dashboard.tsx、Quality.tsx（F2）
├── src/pages/ReviewCenter.tsx + components/EvidenceCard.tsx（F3）
├── src/pages/AssetDetail.tsx + components/VideoPlayer.tsx、SceneTimeline.tsx（F4）
├── src/pages/GenerationTasks.tsx、TagCatalog.tsx、TagImports.tsx（F5/F6）
└── src/**/*.test.tsx（Vitest）
```

---

### Task B1: 账号表、密码与启动引导

**Files:** Create `auth/__init__.py`、`auth/password.py`、`auth/service.py`、`alembic/versions/0003_user_accounts.py`、`tests/test_auth_password.py`；Modify `db/models.py`、`api/__init__.py`（lifespan 引导）

**Interfaces:**
- Produces: `hash_password(pwd: str) -> str`（`pbkdf2$200000$salt_hex$hash_hex`）；`verify_password(pwd: str, stored: str) -> bool`（格式坏 → False 不抛）；`create_user(session, username, password, role) -> UserAccount`（重名 ValueError）；`get_user_by_username(session, username) -> UserAccount | None`；`bootstrap_admin(session, password: str) -> bool`（表空才建，返回是否创建）。lifespan 启动时调 `bootstrap_admin(session, os.environ.get("TAGPLT_ADMIN_PASSWORD", "admin123"))`。模型类 `UserAccount` 列名见 Global Constraints。

- [ ] **Step 1: 写失败测试** — `hash/verify` 往返、错密码 False、坏格式 False；`create_user` 重名 ValueError；`bootstrap_admin` 空表 True/非空 False；迁移子进程测试（0003 后 user_account 表存在，model 列对齐）。
- [ ] **Step 2: 确认失败** — `python -m pytest tests/test_auth_password.py -v` FAIL
- [ ] **Step 3: 实现** — 全 stdlib；lifespan 引导（注意 lifespan 已有 executor 构建/孤儿清理/启动顺序——引导放最前）。
- [ ] **Step 4: 确认通过 + 全量**（130 既有零回归）
- [ ] **Step 5: 提交** — `git commit -m "feat: 账号表/密码哈希/启动引导 admin"`

### Task B2: 会话认证与 API 保护

**Files:** Create `auth/session.py`、`api/depends.py`、`api/routers/auth.py`、`api/routers/users.py`、`tests/test_auth_api.py`；Modify `api/__init__.py`（注册路由+依赖挂载）、`tests/conftest.py`

**Interfaces:**
- Produces: `create_session_token(user_id, role, secret: str, ttl_days=7) -> str`；`parse_session_token(token, secret) -> dict | None`（失败/过期 None）；`get_current_user(request, session=Depends(get_session)) -> UserAccount`（401 未认证）；`require_admin(user=Depends(get_current_user)) -> UserAccount`（403 非 admin）。
- 路由：`POST /api/v1/auth/login` {username,password} → cookie + {user_id, username, role}（错 → 401 `{"detail": "用户名或密码错误"}`）；`POST /api/v1/auth/logout`（清 cookie）；`GET /api/v1/auth/me`。`GET /api/v1/users`、`POST /api/v1/users` {username,password,role}（admin only，role 校验）。
- **全 API 保护**：所有 `/api/v1/*` 路由加 `get_current_user` 依赖——用 FastAPI router-level `dependencies=[Depends(get_current_user)]` 挂到既有六个 router（health 除外）。
- conftest：`client` fixture 升级——启动后 `client.post("/api/v1/auth/login", json={"username":"admin","password":"admin123"})`（lifespan 引导已建号），cookie 自动随 TestClient 会话；既有 1/2 阶段 API 测试**断言不动**（登录后全部应绿——这是验收）。

- [ ] **Step 1: 写失败测试** — 登录成功/密码错 401；me 返回角色；无 cookie 访问 `/api/v1/tags` → 401；登出后 → 401；非 admin 建 reviewer 账号 403、admin 可建；token 往返/过期/篡改（改一位 HMAC → None）。
- [ ] **Step 2-4: 红绿实现**（过期测试用 ttl_days=0 或负构造）
- [ ] **Step 5: 提交** — `git commit -m "feat: 会话认证与全 API 保护"`

### Task B3: rejected 跳过集与结果 id 暴露

**Files:** Modify `src/tagplatform/results.py`、`tests/test_results.py`、`api/routers/generation.py`、`api/routers/content.py`、相关测试

**Interfaces:**
- `FINALIZED_STATUSES = ("approved", "rejected", "merged")`（语义：人工已定集，重跑不碰）；persist 测试补 rejected 保留用例（同构 approved 测试）。
- `GenerationResultOut`/`AssetTagOut`/任务详情 results/`GET /assets/{id}/tags` 的每行加 `"id": AssetTag.id`（int）——review API 与前端行操作的主键（终审输入②）。
- 现有响应测试同步加 id 断言。

- [ ] **Step 1-4: 红绿**（rejected 用例 + id 断言先写）
- [ ] **Step 5: 提交** — `git commit -m "feat: rejected 入保护集与结果行 id 暴露"`

### Task B4: 审核操作 API（单条 + 批量 + 并发保护）

**Files:** Create `review/__init__.py`、`review/service.py`、`api/routers/review.py`、`tests/test_review_service.py`、`tests/test_api_review.py`

**Interfaces:**
- `apply_review(session, asset_tag_id: int, action: str, reviewer: str, payload: dict | None) -> AssetTag`：
  - 前置：行不存在 → ValueError("标签行不存在")；`review_status != "pending"` → `ConflictError("该标签已被处理")`（API 层 409）
  - approve：status→approved；reject：status→rejected；modify：改 tag_name（若 generated 改 candidate_name）/tag_type/candidate_description 或 description 映射，**status 回 pending**（设计：修改后重新进队）；merge：payload 必含 `merge_to_tag_id`（须存在于 tag_catalog，否则 ValueError→422）→ 该行 tag_id=merge_to、candidate_* 置 None、status→merged
  - 每动作写一行 review_record（before_json/after_json 为行关键字段快照，comment 取 payload）
  - 全动作后 commit（服务内提交——与 persist 不同：审核是用户操作，即时落库）
- `ConflictError(Exception)` 定义在 review/service.py
- 路由：`POST /api/v1/asset-tags/{id}/review` {action, payload?} → 行摘要；`POST /api/v1/asset-tags/batch-review` {ids: int[], action, payload?} → `{"results": [{"id", "ok": true} | {"id", "ok": false, "detail": "该标签已被处理"}]}`（逐条独立，冲突不中断——Review Focus 4）；404 未知 id；422 非法 action/空 modify 值/坏 merge_to。

- [ ] **Step 1: 写失败测试** — service 层四动作 + 状态前置 + 记录写入；API 层 409（Review Focus 1）/批量部分冲突/422/404。
- [ ] **Step 2-4: 红绿 + 全量**
- [ ] **Step 5: 提交** — `git commit -m "feat: 审核操作 API（四动作/批量/并发冲突保护）"`

### Task B5: 审核列表 API

**Files:** Create `api/routers/review.py` 内列表端点、`tests/test_api_review.py` 扩展

**Interfaces:**
- `GET /api/v1/review/items?status=&confidence_min=&confidence_max=&tag_type=&mode=&asset_id=&origin=&page=1&page_size=50`：join tag_catalog + generation_task 的待审核/已审列表，每行含 id、asset_id、asset_title、tag_name（origin 感知，同任务详情 results 形状）、origin、mode、tag_type、confidence、relevance、review_status、evidence、task_id；按 confidence 升序默认（低分先审）；响应 `{items, total, page}`。
- 供前端下拉的辅助：`GET /api/v1/review/filters` → `{tag_types: [...库内去重], statuses: [...], modes: ["closed","open"]}`。
- **顺带落地终审 deferred（evidence 解析兜底）**：列表的 evidence 解析对 shape 非法项 catch ValidationError 降级为空列表（不再 500）——此端点是审核主消费路径，正是终审预告的时机；generation.py 的 `_parse_evidence` 同步收紧（同一次修复）。

- [ ] **Step 1-5: 红绿提交** — `git commit -m "feat: 审核列表与筛选 API"`

### Task B6: 视频流端点（Range）

**Files:** Create `api/routers/video.py`、`tests/test_api_video.py`

**Interfaces:**
- `GET /api/v1/assets/{asset_id}/video`：asset 无 video_uri 或文件 `Path(...).is_file()` False → 404 `{"detail": "视频文件不存在"}`；请求带合法 `Range: bytes=start-end` → 206 + `Content-Range`/`Accept-Ranges: bytes` + 对应分块（StreamingResponse 或 Response(content=chunk)）；end 越界取到文件尾；`Range` 语法非法（非 `bytes=` 或解析失败）→ 忽略 Range 回 200 全量；无 Range → 200 全量；`Content-Type` 取 `video/mp4`（按后缀 .mp4/.webm/.mkv 映射，其余 application/octet-stream）。
- video_uri 解析复用 `resolve_video_path(video_uri, None)`（file:// → 路径部分；绝对路径原样）。
- 单测用临时二进制文件（任意字节，如 `b"\x00" * 1024` 写文件）验证 200/206/Content-Range/越界 end/非法 Range/404。

- [ ] **Step 1-5: 红绿提交** — `git commit -m "feat: 视频流端点（Range/206/404）"`

### Task B7: 质量指标 API

**Files:** Create `src/tagplatform/metrics.py`、`api/routers/metrics.py`、`tests/test_metrics.py`

**Interfaces:**
- `compute_metrics(session) -> dict`（纯读，核心包不 import fastapi）：
  - `pending_count`（asset_tag pending 总数）
  - `review_stats`：{approved, rejected, merged, modified} 各计数与占比（分母 = review_record 总数按动作；modified = action=="modify" 计数）；新标签（origin=generated）通过率 = generated 且 approved 行数 / generated 已终审行数
  - `evidence_coverage`：evidence_json 非空且非 `[]` 的行占比（分母 asset_tag 总数）
  - `task_stats`：{total, succeeded, failed, success_rate, avg_duration_ms}（generation_task 推导，duration=finished_at-created_at 均值，仅终态任务）
- `confidence_distribution(session, buckets=10) -> list[{"bucket": "0.0-0.1", "count": n}]`（pending+approved+rejected 全部 asset_tag 的 confidence 分桶）
- `tag_type_coverage(session) -> list[{"tag_type", "total", "approved"}]`（每 tag_type 的标签行数与已通过数——承诺二④）
- 路由：`GET /api/v1/metrics/summary`、`GET /api/v1/metrics/confidence-distribution`、`GET /api/v1/metrics/tag-type-coverage`。

- [ ] **Step 1-5: 红绿提交**（测试构造小型已知数据断言精确数字）— `git commit -m "feat: 审核推导质量指标 API"`

---

### Task F1: 前端脚手架、登录与布局

**Files:** Create `frontend/`（package.json、vite.config.ts、tsconfig、index.html、src/main.tsx、App.tsx、api/client.ts、pages/Login.tsx、pages/Layout.tsx）、`frontend/src/api/client.test.ts`、`frontend/src/App.test.tsx`；Modify `.gitignore`

**Interfaces:**
- `npm create vite@latest frontend -- --template react-ts` 基底 + `npm i antd react-router-dom` + `npm i -D vitest @testing-library/react @testing-library/jest-dom jsdom`；vite.config `server.proxy = {"/api": "http://127.0.0.1:8000"}`；`test` script = `vitest run`（环境 jsdom）。
- `api/client.ts`：`apiFetch<T>(path, init?) -> Promise<T>`——自动 JSON、带 cookie（credentials: "include"）、**401 拦截**：抛 `UnauthorizedError` 并 `window.location.href = "/login"`（除 /login 自身）；导出 `login(username, password)`、`logout()`、`getMe()`。
- 路由：`/login` 公开；其余套 `Layout`（requireAuth：me 失败跳 login）。Layout：AntD `Layout` + `Menu` 八项（工作台/审核中心/内容资产（列表→详情）/生产任务/标签导入/标签库/质量评估/用户管理（admin 才显示）），路由懒加载可不必。
- `Login.tsx`：AntD Form，提交 login 成功跳 `/`。

- [ ] **Step 1: 写失败测试** — client.test.ts：401 时跳转（mock fetch + location）；App.test.tsx：未登录渲染跳 login 路由（MemoryRouter）。
- [ ] **Step 2: 红绿实现** — `cd frontend && npm test`。
- [ ] **Step 3: 手动验收** — `npm run dev` + 后端 uvicorn（mock 双开）→ 浏览器登录 admin/admin123 → 见菜单布局 → 错密码提示 → 登出回登录页。（执行者有浏览器则截图自查；无法浏览器则报告说明）
- [ ] **Step 4: 全量后端测试不受影响**（前端目录不进 pytest 收集路径）
- [ ] **Step 5: 提交** — `git commit -m "feat: 前端脚手架/登录/布局（React+AntD）"`

### Task F2: 工作台与质量评估中心

**Files:** Create `pages/Dashboard.tsx`、`pages/Quality.tsx`、`components/BarChart.tsx`（纯 CSS 条形）、对应 `*.test.tsx`

**Interfaces:**
- Dashboard：统计卡片行（pending_count、success_rate、evidence_coverage、今日任务数——summary API 字段直接展示）+ 快捷按钮"进入审核"。
- Quality：指标卡片组（review_stats 四计数/占比、新标签通过率、任务统计）+ 两个纯 CSS 条形图（置信度分布、tag_type 覆盖——`BarChart({items: {label, value, max?}[]}` 组件，div 宽度百分比渲染）。
- 组件测试：BarChart 渲染 label/比例宽度；Dashboard 用 mock apiFetch 断言卡片字段出现。

- [ ] **Step 1-3: 红绿 + 手动验收**（登录后两页数据正确渲染——用后端冒烟库）
- [ ] **Step 4: 提交** — `git commit -m "feat: 工作台与质量评估中心（指标+分布图）"`

### Task F3: 审核中心（核心页面）

**Files:** Create `pages/ReviewCenter.tsx`、`components/EvidenceCard.tsx`、对应测试

**Interfaces:**
- 列表：AntD `Table` + 顶部筛选栏（status/tag_type/confidence 区间/mode/origin，数据源 `GET /review/filters`），列：资产标题、标签名、类型、来源、confidence（进度条）、relevance、状态 Tag、操作；默认拉 pending；分页走列表 API。
- 行展开：`EvidenceCard`（evidence 列表：scene_id + 时间戳 + description；点击跳内容详情并定位该场景——`navigate(\`/assets/{asset_id}?scene={scene_id}&t={start_ms}\`)`）。
- 操作：单条 通过/拒绝（Popconfirm）；修改（Modal 表单：tag_name/tag_type/description）；合并（Modal：搜索标准标签选择 merge_to）；批量（row selection + 批量通过/拒绝）。
- **冲突处理（Review Focus 1）**：409 响应 → `message.error("该标签已被他人处理")` + 自动刷新列表。
- 组件测试：mock apiFetch 渲染行、通过操作调 POST review、409 后触发刷新回调；EvidenceCard 点击 emit 导航参数。

- [ ] **Step 1-3: 红绿 + 手动验收**（真实审核流：通过/拒绝/修改/合并/批量/冲突——两个浏览器会话模拟并发）
- [ ] **Step 4: 提交** — `git commit -m "feat: 审核中心（筛选/单条与批量操作/证据卡/冲突处理）"`

### Task F4: 内容详情与视频证据定位（承诺二核心）

**Files:** Create `pages/AssetList.tsx`（轻量：资产表格→详情入口）、`pages/AssetDetail.tsx`、`components/VideoPlayer.tsx`、`components/SceneTimeline.tsx`、对应测试

**Interfaces:**
- `VideoPlayer.tsx`：原生 `<video controls src="/api/v1/assets/{id}/video">` + `seek(ms: number)` 方法（currentTime = ms/1000），`ref` 暴露给父。
- `SceneTimeline.tsx`：横向条带，每场景宽度∝时长，hover 显示描述，点击 `onSceneClick(scene)`；选中场景高亮；标签列表点击 → 高亮其 evidence 涉及的场景（**标签反向高亮**，承诺二①）。
- `AssetDetail.tsx`：上半 VideoPlayer + 时间轴（scene 选中 → seek 起始毫秒）；下半：该资产标签列表（GET /assets/{id}/tags）+ 每标签 EvidenceCard（点击证据 → seek 该时间戳 + 高亮场景，承诺二②）。URL query `?scene=&t=` 支持从审核中心跳入定位。
- 组件测试：Timeline 比例渲染/点击回调；VideoPlayer seek（jsdom 下断言 currentTime 赋值）；AssetDetail 收 query 触发 seek。

- [ ] **Step 1-3: 红绿 + 手动验收**（需要真实 mp4：`ffmpeg -f lavfi -i testsrc=duration=10 ...` 生成或任意短视频文件登记导入；无 ffmpeg 则用 mock 视频接口冒烟 + 报告注明真实视频未验）
- [ ] **Step 4: 提交** — `git commit -m "feat: 内容详情（视频流播放/场景时间轴/证据定位）"`

### Task F5: 生产任务页

**Files:** Create `pages/GenerationTasks.tsx` + 测试

**Interfaces:** 任务 Table（状态 Tag 色映射、mode、错误列 Tooltip）+ 行展开任务详情（GET /generation/tasks/{id} 的 results 列表）+ failed 行"重试"按钮（POST retry → 刷新）；创建任务入口（Modal：多选资产 + mode 单选 + policy 数字输入 → POST tasks）。

- [ ] **Step 1-3: 红绿 + 手动验收 + 提交** — `git commit -m "feat: 生产任务页（列表/详情/重试/创建）"`

### Task F6: 标签导入管理与标签库管理

**Files:** Create `pages/TagImports.tsx`、`pages/TagCatalog.tsx`、`pages/Users.tsx`（轻量收尾）+ 测试

**Interfaces:**
- TagImports：上传（AntD Upload，POST /tags/import multipart）→ 结果摘要（inserted/skipped/issues/parse_errors 折叠表）+ 批次历史列表 + 批次详情（result_summary 解析展示）。
- TagCatalog：`GET /tags` 表格（q/type 筛选、分页）+ 详情 Drawer（aliases/description/status）。
- Users（admin）：`GET/POST /users` 简单表格 + 创建表单。
- 组件测试各一：mock 数据渲染关键列/上传成功回调展示摘要。

- [ ] **Step 1-3: 红绿 + 手动验收 + 提交** — `git commit -m "feat: 标签导入/标签库/用户管理页"`

---

## 收尾核对（执行完成后）

- 后端 `python -m pytest` 全绿；前端 `cd frontend && npm test` 全绿。
- **双进程手动冒烟**（uvicorn + `npm run dev`）：admin 登录 → 导 Excel → 导内容 → 建 closed 任务（mock 双开）→ 审核中心走完 通过/拒绝/修改/合并/批量/冲突 → 内容详情视频播放与时间戳跳转 → 工作台/质量页数据一致 → 建第二个 reviewer 账号验证权限。
- 更新 README：前端启动段（npm install/dev、proxy 说明、双进程开发流）。
- 更新项目记忆：阶段 3 完成；阶段 4 计划输入（Docker 编排含迁移顺序、锁依赖、上传占位、Prompt 编辑、模型配置中心）。

## 留给实现者的决定

- AntD 组件具体选型（Table/Descriptions 等价物）、Modal 表单布局细节。
- 视频类型 MIME 映射表具体条目。
- 前端目录内部组件拆分细度（以页面任务边界为准）。
- tsconfig 严格度细节、vitest setup 文件组织。
