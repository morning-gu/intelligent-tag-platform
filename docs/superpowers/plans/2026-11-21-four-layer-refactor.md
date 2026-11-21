# 四层架构重构实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 intelligent-tag-platform 一次归位到接入/编排/服务/能力四层，搬家即清偿 P0/P1 债务，容器化部署切换。

**Architecture:** 单分支 `refactor/four-layer`（worktree 隔离，main 冻结），按层推进：迁移字面量冻结 → capabilities → services → orchestration + routers 瘦身 → 前端 → 顺修收尾；每步测试随迁随时全绿，不合入直到全部完成。

**Tech Stack:** FastAPI + SQLAlchemy + Alembic（后端），Vue/React TS + vitest（前端），Docker 多阶段镜像（部署）。

**Spec:** `docs/superpowers/specs/2026-11-21-four-layer-refactor-design.md`（已批准 2026-11-21）——目录映射、分层红线、同层规则、复用协议、装配两级化、prompt 本地化、端点白名单、顺修清单、部署 runbook 全部以 spec 为准；本计划不重复 spec 内容，只锁执行顺序与新接口。

## Global Constraints

- 分层红线：`api → orchestration → services → capabilities`，任何层不得反向 import；领域层不 import fastapi（既有红线保持）
- 同层规则：编排/能力/接入层同层互不 import；服务层领域库可被任何服务 import，有状态服务彼此零直接 import（经注册表或编排层）
- 迁移顺序硬约束：0012/0013/0016 先改字面量快照 → 删 models 常量与 PromptTemplate ORM → 新迁移编号从 **0017** 起
- prompt 本地化：无 prompt_template 表（0018 drop）；templates.py 初始值 = `db/models.py` 当前 `_PROMPT_V1_CLOSED_TEXT` 内容（0016 播种源，逐字复制）；配置页只读
- API 契约：端点改造白名单（spec §端点改造白名单）之外一切 URL/请求/响应冻结
- 不改业务语义：打标算法、评测口径、治理规则零变化；测试断言总数不减（允许搬家与契约更新，不允许删除断言）
- 每个任务完成 = 该任务涉及的测试全绿 + 全量回归绿；单分支按任务提交（commit message 前缀 `refactor:`，纯新功能用 `feat:`）

## Review Focus

1. **fresh 迁移链完整性**（0012→0018 空库顺序执行）——删活常量后历史迁移不得断链 → Task 1/3 的迁移测试 + Task 23 全链 fresh 库跑
2. **prompt 文案逐字保真**（本地化后渲染与 0016 播种版一致）——错一字即评测口径漂移 → Task 3 测试断言 `render_prompt(get_prompt("closed"), content=X)` 与常量渲染逐字一致
3. **scope 组合值解析**（`"content,scene,character"` 不再静默降级）→ Task 11 测试：组合值目录的 scope 解析出三值集合
4. **评测装配快照隔离**（run 启动后库/模板变更不影响在跑 run）→ Task 8 测试：装配 context 后修改 DB 库行，干跑产出仍用旧库
5. **孤儿任务复位**（AssetBuild/TagImport 重启后不卡 running）→ Task 18 测试：置 running 后执行复位函数，状态变 failed

---

### Task 0: 分支与基线

**Files:** 无代码变更（git 操作 + 提交设计/计划文档）

**Interfaces:**
- Produces: `refactor/four-layer` 分支（自 `origin/main` @4412b14），后续所有任务在此分支

- [ ] **Step 1: 建 worktree 并切分支**

```bash
git fetch origin && git worktree add .claude/worktrees/four-layer -b refactor/four-layer origin/main
```

- [ ] **Step 2: 基线全绿确认**

Run: `cd .claude/worktrees/four-layer && python -m pytest tests/ -q`
Expected: 全绿（约 73 个后端测试文件）；记录断言总数基线（`pytest tests/ -q --co -q | tail -1` 的用例数）供 Task 23 对比

- [ ] **Step 3: 基线前端绿确认**

Run: `cd frontend && npm ci && npm run test -- --run`
Expected: 全绿

- [ ] **Step 4: Commit（设计+计划文档入分支）**

```bash
git add docs/superpowers/specs/2026-11-21-four-layer-refactor-design.md docs/superpowers/plans/2026-11-21-four-layer-refactor.md
git commit -m "refactor: 四层重构设计与实施计划入分支"
```

### Task 1: 迁移字面量冻结（0012/0013/0014/0016）

> 执行修正（2026-11-21）：0014_tag_quality_evaluation.py 也 import 活常量，加入冻结范围（原清单漏）。残留信息：0002/0005 仍 import `PROMPT_V1_SEED`/`PROMPT_DEPRECATED_OPEN_SEED`——Task 2 迁 db/ 时若动这两个常量需同样冻结。

**Files:**
- Modify: `alembic/versions/0012_closed_prompt_grouped_candidates.py`
- Modify: `alembic/versions/0013_closed_prompt_standardized_scoring.py`
- Modify: `alembic/versions/0014_tag_quality_evaluation.py`
- Modify: `alembic/versions/0016_closed_prompt_multimodal_evidence.py`
- Test: 既有 `tests/test_migration_0013.py`、`test_migration_0016.py`（0012 的 upgrade 经 0013/0016 测试内 `upgrade(cfg, "0012")` 覆盖；0014 rehearsal 在 test_migration_0013.py 内）

**Interfaces:**
- Produces: 三个迁移文件零 `from tagplatform.db.models import`——`_PROMPT_V1_CLOSED_TEXT`/`_PREVIOUS_CLOSED_V1_TEXT` 等以字面量内联（从 models.py 当前值逐字复制）；models.py 常量**暂不删**（Task 3 删）

- [ ] **Step 1: 三个迁移文件去掉活常量 import，文案与新旧值全部内联为字面量**

- [ ] **Step 2: 验证迁移测试绿**

Run: `python -m pytest tests/test_migration_0012.py tests/test_migration_0013.py tests/test_migration_0016.py -v`
Expected: PASS

- [ ] **Step 3: Commit** — `refactor: 0012/0013/0016 迁移冻结为字面量快照（删常量前置）`

### Task 2: capabilities 层骨架 + llm/embed/adapters/storage 迁移

**Files:**
- Create: `src/tagplatform/capabilities/__init__.py`（模块 docstring 声明能力层定位与防滑坡规则）
- Move: `src/tagplatform/llm/{__init__,client,mock}.py` → `src/tagplatform/capabilities/llm/`（**parsing.py 不动**，随 Task 7 迁生成域）
- Move: `src/tagplatform/embed/` → `src/tagplatform/capabilities/embed/`
- Move: `src/tagplatform/adapters/` → `src/tagplatform/capabilities/adapters/`
- Move: `src/tagplatform/db/` + `src/tagplatform/content_io.py` 的存储读写部分 → `src/tagplatform/capabilities/storage/`（content_io 若与 content_import 强耦合可整体后移至 Task 12，此处只迁 db/；判据：import 图无反向）
- Modify: 全部消费方 import 路径（`grep -rn "tagplatform.llm\|tagplatform.embed\|tagplatform.adapters\|tagplatform.db"` 逐个更新）

**Interfaces:**
- Produces: 包路径 `tagplatform.capabilities.llm`（OpenAIClient/MockLLMClient/Transport 协议）、`tagplatform.capabilities.embed`、`tagplatform.capabilities.adapters`（register_adapter/get_adapter）、`tagplatform.capabilities.storage`（create_engine_for/make_session_factory/models）
- 注意：`db/models.py` 的对外名不变（`from tagplatform.capabilities.storage.models import ...`）；llm/parsing.py 暂留 `tagplatform/llm/parsing.py` 并保持原路径可 import（新包内 re-export 或直接更新其 import——选 re-export 保旧路径，Task 7 删）

- [ ] **Step 1: 建 capabilities 包骨架（四子包 __init__ 带 docstring）**

- [ ] **Step 2: git mv 四组文件 + 更新全库 import（含 tests/）**

- [ ] **Step 3: 全量回归**

Run: `python -m pytest tests/ -q`
Expected: 全绿（用例数 = Task 0 基线）

- [ ] **Step 4: 同层红线机械验证**

Run: `grep -rEn "^(from|import)" src/tagplatform/capabilities/ | grep -E "tagplatform\.(services|orchestration|api)"`
Expected: 零命中（能力层零出层依赖）

- [ ] **Step 5: Commit** — `refactor: capabilities 层就位（llm 传输/embed/adapters/storage）`

### Task 3: capabilities/prompts 本地化 + 0018 drop 迁移

**Files:**
- Create: `src/tagplatform/capabilities/prompts/__init__.py`、`templates.py`
- Delete: `src/tagplatform/prompts.py`（顶层）
- Create: `alembic/versions/0018_drop_prompt_template.py`（drop `prompt_template` 表；附 downgrade = 建表 + 从 `templates.py` 常量重新播种）
- Delete: `src/tagplatform/db/models.py` 中 `PromptTemplate` ORM 与 `_PROMPT_V1_CLOSED_TEXT` 常量（models 迁 storage 后在 `capabilities/storage/models.py`）
- Modify: `src/tagplatform/api/routers/prompts.py`（POST 与 activate 端点删除；GET 改只读契约视图）
- Modify: `src/tagplatform/tasks/runner.py`、`src/tagplatform/closed_eval.py`（`get_active_prompt(session, mode)` → `get_prompt(mode)`，session 参数删除）
- Test: `tests/test_prompts.py` 改造 + 新增 `tests/test_prompts_local.py`

**Interfaces:**
- Produces:
  - `get_prompt(mode: str) -> str`（返回模板文本常量；未知 mode 抛 `ValueError`）
  - `render_prompt(template_text: str, **fields: str) -> str`（纯函数，签名从 template 对象改为文本）
  - `ALLOWED_FIELDS: frozenset`、`REQUIRED_PLACEHOLDERS: dict[str, tuple]`（契约导出）
  - `templates.py`：`CLOSED_TEMPLATE_TEXT`（= 0016 播种版逐字）、`OPEN_TEMPLATE_TEXT`
  - GET `/api/v1/prompts` 响应：`[{mode, template_text, allowed_fields: [..], required_placeholders: [..]}]`（只读，无版本字段）
- 消费方更新：runner/closed_eval 渲染调用点去 session

- [ ] **Step 1: 写失败测试**（`tests/test_prompts_local.py`）：
  - `get_prompt("closed")` 与 `models.py` 旧常量渲染逐字一致（文案保真，Review Focus #2）
  - 未知 mode 抛 ValueError
  - 模板常量含必需占位符、无非法字段（原 `_validate_template_text` 语义转测试断言）
  - 契约端点 GET 返回结构与白名单字段一致

- [ ] **Step 2: 跑测试确认失败** — `python -m pytest tests/test_prompts_local.py -v` → FAIL（模块不存在）

- [ ] **Step 3: 实现**：templates.py 常量（**逐字复制 models 当前值**）+ get_prompt/render_prompt + __init__ 导出；routers/prompts.py 改只读端点；删 POST/activate 端点与 `tests/test_api_prompts_admin.py` 中对应用例（改为断言 404/405 的契约锁定测试，断言数不减）；写 0018 迁移

- [ ] **Step 4: 全量回归** — `python -m pytest tests/ -q` 全绿

- [ ] **Step 5: fresh 迁移链验证**（Review Focus #1）：临时空库跑 `alembic upgrade head`（0012→0018 全链），Expected: 无 import 错误走通；`alembic downgrade` 一级验证 0018 可回

- [ ] **Step 6: Commit** — `feat: prompt 本地化（常量+只读契约端点）+ 0018 drop prompt_template`

### Task 4: services/generation 域迁移 + TagProducer 协议补齐

**Files:**
- Create: `src/tagplatform/services/__init__.py`、`src/tagplatform/services/generation/__init__.py`
- Move: `src/tagplatform/generation/base.py` → `services/generation/base.py`
- Move: `src/tagplatform/generation/{closed.py,recall.py,open_gen.py}` → `services/generation/{closed/,open/}` 子包；`llm/parsing.py` → `services/generation/closed/parse.py`（删 Task 2 的临时 re-export）
- Move: `src/tagplatform/generation/mtg/` → `services/generation/open/mtg/`
- Move: `src/tagplatform/review/` → `services/generation/review/`
- Modify: `base.py` TagProducer 协议补 `builds_prompt: bool`、`prompt_version: str` 声明（消 getattr）
- Modify: `closed.py`/`open_gen.py` 实现补齐声明成员；`runner.py`/`assemble` 调用点改直接属性访问
- Test: `tests/test_generation_base.py` 增补

**Interfaces:**
- Produces:
  - `tagplatform.services.generation.base`：`TagProducer` 协议（`mode: str`、`produce(...)`、`builds_prompt: bool`、`prompt_version: str`）、`register_producer/get_producer`、契约 dataclass 不变
  - `tagplatform.services.generation.closed.producer.ClosedTagProducer`、`...closed.recall`、`...closed.parse.parse_closed_output`
  - `tagplatform.services.generation.open.producer`、`...open.mtg.*`
  - `tagplatform.services.generation.review.*`
- 协议违约：`register_producer` 对缺 `builds_prompt`/`prompt_version` 属性的实现抛 `TypeError`（注册时而非调用时——动态扩展承诺的护栏）

- [ ] **Step 1: 写失败测试**：注册一个缺 `prompt_version` 的假 producer → `register_producer` 抛 TypeError

- [ ] **Step 2: 确认失败** — `python -m pytest tests/test_generation_base.py -v` → FAIL

- [ ] **Step 3: 迁移 + 协议补齐**（git mv + import 更新 + 协议成员 + 注册时校验）

- [ ] **Step 4: 全量回归** — 全绿；死代码顺带删除：`replaces_pending`、`catalog_align.replaceable`、`DescriptionError`、`candidate_line`（spec 顺修清单死代码簇）

- [ ] **Step 5: Commit** — `refactor: generation 域归位 services（closed/open/review 子包）+ 协议缝补齐`

### Task 5: assemble.py 两级化 + 评测预装配

**Files:**
- Create: `src/tagplatform/services/generation/assemble.py`
- Delete from: `src/tagplatform/tasks/runner.py` 的 `assemble_spec`（迁出并改造）
- Modify: `src/tagplatform/tasks/runner.py`（改调两级函数）、`src/tagplatform/closed_eval.py`（run 启动装配一次、干跑循环用 build_spec）
- Test: 新增 `tests/test_assemble.py`；改造 `tests/test_task_runner.py`、`test_closed_eval.py` 相关用例

**Interfaces:**
- Produces:
  - `@dataclass AssemblyContext: library: TagLibrary; prompt_text: str; policy: GenerationPolicy; producer: TagProducer`
  - `load_assembly_context(mode: str, policy_json: str = "", session: Session | None = None) -> AssemblyContext`——唯一 DB 交互（try_load_library 经参数 session 或全局；prompt 走 get_prompt 常量）
  - `build_spec(context: AssemblyContext, *, content: ContentJSON, task_id: str, asset_id: str, run_id: str) -> GenerationSpec`——纯内存，零 DB
- 评测路径：`ClosedEvalService` 启动 run 时调 `load_assembly_context` 一次，干跑循环仅 `build_spec`（Review Focus #4：装配后改库行，干跑仍用旧库）

- [ ] **Step 1: 写失败测试**：① build_spec 纯内存（mock context 断言零 session 访问）② 装配快照隔离（context 装好后变更 DB 库行，产出不变）③ 生成与评测用同一对函数（导入同源断言）

- [ ] **Step 2: 确认失败** → FAIL

- [ ] **Step 3: 实现 + 双消费方改造**（runner 每任务执行时 load；closed_eval run 启动时 load 一次）

- [ ] **Step 4: 全量回归** — 全绿

- [ ] **Step 5: Commit** — `refactor: 装配两级化（load_assembly_context/build_spec）+ 评测 run 级快照`

### Task 6: persist.py 归位 + runner 纯任务管理器化

**Files:**
- Move: `src/tagplatform/results.py` → `src/tagplatform/services/generation/persist.py`（persist_tags、SYSTEM_REVIEWER；**#37 入库留痕日志逻辑照搬不丢**）
- Create: `src/tagplatform/services/generation/execute.py`：`run_generation_task(task_id, session, llm, embedder) -> TagGenerationTask`（状态机 + load_assembly_context → build_spec → produce → persist 完整执行序列，自 runner 原文搬出）
- Modify: `src/tagplatform/tasks/runner.py` 瘦身为纯任务管理器（状态机/调度/线程池/孤儿重置；执行序列只调 `execute.run_generation_task`）

**Interfaces:**
- Produces: `tagplatform.services.generation.persist.persist_tags(...)`（签名不变）、`...execute.run_generation_task(...)`
- tasks/runner 对外 API（提交/查询/重置）签名不变
- 附带归位：`src/tagplatform/filtering/` → `services/generation/filtering/`（执行序列的过滤环节）；`src/tagplatform/tag_import.py` 执行侧与 `asset_build.py` 一并收编 tasks/（执行器调用，业务逻辑进对应服务域）；`schemas/` 保持顶层契约包不动（≥3 域共享契约的既有实践）

- [ ] **Step 1: 迁移与瘦身**（执行序列整体搬出，行为零变化——现有 test_task_runner/test_runner_persist_log 全绿即证明）

- [ ] **Step 2: 全量回归** — 全绿（test_runner_persist_log 的留痕断言仍过）

- [ ] **Step 3: Commit** — `refactor: runner 纯任务管理器化 + persist/execute 归位 generation 域`

### Task 7: run_open_generation 拆分（12 小节 → 模块函数）

**Files:**
- Modify: `src/tagplatform/services/generation/open/mtg/pipeline.py`（747 行）
- Test: `tests/test_generation_open.py`（既有用例不动——拆分不改行为，全绿即验收）

**Interfaces:**
- Produces: `run_open_generation(spec, session, llm, embedder, judge_client=None)` 对外签名不变（runner/评测/测试零改动）；内部按现有 12 个编号小节注释拆为同模块私有函数（命名规则：小节语义直译，如 `_run_generation_rounds`、`merge_and_judge`、`filter_candidates`、`align_catalog`、`assign_new_tag_dirs`、`fill_descriptions`、`write_audit`——以代码内小节注释为准，合理命名即可）；共享状态经显式参数传递（消嵌套闭包）

- [ ] **Step 1: 按小节拆函数，每拆一个跑 `pytest tests/test_generation_open.py -q` 保持绿**

- [ ] **Step 2: 全量回归** — 全绿

- [ ] **Step 3: Commit** — `refactor: run_open_generation 按小节拆分（签名不变）`

### Task 8: services/tags 域（library + governance + mounting + scope 修复）

**Files:**
- Create: `src/tagplatform/services/tags/__init__.py`
- Move: `src/tagplatform/library.py` → `services/tags/library.py`（读写分侧规则入模块 docstring）
- Move: `src/tagplatform/governance/` → `services/tags/governance/`（excel.py **三处 pprint 调试残留删除**）
- Move: `src/tagplatform/mounting.py` → `services/tags/mounting.py`
- Modify: `library.py` `scope_of` 支持解析组合值（`"content,scene,character"` → `{"content","scene","character"}`；无效单值仍走父链回退）
- Test: `tests/test_library.py` 增补 scope 组合值用例；`test_governance.py` 增 pprint 消失断言（capsys 空输出）

**Interfaces:**
- Produces: `tagplatform.services.tags.library`（TagLibrary/try_load_library/library_index 等名不变）、`...governance`、`...mounting`
- scope_of 语义：组合值解析为集合（Review Focus #3）；mounting 的覆盖语义随之生效

- [ ] **Step 1: 写失败测试**：组合值目录 scope 解析出三值；治理解析不再打印 stdout

- [ ] **Step 2: 确认失败** → FAIL

- [ ] **Step 3: 迁移 + scope_of 组合值实现 + pprint 删除**
- [ ] **Step 3b: run_governance 拆分**（406 行：两个嵌套闭包共享的 6 个外层可变状态改为显式参数传递的中间结构；按判定/批量嵌入/插入/摘要分段拆函数；spec 目标 3 的第三个巨函数）

- [ ] **Step 4: 全量回归** — 全绿

- [ ] **Step 5: Commit** — `refactor: tags 域归位 + scope 组合值修复 + run_governance 拆分`

### Task 9: services/content 域

**Files:**
- Move: `content_import.py`、`content_template.py`、`ingest.py`、`movie_import.py`、`media.py`、`content_io.py`（若 Task 2 未整体迁）→ `services/content/`
- Move: `src/tagplatform/assets/` → `services/content/assets/`（builder.py 667 行：`build_movie` 拆函数、函数体内 `import threading` 提模块级、`build_many` 的 print → logger）
- Test: 既有 `test_assets_builder.py` 等（builder 拆分行为零变化，全绿即验收）

**Interfaces:**
- Produces: `tagplatform.services.content.*`（函数名不变）；`ingest.py` 与 `library.py` 的 aliases 双解析器统一为单一实现（以 library 的 JSON 优先+逗号回落为准，ingest 复用）

- [ ] **Step 1: 迁移 + aliases 解析统一（改 ingest 调 library 实现）**

- [ ] **Step 2: builder 拆分（build_movie → 读 meta/调线程池/落盘三函数）**

- [ ] **Step 3: 全量回归** — 全绿

- [ ] **Step 4: Commit** — `refactor: content 域归位 + aliases 解析统一 + builder 拆分`

### Task 10: services/auth + services/eval 域

**Files:**
- Move: `src/tagplatform/auth/` → `services/auth/`
- Move: `src/tagplatform/eval/` + `src/tagplatform/closed_eval.py` → `services/eval/`（closed_eval 1258 行拆三文件：`dataset.py`（scan/load/select）、`service.py`（ClosedEvalService 干跑运行器）、`report.py`（assemble_report/render/write）
- Move: `src/tagplatform/metrics.py` → `services/eval/metrics.py`；`src/tagplatform/vectors.py` → `capabilities/vectors.py`
- Test: 既有 auth/eval/closed_eval 测试随迁改名

**Interfaces:**
- Produces: `tagplatform.services.auth.*`、`tagplatform.services.eval.{closed_ranking,tag_quality,movie_breakdown,ranking}`、`...eval.dataset`、`...eval.service.ClosedEvalService`、`...eval.report`
- ClosedEvalService 对外方法（start/cancel/history/create）签名不变（routers/eval.py 零改动验证）

- [ ] **Step 1: 迁移 + closed_eval 三分（行为零变化，test_closed_eval 全绿即验收）**

- [ ] **Step 2: 全量回归** — 全绿

- [ ] **Step 3: Commit** — `refactor: auth/eval 域归位 + closed_eval 拆三分`

### Task 11: orchestration 层 + _shared 装配收编

**Files:**
- Create: `src/tagplatform/orchestration/__init__.py`、`_shared.py`、`generation.py`、`review.py`、`imports.py`、`tags.py`、`content.py`、`media.py`、`eval.py`、`tasks.py`
- Move: `api/routers/generation.py` 的 `_result_out`/`_tag_info`/`_parse_evidence`/`AssetTagOut`/`EvidenceOut` → `orchestration/_shared.py`（review/imports router 改从编排层 import——消跨 router 私有导入）
- Modify: 全部 15 个 routers 瘦身（读路径 SQL 与装配逻辑迁对应 orchestration 模块）

**Interfaces:**
- Produces: `tagplatform.orchestration._shared`（装配三件套，函数签名不变）；每域编排模块函数 = 原路由内嵌逻辑抽出（签名：`(session, 参数...) -> 响应模型`）
- 红线：orchestration 模块互不 import（共享走 _shared）；可 import services/capabilities

- [ ] **Step 1: _shared 收编三件套 + review/imports 改 import + 全量回归绿**

- [ ] **Step 2: 逐域抽编排函数（tags/generation/review/eval/content/imports/media/tasks/config），每域抽完跑该域 API 测试**

- [ ] **Step 3: 同层红线验证** — `grep -rEn "from tagplatform.orchestration.[a-z_]+ import" src/tagplatform/orchestration/ | grep -v _shared` 零命中

- [ ] **Step 4: 全量回归** — 全绿

- [ ] **Step 5: Commit** — `refactor: 编排层就位，15 路由瘦身为薄路由`

### Task 12: 端点白名单改造（性能/安全/契约）

**Files:**
- Modify: `orchestration/tags.py`（list：SQL LIMIT/OFFSET + total；export 流式分批）、`api/routers/tags.py`（响应模型加 total）、`api/schemas.py`
- Create: `alembic/versions/0017_import_batch_key.py`（TagChangeLog 加 `task_id` 列 + 索引，**先于 0018 编号**——注意 Task 3 已写 0018，此处若已占用则本迁移改 0018、drop 改 0019，按实际未占用编号顺延即可）；Modify: `orchestration/imports.py`（批次反查 SQL 化）
- Modify: `orchestration/generation.py`（`_dir_info` 端点级单次库加载，消 N+1）
- Modify: `api/routers/media.py`（进度页 HTML → 静态文件 + `html.escape` 转义替换）
- Modify: `api/routers/auth.py`（samesite=lax；用户不存在时 dummy verify 抹平时序）、`users.py`（建号 IntegrityError → 409）
- Test: `tests/test_api_tags.py`（total 断言）、`test_api_imports.py`、`test_api_media.py`、`test_auth_api.py` 增补

**Interfaces:**
- Produces: `GET /api/v1/tags` 响应 `TagListResponse{items, total}`；imports 批次反查走索引列；其余行为兼容

- [ ] **Step 1: 写失败测试**：tags list 返回 total 且 SQL 分页（大偏移量返回空列表+正确 total）；建号竞态 409；samesite 头断言

- [ ] **Step 2: 确认失败** → FAIL

- [ ] **Step 3: 实现五项改造 + 新迁移**

- [ ] **Step 4: 全量回归 + fresh 迁移链**（0012→最新全链空库走通）

- [ ] **Step 5: Commit** — `feat: 端点白名单改造（分页/索引/N+1/静态进度页/auth 加固）`

### Task 13: 孤儿任务重置

**Files:**
- Modify: `src/tagplatform/tasks/runner.py`（启动时对 AssetBuildTask/TagImportTask 的 running 行复位 failed，复用 generation `reset_orphan_running` 模式）
- Test: 新增 `tests/test_orphan_reset.py`

**Interfaces:**
- Produces: `reset_orphan_tasks(engine)` —— app 启动（`api/__init__.py` lifespan）调用

- [ ] **Step 1: 写失败测试**：置 running 行 → 调 reset → 状态 failed（Review Focus #5）**

- [ ] **Step 2: FAIL → 实现 → PASS → 全量回归 → Commit** — `feat: AssetBuild/TagImport 孤儿任务启动复位`

### Task 14: 前端 7 模块归位 + 常量统一

**Files:**
- Move: `frontend/src/pages/*` → `frontend/src/pages/{tag-management,content-management,generation,review,closed-eval,config}/`（open-eval 只建目录占位不写代码）
- Create: `frontend/src/shared/labels.ts`（ORIGIN/TARGET/review 三态 labels/colors 单一来源）
- Delete: `frontend/src/api/library.ts`（死模块）
- Modify: ModelConfig.tsx 只读视图（提示文案从 GET /prompts 契约拉取）；`api/client.ts` 等 413 判断改 `ApiError.status`；`api/tags.ts` 响应加 total；查询串拼装统一 helper
- Test: `frontend/src/**/*.test.ts(x)` 随迁 + ModelConfig 契约测试改造

**Interfaces:**
- Produces: `shared/labels.ts` 导出 `ORIGIN_LABELS/ORIGIN_COLORS/TARGET_TYPE_LABELS/REVIEW_STATUS_LABELS` 等常量

- [ ] **Step 1: 目录归位 + import 路径更新 + `npm run test -- --run` 绿**

- [ ] **Step 2: 常量统一 + ModelConfig 只读化 + api client 三项修正 + 测试绿**

- [ ] **Step 3: Commit** — `refactor: 前端 7 模块归位 + 常量单一来源 + ModelConfig 只读`

### Task 15: AssetList 拆分

**Files:**
- Modify: `frontend/src/pages/content-management/AssetList.tsx`（851 行）
- Create: 同目录 `AssetUploadWizard.tsx`、`AssetProgressPolling.tsx`、`AssetDirImportModal.tsx`（列表/上传向导/轮询/目录导入四职责；open/close 的 11 个 state 复位收进 wizard 自身 useEffect 清理）

**Interfaces:**
- Produces: AssetList 保留页面骨架与对外路由；三个子组件 props 由拆分者定（合理选择即可），行为零变化（既有 AssetList 测试全绿即验收）

- [ ] **Step 1: 逐组件抽出（每抽一个跑相关测试）**

- [ ] **Step 2: `npm run test -- --run` 全绿 → Commit** — `refactor: AssetList 拆四子组件`

### Task 16: 顺修清单收尾（DRY/死代码/杂项）

**Files:**
- Create: `src/tagplatform/services/generation/open/mtg/normalization.py`（归一化键单一实现，merge/judge/pipeline 改引用）
- Create: `services/content/assets/lexicon.py`（噪音词表统一，dataset 与 asset_build 共用）
- Modify: `_PLACEHOLDER_MARKS` 收单一来源（services/tags/library 与 catalog_align 共用）；`_content_texts`/`_encode_image`/usage 累加×5 → 单一 helper；`_make_judge_client`/`_make_verify_client` 参数化合并；`isinstance(MockLLMClient)` → 协议方法判定；judge/verify/descriptions 提示词字面量提为模块级命名常量（与解析器相邻声明协议）；`settings.py:157` 注释校正；`library.py:391` 缓存键改 `embedder.name`；governance 死接口（GovernanceConfig/Issue.similar_to）删除
- Delete: `scripts/reembed_catalog.py` + 修正 settings.py/catalog_align.py 三处指向它的注释（改指 `src/tagplatform/reembed.py`）

**Interfaces:**
- Produces: `normalization.py: normalize_key(text: str) -> str`；`lexicon.py: RELEASE_GROUP_NOISE: frozenset[str]`

- [ ] **Step 1: 逐项修（每项跑对应测试域）——归一化键统一后跑 test_generation_closed/open/judge 相关全量**
- [ ] **Step 2: 全量回归绿 → Commit** — `refactor: 顺修清单收尾（DRY 统一/死代码清除/杂项）`

### Task 17: 验收与红线总检

**Files:**
- Create: `docs/superpowers/plans/2026-11-21-deploy-runbook.md`（部署/回退 runbook 落文，按 spec 执行策略 §4-5）

**Interfaces:** 无代码

- [ ] **Step 1: 分层红线全量 grep**（api→orch→services→capb 无反向；orchestration 同层零互依；routers 零互 import；能力层零出层）——四条 grep 全部零命中
- [ ] **Step 2: 全量后端+前端测试** — 全绿，用例数 ≥ Task 0 基线（断言不减）
- [ ] **Step 3: fresh 迁移链空库全链** — `alembic upgrade head` + downgrade 一级（0018/0019 可回）
- [ ] **Step 4: 白名单外端点契约快照**（对既有 API 测试全绿确认；部署时 smoke 按 runbook）
- [ ] **Step 5: runbook 文档化 + Commit** — `docs: 四层重构部署 runbook`
- [ ] **Step 6: 分支终态推远端开 PR**（合入 main 解冻，进入 Task 17 后的部署窗口）
