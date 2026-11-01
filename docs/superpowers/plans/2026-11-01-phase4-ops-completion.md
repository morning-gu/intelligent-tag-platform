# 影视智能标签平台 阶段 4（部署工程化与运营收口）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 收官交付：直接部署工程化（venv + systemd + nginx，**Docker 不实现**——用户 2026-11-01 拍板推翻设计文档的 Docker Compose 决策）、Prompt 版本管理 API 与模型配置中心页（兑现"改提示词零代码变更"承诺的最后一环）、上传占位（入口 A）、MockLLM 场景证据（演示完整性）。

**Architecture:** 后端延续分层；Prompt 编辑走**版本不可变**模式（新版本插入 + 激活切换，历史版本保留——与 generation_task.prompt_version 记录语义咬合）。运行配置（模型名/阈值等）仍走 env（TAGPLT_*），模型配置页**只读展示**——不引入运行期配置热切换（YAGNI，改 env 重启生效，文档说明）。上传占位：文件以 uuid 重命名存 `TAGPLT_MEDIA_ROOT`，登记 media_asset，不做识别。

**Tech Stack:** 既有栈零新增；部署产物为 systemd unit + nginx site 模板 + 依赖 lock（纯文本，无容器）。

**Spec:** `docs/superpowers/specs/2026-10-31-intelligent-tag-platform-design.md`（§8 模型运营、§11 阶段 4）
**Spec 偏离声明**：设计文档"部署：Docker Compose"已被用户推翻——本计划主线为直接部署，Docker 不实现；README 部署段注明"如需容器化可后续补充"。设计文档该决策点以用户拍板为准。

基点：main @ `51a59a9`（阶段 1-3 全合并，pytest 262 + npm 134）。

## Global Constraints

- 沿用全部既有约定：src 布局、`TAGPLT_` 前缀、标识符英文注释中文、核心包不 import fastapi、git 每任务一次提交、后端 `.venv/Scripts/python -m pytest`（262 基线）、前端 `cd frontend && npm test`（134 基线）+ `npm run build`。
- **Prompt 版本不可变**：编辑 = 插入新行（mode, version=max+1, template_text, is_active），同 mode 旧行保留；激活切换 = 单事务内"同 mode 全部置 0 + 目标行置 1"。generation_task.prompt_version 历史语义不变。
- **Prompt 管理 API 仅 admin**（require_admin）；`GET /api/v1/prompts` 维持登录可见（既有）。
- 运行配置不热切换：`GET /api/v1/config`（admin）只读返回当前 Settings 关键值（llm_model/temperature/阈值/mock 开关等，**不含 secret**）；修改路径 = 改 env 重启（文档说明）。
- 上传占位：`POST /api/v1/media/upload`（multipart `file`）——存盘名 `{uuid4().hex}{原后缀小写白名单}`（后缀白名单 .mp4/.webm/.mkv，其余 422）到 `TAGPLT_MEDIA_ROOT`（settings 新字段，默认 `data/media/`，启动建目录）；文件大小上限 2GB（413）；asset_id `asset_`+uuid8、asset_type="movie"、title=文件名去后缀、duration_ms NULL、video_uri=存盘绝对路径。
- MockLLM evidence：closed 分支解析 prompt 场景行（runner 组装格式 `- {scene_id} [{start_ms}-{end_ms}] {description}`）按哈希为每个选中标签附 1 条场景证据；open 分支同理取 1 条。**既有测试中 evidence==[] 的断言同步更新**（逐个找出并在报告中列出改动理由）。
- 部署产物（Task 1）：`deploy/` 目录——`tagplatform.service`（systemd，含 `ExecStartPre` 迁移命令）、`nginx-tagplatform.conf`（dist/ 托管 + /api 反代 + SPA 回退）、`requirements.lock`（pip freeze 自当前 venv）、`deploy.md`（runbook：装机→venv→lock 安装→迁移→build 前端→systemd/nginx 启用，逐条命令可走）。
- 阶段 4 不做：Docker、运行期配置热切换、在线搜索（模块七文档级预留）、P/R/F1、多模型供应商、分桶下沉 SQL（标签量 >10 万再议——终审 deferred 记录在案）、`skipped_approved` 改名 `skipped_finalized`（本阶段不动 results.py，backlog）。

## Review Focus

1. **Prompt 双激活/零激活竞态**：并发或失败时同 mode 出现两个 active 或零个 active → 切换必须单事务；新建版本失败不留半行。→ Task 2 测试（激活后同 mode 恰一个 active；插入失败无残留）。
2. **上传路径穿越与超大文件**：文件名含 `../` 或非法后缀 → 422（uuid 重命名天然免疫穿越，仍需白名单测试）；>2GB → 413。→ Task 4 测试。
3. **runbook 命令真实性**：deploy.md 每条命令在干净目录走查可执行（审查者/实现者执行验证）。→ Task 1 + 终审。
4. **Mock evidence 变更不破坏语义**：evidence 断言改动逐条列明；端到端冒烟证据卡有内容。→ Task 6。
5. **lock 与实际依赖一致**：requirements.lock 从当前 venv freeze，干净 venv 按 lock 安装后 pytest 全绿。→ Task 1 走查。

## 文件结构总览

```
deploy/                          # Task 1
├── tagplatform.service、nginx-tagplatform.conf、requirements.lock、deploy.md
src/tagplatform/
├── api/routers/prompts.py       # 修改：POST 新版本/激活切换（Task 2）
├── api/routers/config.py        # Task 2（只读配置端点）
├── api/routers/media.py         # Task 4（上传占位）
├── settings.py                  # 修改：+media_root（Task 4）
└── llm/mock.py                  # Task 6（场景证据）
frontend/src/
├── api/prompts.ts、config.ts、media.ts   # 契约模块（Task 3/5）
└── pages/ModelConfig.tsx        # Task 3（Prompt 管理 + 配置展示）
tests/test_api_prompts_admin.py、test_api_config.py、test_api_media.py   # 新
tests/test_llm_client.py 等既有   # Task 6 同步更新
```

---

### Task 1: 部署工程化（直接部署主线）

**Files:**
- Create: `deploy/tagplatform.service`、`deploy/nginx-tagplatform.conf`、`deploy/requirements.lock`、`deploy/deploy.md`
- Modify: `README.md`（部署段收敛指向 deploy.md）

**Interfaces:**
- Consumes: 当前 venv 依赖、`npm run build` 产物、alembic 迁移链
- Produces:
  - `tagplatform.service`：systemd 单元——`ExecStartPre` = `alembic upgrade head`（env TAGPLT_DATABASE_PATH 指向部署库）、`ExecStart` = uvicorn `tagplatform.api:create_app --factory --host 127.0.0.1 --port 8000`、`Environment=` 行含 TAGPLT_DATABASE_PATH/TAGPLT_SESSION_SECRET/TAGPLT_ADMIN_PASSWORD/OPENAI 两项占位、`Restart=on-failure`。
  - `nginx-tagplatform.conf`：`location /` → `root .../frontend/dist` + SPA `try_files ... /index.html`；`location /api` → `proxy_pass http://127.0.0.1:8000`（含 client_max_body_size 2g 与上传端点匹配）。
  - `deploy.md`：runbook（Ubuntu 假定）——装机（python3.11+/node）、clone、venv + `pip install -r deploy/requirements.lock`、迁移、前端 `npm ci && npm run build`、启用 systemd/nginx（软链命令）、验证（curl /health、登录）；附"数据备份 = 拷 SQLite 文件 + media 目录"段；附 Docker 未实现说明一句。
  - `requirements.lock`：`pip freeze` 自当前 .venv。
- **验证要求**：①lock 安装到临时 venv（`python -m venv <tmp> && pip install -r deploy/requirements.lock -e .`——注意 editable 包本身不在 freeze 里，runbook 写"lock + `pip install -e .`"两步）→ pytest 262 全绿；②deploy.md 命令逐条走查（systemd/nginx 无法在本机真启用——用 `systemd-analyze verify` 类静态校验或 nginx -t -c 走查，无法执行的步骤在报告中注明"文档走查"）。

- [ ] **Step 1-4: 产出 → 验证（lock 干净安装跑测试 + 文档走查）→ pytest/npm 不回归**
- [ ] **Step 5: 提交** — `git commit -m "feat: 直接部署工程化（systemd/nginx/lock/runbook）"`

### Task 2: Prompt 版本管理 API 与配置端点

**Files:**
- Create: `api/routers/config.py`、`tests/test_api_prompts_admin.py`、`tests/test_api_config.py`
- Modify: `api/routers/prompts.py`

**Interfaces:**
- Produces:
  - `POST /api/v1/prompts`（admin）：body `{mode: "closed"|"open", template_text: str}`——校验 template_text 非空（strip）且含 `{content}` 占位符（closed 还须含 `{candidates}`），坏 → 422；插入 `(mode, version=该 mode 现最大+1, text, is_active=1)` 且**同事务**将同 mode 其它行置 0 → 返回新行 `{mode, version, is_active, created_at}`。
  - `POST /api/v1/prompts/{prompt_id}/activate`（admin）：404 未知 id；单事务置激活（同 mode 其它置 0）→ `{prompt_id 语义行的 id?——prompt_template 表有自增 id? 核对：阶段 2 迁移 0002 的表结构（Task 2 实现时以 models.py 为准——若无独立 id 列则 activate 路由改用 (mode, version) 定位，接口形状 `{mode}/{version}/activate`）}`。**实现者先读 models.py 确认主键形状再定路由参数，报告注明选择。**
  - `GET /api/v1/config`（admin）：只读返回 `{llm_model, llm_temperature, recall_top_k_per_type, open_match_threshold, semantic_similar_threshold, generation_workers, use_mock_llm, use_mock_embedder, database_path}`（**不含 session_secret/admin_password**）。
- 覆盖测试：新版本插入+旧版去激活（恰一个 active）；激活切换幂等；422 两类（空文本/缺占位符）；404；非 admin 403；config 端点无 secret 断言。

- [ ] **Step 1-5: TDD 红绿 + 全量（262 基线）+ 提交** — `git commit -m "feat: Prompt 版本管理与只读配置端点（admin）"`

### Task 3: 模型配置中心页（前端）

**Files:**
- Create: `frontend/src/pages/ModelConfig.tsx`、`frontend/src/api/prompts.ts`、`frontend/src/api/config.ts`、对应测试
- Modify: `App.tsx`（新路由 `/model-config`，admin 守卫）+ `Layout.tsx`（菜单加"模型配置"，admin 显示——注意菜单现在是八项+用户管理，加第九项）

**Interfaces:**
- Consumes: Task 2 API（POST prompts/activate/config）
- Produces: 页面两区——①Prompt 管理：按 mode 分组 Tab（closed/open），版本 Table（version/激活 Tag/created_at/操作"激活"），"新建版本"Modal（textarea 编辑 template_text，提交 → 刷新）；②当前配置：Descriptions 只读卡（config 端点字段）+ 说明文案"修改运行配置：编辑环境变量后重启服务"。
- 组件测试：版本列表渲染、激活操作 POST 与刷新、新建 Modal 载荷与 422 提示、config 卡字段渲染。

- [ ] **Step 1-4: 组件测试先行 → 红绿 → build 绿 → pytest 不回归**
- [ ] **Step 5: 提交** — `git commit -m "feat: 模型配置中心页（Prompt 版本管理+运行配置展示）"`

### Task 4: 上传占位后端

**Files:**
- Create: `api/routers/media.py`、`tests/test_api_media.py`
- Modify: `settings.py`（+`media_root: str = "data/media/"`）、`api/__init__.py`（挂载）

**Interfaces:**
- `POST /api/v1/media/upload`（multipart `file`，**登录即可上传**——设计无此权限约束，报告注明）：后缀白名单 {".mp4", ".webm", ".mkv"}（422 `{"detail": "不支持的视频格式"}`）；大小 >2GB → 413；存盘名 `f"{uuid4().hex}{suffix.lower()}"` 到 media_root（启动/请求时确保目录存在）；建 `MediaAsset(asset_id="asset_"+uuid8, asset_type="movie", title=文件名去后缀, duration_ms=None, video_uri=str(存盘绝对路径), created_at=显式)` → `{asset_id, title, video_uri}`。无识别无 context（占位语义——前端资产列表可见、内容详情提示"未导入识别内容"）。
- 覆盖测试：成功上传（文件落盘+行入库+响应形状）、422 后缀、413 大小（构造大 Content-Length 或流式跳读——实现者选可行法）、文件名含 `../` 与中文（uuid 重命名天然免疫，断言存盘名不含原名）、401 未登录。

- [ ] **Step 1-5: TDD 红绿 + 全量 + 提交** — `git commit -m "feat: 上传占位（存文件+登记资产，不做识别）"`

### Task 5: 上传占位前端

**Files:**
- Create: `frontend/src/api/media.ts`、`AssetList.tsx` 内上传入口（Modify）、对应测试

**Interfaces:**
- Consumes: Task 4 API
- Produces: `AssetList.tsx` 顶部"上传视频"按钮 → Modal（AntD Upload 单文件 manual + 提交）→ 成功 message + 刷新列表（新行无 context 状态显示"未识别"Tag）。
- 组件测试：上传成功调 POST（FormData file 字段）+ 刷新；422/413 提示。

- [ ] **Step 1-4: 组件测试 → 红绿 → build → pytest 不回归**
- [ ] **Step 5: 提交** — `git commit -m "feat: 资产页上传入口（入口 A 占位）"`

### Task 6: MockLLM 场景证据

**Files:**
- Modify: `src/tagplatform/llm/mock.py`、受影响测试（`tests/test_llm_client.py` 等——逐个找出 evidence 断言）
- Consumes: runner 组装的 prompt 场景行格式 `- {scene_id} [{start_ms}-{end_ms}] {description}`
- Produces: closed 分支——解析 prompt 全部场景行，每个选中标签按 `sha256(evidence|run_id|tag_id)` 选一条场景行附为 evidence（scene_id/start_ms/end_ms=该行值，description 取标签描述或场景描述——定场景描述）；open 分支同理按 tag_name。输出契约仍含五键，evidence 由 [] 变为 0-1 条（场景行缺失时仍 []——title-only prompt 兜底）。
- **测试同步**：grep 所有 `evidence` 断言的既有用例，逐个更新并报告列出（例：T1 键形测试 evidence 键仍存在；端到端若断言 [] 则改为断言证据卡数据来自场景行）。端到端冒烟（uvicorn+curl 造任务）证据卡 evidence 非空自证。

- [ ] **Step 1-5: TDD 红绿（先改断言/新断言）+ 全量 + 提交** — `git commit -m "feat: MockLLM 场景证据（演示证据链完整性）"`

---

## 收尾核对（执行完成后）

- pytest 262+ 全绿、npm 134+ 全绿、build 绿；干净 venv 按 lock 安装复跑全绿。
- 双进程冒烟：上传视频（任意小文件改名 .mp4）→ 资产出现（未识别）→ 导内容建任务（MockLLM evidence 非空）→ 审核中心证据卡有场景 → 模型配置页新建 Prompt 版本并激活 → 新任务用新版本（prompt_version 变化）。
- **真实环境验收（用户配合项，非合并门槛）**：服务器按 deploy.md 走一遍部署；真实 qwen closed 任务一次；真实 mp4 播放走查。
- 更新项目记忆：平台四阶段收官；剩余遗留（Task 12 外部适配器等样例、分桶下沉等 backlog）。

## 留给实现者的决定

- Task 2 activate 路由参数形状（表主键为准）。
- Task 4 413 测试的构造方式。
- Task 6 场景行 regex 细节与兜底。
- systemd/nginx 模板的发行版适配细节（Ubuntu 假定，注释说明）。
