# v2 表结构全平台切换 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按 `docs/table_design.md`（v2）一次性切换全平台数据结构：11 张新表 + 全部后端服务/路由 + 前端 API 与页面同步改绿，服务器一次 `alembic upgrade head` 后平台即恢复可用。

**Architecture:** 先重写 ORM 模型与 0007 重建迁移（地基），再按域纵切（内容/治理导入/目录/closed 生成/open 生成/审核/指标），每域带各自路由与测试；后端全量绿后重约前端 API client，逐页改造；两端全量绿 + build 收官。**中间任务完成后全量套件是红的（预期）**——每任务以"本域测试文件全绿"为完成标准，Task 11 / Task 16 恢复全量绿。执行者见此情况不要修别的域的测试。

**Tech Stack:** FastAPI + SQLAlchemy ORM + alembic（SQLite，非事务 DDL）/ pytest；React 19 + AntD 6 + Vite + Vitest。

**Spec:** `docs/table_design.md`（含 2026-11-05 四处口径修正：content_json 契约本期不变、召回文本按现行 `embed_content_texts`、pending 删除限同 origin、`open_match_threshold` 为 mtg 归一命中阈值）

## Global Constraints

- 表/列/枚举/索引**逐字**按 `docs/table_design.md` §3 DDL；11 表：media_asset、tag_catalog、tag_define、tag_generation_task、pre_asset_tag、asset_tag、tag_import_task、tag_change_log、prompt_template、user_account、asset_build_task。
- **预留不启用**：`target_type` 只写 `'content'`（target_id = asset_id）、标签只 `val_fixed`；流水线与前端不出现 scene/character/泛化值。
- `user_account` / `prompt_template` / `asset_build_task` **物理不动，ORM 保持 v1 形状**（doc §3.9–3.11 的 CHECK 是 PG 目标形态，本期不加）。
- models.py 重写**必须保住导出** `PROMPT_V1_SEED`、`PROMPT_DEPRECATED_OPEN_SEED` 与 after_create 种子钩子（alembic 0002/0005 import 它们；全新库 0001→head 链不能断）。
- 重跑语义（doc §6.1 修正版）：写结果前删除该资产**同 origin** 的全部 pending 行，再逐行 `INSERT OR IGNORE`；approved/rejected 行永不被覆盖。
- `review_status` 只有 pending/approved/rejected；merge 终态 = approved。origin = 任务模式 `closed|open`（v1 `catalog|generated` 词汇退役；**open 任务里的归一命中行也 origin=open**）。
- **content_json 契约本期不变**：沿用现行适配器形状（`content.summary`、`scenes[].time_range`，见 `src/tagplatform/schemas/content.py`）；doc §4 示例是外部适配器目标契约，不实现。
- closed 提示词模板冻结（PROMPT_V1_SEED 文案不改）；候选行渲染保持 4 字段格式 `- {tag_id} | {tag_name} | {分类} | {定义}`，第三字段由 tag_type 换成**父目录名**（MockLLM 契约依赖此格式）。
- API 口径：概念未变的 URL 不动（/assets、/tags、/generation/tasks、/review、/media、/prompts、/users、/config、/auth、/asset-tags/{id}/review）；变的按"契约速查"执行。
- 服务器业务数据已清零：**不做业务数据迁移**；0007 重建 + 仅保 user_account/prompt 种子（doc §8）。
- 每任务完成即 commit（conventional commits，中文描述）；后端跑法 `python -m pytest tests/<file> -v`（venv），前端 `npm test -- <file>`（frontend/）。
- 术语：状态字面量 v1 `"deprecate"` → v2 `"deprecated"`（治理/生命周期/前端三处同步，测试断言注意）。

## 契约速查（接口定稿，全任务共用）

**ORM 类（src/tagplatform/db/models.py，Task 1 产出）：** `MediaAsset`(asset_id, asset_type, parent_id, title, duration_ms, video_uri, **content_json**, **source**, created_at)、`TagCatalog`(dir_id, dir_name, dir_type, description, parent_id, aliases, applicable_scope, embedding, created_at, updated_at)、`TagDefine`(tag_id, tag_name, tag_type, description, **dir_id**, aliases, status, embedding, created_at, updated_at)、`TagGenerationTask`(task_id, **asset_id**, mode, model_version, prompt_version, policy_json, status, error, created_at, finished_at)、`PreAssetTag`(id, asset_id, target_type, target_id, tag_id, candidate_name, candidate_dir_id, candidate_description, origin, relevance_score, confidence, evidence_json, task_id, review_status, reviewer_id, review_comment, created_at, updated_at)、`AssetTag`(id, asset_id, target_type, target_id, tag_id, tag_name, created_at, updated_at)、`TagImportTask`(task_id, file_name, status, result_summary, created_at)、`TagChangeLog`（形状同 v1，枚举 create/update/merge/deprecate/import）。保留：`PromptTemplate`、`UserAccount`、`AssetBuildTask`（v1 形状）、`Base`、两个种子常量、`_seed_prompt_v1`。

**服务函数签名：**

```python
content_import.import_content(content: ContentJSON, session, video_root: str | None, raw: dict | None = None) -> ImportResult  # (asset_id, scene_asset_ids) — context_id 移除
governance.pipeline.run_governance(rows: list[TagRow], session, embedder, config: GovernanceConfig | None = None) -> GovernanceResult  # (task_id, inserted, issues)
generation.recall.recall_by_dir(session, queries: list[str], embedder, top_k: int) -> list[TagDefine]  # 按 dir_id 分组各取 top-k
results.persist_tags(session, asset_id: str, task_id: str, tags: list[ProducedTag], run_id: str) -> PersistSummary  # (inserted, deleted_pending, skipped_finalized)
tasks.runner.run_generation_task(task_id: str, session, llm, embedder) -> TagGenerationTask
tasks.runner._clear_pending(session, asset_id: str, origin: str) -> int   # 删同资产同 origin 的 pre pending 行
review.service.apply_review(session, pre_tag_id: int, action: str, reviewer: str, payload: dict | None) -> PreAssetTag
review.service.refresh_mounted_tag_names(session, tag_id: str) -> int     # doc §6.3 刷新规则
metrics.compute_metrics(session) -> dict / confidence_distribution(session, buckets=10) / tag_dir_coverage(session) -> list[dict]
reembed.reembed_all(session, embedder) -> ReembedSummary  # (dir_rows, tag_rows)
```

**API v2 变更表（其余端点不动）：**

| 端点 | v1 | v2 |
|---|---|---|
| GET `/api/v1/tags` | TagOut（tag_id/tag_type…） | 混合树节点 `TagNodeOut{id, name, kind: dir\|tag, dir_type?, description, parent_id, status, has_children, aliases}`；query: q/parent_id/root/scope_id/kind/limit/offset（q 同时匹配目录与标签的 name+aliases） |
| GET `/api/v1/tags/{id}` | TagDetail | `TagNodeDetail`（同上 + path/path_ids；id 可为 dir_id 或 tag_id） |
| POST `/tags/{tag_id}/deactivate·activate`、DELETE `/tags/{tag_id}` | 同 | 只作用于 tag_define（id 是目录 → 404）；删除前查 asset_tag 引用；prune 空祖先目录；TagChangeLog |
| GET `/api/v1/tags/export` | zip | 不变；路径推导改跨双表（叶子=tag_define，祖先=tag_catalog） |
| POST `/api/v1/tags/import` | ImportResponse{batch_id, summary} | `ImportResponse{task_id, summary}`（结构不变） |
| `/api/v1/import/batches*` | 4 端点 | **改 `/api/v1/import/tasks`**（list/详情/导出 zip/撤销），字段 batch_id→task_id，语义同 |
| GET `/api/v1/assets/{id}` | + contexts[] | `AssetDetail{...asset 字段, recognized: bool}`（contexts 消失） |
| GET `/api/v1/assets/{id}/contexts/{cid}` | 全量 content | **删除**；新增 GET `/api/v1/assets/{id}/content` → ContentJSON（content_json 为空→404） |
| POST `/api/v1/content/import` | +context_id | 响应 `{asset_id, scene_asset_ids}` |
| GET `/api/v1/assets/{id}/tags` | 全部行（含评分/证据） | **仅 approved 挂载**：`[{id, tag_id, tag_name, origin, created_at}]`（评分/证据在 pre 侧） |
| GET `/generation/tasks` | asset_id 经 context 外连 | asset_id 直列 |
| GET `/generation/tasks/{id}` | results 来自 asset_tag | results 来自 pre_asset_tag：`PreTagOut{id, tag_name, tag_id?, candidate_name?, origin(closed\|open), relevance_score?, confidence?, review_status(3 态), evidence}` |
| GET `/api/v1/review/items` | 7 过滤（tag_type/mode/origin） | 过滤参数：status/confidence_min/confidence_max/**dir**/**origin**/asset_id/page/page_size；`ReviewItemOut{..., dir_name?, origin}`（tag_type/mode 列消失） |
| GET `/api/v1/review/filters` | {tag_types, statuses, modes} | `{statuses: [pending,approved,rejected], origins: [closed,open], dirs: [str]}` |
| POST `/asset-tags/{id}/review`（含 batch） | 4 动作 | URL 不动；payload：approve/reject `{comment?}`；modify `{candidate_name?, candidate_description?, comment?}`（更新后**直接 approve**）；merge `{merge_to_tag_id}` **或** `{new_tag_name, new_dir_id, new_description?, comment?}` |
| GET `/api/v1/metrics/summary` | review_stats=动作分布 | review_stats = `{approved, rejected, merged}` 的 {count, ratio}（merged 取自 tag_change_log action='merge'；modified 退役）；pending_count/生成质量/任务统计源换 pre/TagGenerationTask，公式不变 |
| GET `/api/v1/metrics/tag-type-coverage` | 按 tag_type | **改 GET `/api/v1/metrics/dir-coverage`**：`[{dir_name, total, approved}]`（无目录归属的 open 候选归"未归置"桶） |

**导入落位规则（Task 4）：** 层级 Excel/XML 的一/二/三级 → tag_catalog 目录（一律 dir_static，applicable_scope 默认 content）；「标签」列 → tag_define（val_fixed）挂最近目录；**扁平导入（无层级）落入自动创建的根目录「未分类」**（dir_static，幂等复用）。重复/别名/父链/相似检测语义沿用，键改为 (dir, name)；相似检测 embedder 用 passage 角色对 define 文本。

## Review Focus

1. **closed 重跑不清 open 待审行**：同资产先 open 后 closed 两个任务，open 的 pending 候选必须原样保留 → Task 6 `test_task_rerun.py::test_rerun_scoped_by_origin`。
2. **重跑不动终态**：approved/rejected 的 pre 行与 asset_tag 挂载在重跑后逐字段不变，pending 行被新证据替换 → Task 6 `test_task_rerun.py::test_rerun_preserves_finalized`。
3. **merge 并入既有撞同名**：候选名与库内某标签同名时走并入既有（赋 tag_id），不得触发 ux_tag_define_dir_name 唯一冲突 → Task 8 `test_review_service.py::test_merge_into_existing_same_name`。
4. **未识别资产跑生成**：content_json 为空的资产提交 closed 任务 → 任务 failed、error 含"未完成内容识别"，API 不 500 → Task 6 `test_task_runner.py::test_task_fails_when_asset_unrecognized`。
5. **层级导入混合落位**：目录下同时有子目录与叶子标签、三级链缺级、扁平行 → 全部正确落双表且 (dir,name) 重复被 skipped+issue → Task 4 `test_governance.py::test_hierarchical_split_and_flat_fallback`。

---

### Task 1: v2 ORM 模型重写

**Files:**
- Modify: `src/tagplatform/db/models.py`（全量重写）
- Test: `tests/test_models.py`（重写；`tests/test_db.py` 应保持绿）

**Interfaces:**
- Produces: 契约速查所列 8 个新/改模型类 + 保留 3 类 + `Base` + `PROMPT_V1_SEED`/`PROMPT_DEPRECATED_OPEN_SEED`/`_seed_prompt_v1`。时间戳列一律 `DateTime`（应用层赋值）。pre_asset_tag 的两个部分唯一索引用 `Index(..., unique=True, sqlite_where=text(...))`（沿用 v1 写法）。

- [ ] **Step 1: 写失败测试** — `test_models.py` 重写：枚举 CHECK（dir_type/tag_type/review_status/origin/status 各取合法值入库 + 非法值 assert IntegrityError）、`ux_tag_define_dir_name`（同 dir 同名冲突、跨 dir 允许）、`ux_tag_catalog_parent_name`（parent 作用域唯一）、`ux_pre_tag_catalog`/`ux_pre_tag_candidate`（含 NULL 语义：tag_id NULL 行不受 catalog 索引约束）、`ux_asset_tag` 全键唯一、PreAssetTag→TagDefine/TagGenerationTask/MediaAsset 外键、prompt 种子钩子（create_all 后 prompt_template 恰 3 行且 closed v1 active）。
- [ ] **Step 2: 跑测试确认失败** — `python -m pytest tests/test_models.py tests/test_db.py -v` → FAIL（新类不存在 / 旧断言失效）
- [ ] **Step 3: 重写 models.py** — 按 doc §3 DDL 逐字实现 11 类；删除 MediaContext/ReviewRecord/ImportBatch/GenerationTask/v1-AssetTag/v1-TagCatalog；PromptTemplate/UserAccount/AssetBuildTask 与种子区**原样搬运**（含注释）；模块 docstring 更新为"11 张 v2 表"。
- [ ] **Step 4: 跑测试确认通过** — 同 Step 2 命令 → PASS（其余域测试此时红属预期，不修）
- [ ] **Step 5: Commit** — `git add src/tagplatform/db/models.py tests/test_models.py && git commit -m "feat(v2): ORM 模型重写（目录/标签拆分、pre/正式分离、media 合并）"`

### Task 2: alembic 0007 重建迁移 + 带数据彩排

**Files:**
- Create: `alembic/versions/0007_v2_schema_rebuild.py`
- Test: `tests/test_migration_0007.py`

**Interfaces:**
- Consumes: Task 1 模型（用于对拍 DDL）。
- Produces: revision `"0007"`，down_revision `"0006"`。upgrade：drop（review_record、asset_tag、tag_catalog、import_batch、generation_task、media_context、tag_change_log）→ `media_asset` add content_json/source 列 → create（新 tag_catalog、tag_define、pre_asset_tag、asset_tag、tag_generation_task、tag_import_task、tag_change_log）。downgrade：反向重建 v1 空表（数据不回迁，注释写明）。

- [ ] **Step 1: 写失败测试** — `test_migration_0007.py`：临时 SQLite 库经 alembic 程序化 `upgrade` 到 0006 → 插 v1 数据（user_account 1 行、tag_catalog 父/子、media_asset+media_context、asset_tag 2 行、tag_change_log 1 行）→ `upgrade head` → 断言：旧业务表全消失；user_account 行与 prompt 3 行种子**原样保留**；新表结构可插入合法行/拒绝非法枚举；`pragma foreign_key_check` 为空。另测全新空库 0001→head（种子链 0002/0005 import 不断）。
- [ ] **Step 2: 跑测试确认失败** — `python -m pytest tests/test_migration_0007.py -v` → FAIL（0007 不存在，head=0006）
- [ ] **Step 3: 实现 0007** — 手写迁移（doc §8；沿用 0006 的 PRAGMA foreign_keys=OFF/ON 包裹；注意该环境非事务 DDL，PRAGMA 真实生效）。`alembic upgrade head` + `alembic history` 本地自查双头。
- [ ] **Step 4: 跑测试确认通过** — Step 1 命令 → PASS
- [ ] **Step 5: Commit** — `git add alembic/versions/0007_v2_schema_rebuild.py tests/test_migration_0007.py && git commit -m "feat(v2): 0007 表结构重建迁移 + 带数据彩排测试"`

### Task 3: 内容层（content_import / asset_build / content 路由）

**Files:**
- Modify: `src/tagplatform/content_import.py`、`src/tagplatform/asset_build.py`、`src/tagplatform/api/routers/content.py`、`src/tagplatform/api/__init__.py`（仅当需改装配）、`scripts/ingest_movienet.py`
- Test: `tests/test_content_import.py`（重写）、`tests/test_content_io.py`、`tests/test_api_content.py`、`tests/test_api_media.py`、`tests/test_api_asset_build.py`

**Interfaces:**
- Consumes: Task 1 `MediaAsset.content_json/source`、`content_io.load_content`（逻辑不变）。
- Produces: `ImportResult(asset_id, scene_asset_ids)`；content 路由新形状（契约速查：AssetDetail.recognized、GET /assets/{id}/content、import 响应去 context_id）。asset_build 复用检查改读 `MediaAsset.source`，`_purge_contexts` 删除（重识别=直接覆盖 content_json）。

- [ ] **Step 1: 写失败测试** — test_content_import：导入后 media_asset.content_json/source 落库、media_context 表不存在、**重复导入覆盖** content_json、scene 子资产照旧、ImportResult 无 context_id。test_api_content：AssetDetail 带 recognized、/assets/{id}/content 200/404（未识别）、contexts 端点 404（路由删除）、import 响应无 context_id。
- [ ] **Step 2: 跑测试确认失败** — `python -m pytest tests/test_content_import.py tests/test_content_io.py tests/test_api_content.py tests/test_api_media.py tests/test_api_asset_build.py -v` → FAIL
- [ ] **Step 3: 实现** — import_content 直写 asset 行（session.get(MediaAsset) 更新 content_json/source）；asset_build 复用分支与进度回调改新源；content 路由按契约速查重写（含 404 语义：content_json 为空→"资产未完成内容识别"）。
- [ ] **Step 4: 跑测试确认通过** — 同命令 → PASS
- [ ] **Step 5: Commit** — `git commit -m "feat(v2): 内容层合并——content_json 落 media_asset、contexts 端点退役"`

### Task 4: 治理导入拆分（run_governance + import 路由）

**Files:**
- Modify: `src/tagplatform/governance/pipeline.py`、`src/tagplatform/api/routers/tags.py`（仅 import 端点）、`src/tagplatform/api/routers/batches.py` → 重命名 `imports.py`、`src/tagplatform/api/__init__.py`（路由注册改名）
- Test: `tests/test_governance.py`（重写）、`tests/test_embed_roles.py`（治理部分）、`tests/test_api_tags.py`（import 用例）、新增 `tests/test_api_imports.py`（由 test_api_tags 的批次用例迁移）

**Interfaces:**
- Consumes: Task 1 双表模型；`governance/models.py` 的 TagRow（解析层不改）。
- Produces: `run_governance -> GovernanceResult(task_id, inserted, issues)` 写 tag_import_task；导入落位规则（契约速查：层级→目录+标签、扁平→「未分类」、status "deprecated"）；imports 路由的 list/详情/revoke 三端点（**export 端点与 `_build_export_archive` 双表化归 Task 5**，本任务先注册路由其余端点）；POST /tags/import 响应 {task_id, summary}。

- [ ] **Step 1: 写失败测试** — test_governance：三级层级导入→2 目录+1 标签（(dir,name) 正确）、扁平导入→「未分类」目录幂等、重复 (dir,name) skipped+issue、相似检测 similar_to（MockEmbedder 同向量）、tag_change_log action='import' 带 task_id。test_api_imports：list/详情/撤销 三端点路径/形状/撤销（含 409 分支：非 completed、已被挂载引用）。
- [ ] **Step 2: 跑测试确认失败** — `python -m pytest tests/test_governance.py tests/test_api_imports.py -v` → FAIL
- [ ] **Step 3: 实现** — run_governance 双表写入（目录链按 parent_tag 链创建，幂等复用）；GovernanceConfig 保留 similar_threshold，allowed_tag_types 校验退役；router 重命名+前缀 /api/v1/import/tasks，revoke/export 语义照 v1 迁移（change_log 扫描 + asset_tag 引用检查）。
- [ ] **Step 4: 跑测试确认通过** — 同命令 + `python -m pytest tests/test_api_tags.py -v`（import 用例）→ PASS
- [ ] **Step 5: Commit** — `git commit -m "feat(v2): 治理导入拆分目录/标签双表，批次改导入任务"`

### Task 5: 目录树与生命周期（tags 路由 + 刷新助手）

**Files:**
- Modify: `src/tagplatform/api/routers/tags.py`（列表/详情/导出/生命周期）、`src/tagplatform/review/service.py`（追加 `refresh_mounted_tag_names`）
- Test: `tests/test_api_tags.py`（重写非 import 部分）、`tests/test_review_service.py`（仅刷新助手用例，其余 Task 8 重写）

**Interfaces:**
- Consumes: Task 1 双表、Task 4 导入（数据准备）。
- Produces: `TagNodeOut`/`TagNodeDetail`（契约速查）；`_build_export_archive` 跨双表路径推导（叶子=tag_define、祖先=tag_catalog）**连带 GET /api/v1/import/tasks/{task_id}/export 端点一并落地**（tags.py 与 imports.py 共用此构建器）；生命周期三端点（仅 tag_define；DELETE 查 asset_tag 引用→409、prune 空祖先、返回 parent 重定向信息）；`refresh_mounted_tag_names(session, tag_id) -> int`（改 tag_define.tag_name 后全量刷新 asset_tag.tag_name；本期无 rename 端点，助手+测试即交付）。

- [ ] **Step 1: 写失败测试** — 混合树：parent_id 下同时返回子目录+叶子标签（kind 正确、has_children 对目录）、root/scope_id/q（目录与标签名+别名都命中）/kind 过滤；详情 path/path_ids；deactivate/activate（status "deprecated"/"active" + change_log）；DELETE 引用 409 / 无引用成功+剪空目录；**导出双端点**（/tags/export 与 /import/tasks/{id}/export）zip 含 xlsx+xml（叶子行带祖先路径）；刷新助手：造 3 条 asset_tag 挂载→改 define.tag_name→刷新→3 行 tag_name 同步。
- [ ] **Step 2: 跑测试确认失败** — `python -m pytest tests/test_api_tags.py -v` → FAIL
- [ ] **Step 3: 实现** — tags.py 按 TagNodeOut 重写查询（目录与标签 UNION 语义在 Python 侧合并即可，排序：目录先、名字序）；生命周期/导出/刷新助手按接口。
- [ ] **Step 4: 跑测试确认通过** — 同命令 → PASS
- [ ] **Step 5: Commit** — `git commit -m "feat(v2): 目录树混合节点 API + 生命周期 + tag_name 刷新规则"`

### Task 6: closed 生成链（recall / closed / runner / results / generation 路由）

**Files:**
- Modify: `src/tagplatform/generation/recall.py`、`src/tagplatform/generation/closed.py`、`src/tagplatform/generation/base.py`（origin 词表注释）、`src/tagplatform/tasks/runner.py`、`src/tagplatform/results.py`、`src/tagplatform/api/routers/generation.py`
- Test: `tests/test_results.py`（重写）、`tests/test_task_runner.py`、`tests/test_task_rerun.py`（重写，含 Review Focus 1/2）、`tests/test_generation_closed.py`、`tests/test_embed_roles.py`（召回部分）、`tests/test_content_render.py`、`tests/test_api_generation.py`

**Interfaces:**
- Consumes: Task 1 模型、Task 3 内容（asset.content_json）。
- Produces: `recall_by_dir(session, queries, embedder, top_k) -> list[TagDefine]`（按 dir_id 分组各取 top-k；过滤 status!='deprecated' 且 embedding 非空）；`persist_tags -> PersistSummary(inserted, deleted_pending, skipped_finalized)`（**删除 pending 由 runner 的 `_clear_pending` 负责**——调用时机沿用 v1 位置；persist_tags 只做 INSERT OR IGNORE；FINALIZED_STATUSES=("approved","rejected")）；`run_generation_task -> TagGenerationTask`（asset 取 content_json，NULL→failed"资产未完成内容识别"；删 context.embedding 回填；_render_candidates_text 第三字段=父目录名）；generation 路由（契约速查：tasks 直连 asset_id、detail results 取 pre、/assets/{id}/tags 仅 approved 挂载）。ProducedTag.origin 词表 closed|open（candidate_type 字段保留供 mtg 审计，不入库）。

- [ ] **Step 1: 写失败测试** — test_results：首次写入 inserted=N/deleted_pending=0；同标签二轮（旧 pending）→删旧插新、分数/证据为新值；旧 approved/rejected→skipped_finalized 且行不动；origin 值 closed。test_task_rerun：**Review Focus 1/2 两用例**（同资产 open→closed 顺序、终态逐字段比对）。test_task_runner：未识别资产→failed+error 文案（**Review Focus 4**）；状态机/executor/orphan reset 照 v1 断言换新表。test_generation_closed：召回按目录分组、候选文本 4 字段第三段为目录名、MockLLM 往返。test_api_generation：创建/列表/详情/retry + /assets/{id}/tags 仅挂载。
- [ ] **Step 2: 跑测试确认失败** — `python -m pytest tests/test_results.py tests/test_task_runner.py tests/test_task_rerun.py tests/test_generation_closed.py tests/test_embed_roles.py tests/test_content_render.py tests/test_api_generation.py -v` → FAIL
- [ ] **Step 3: 实现** — 按接口逐文件改；runner 的执行流保持 v1 状态机形状，只换数据源与写入口。
- [ ] **Step 4: 跑测试确认通过** — 同命令 → PASS
- [ ] **Step 5: Commit** — `git commit -m "feat(v2): closed 生成链切换 pre_asset_tag + 按目录召回"`

### Task 7: open 生成链（mtg / open_gen）

**Files:**
- Modify: `src/tagplatform/generation/mtg/pipeline.py`（`_catalog_hits`）、`src/tagplatform/generation/open_gen.py`
- Test: `tests/test_generation_open.py`

**Interfaces:**
- Consumes: Task 1 `TagDefine`、Task 6 ProducedTag 词表。
- Produces: `_catalog_hits` 查 TagDefine（embedding 余弦 ≥ `open_match_threshold`，settings 语义不变）；open 任务所有行 origin="open"——**归一命中行带 tag_id+relevance，自由候选行带 candidate_name**（命中行 origin=open 而非 closed，作为硬断言）。mtg 其余文件（judge/keyframes/merge/parse/prompts）不动。

- [ ] **Step 1: 写失败测试** — 归一命中行断言 origin=="open" 且 tag_id 非空；自由候选行 candidate_name 非空、tag_id 空；命中阈值边界（≥命中、<不命中）用 MockEmbedder 构造。
- [ ] **Step 2: 跑测试确认失败** — `python -m pytest tests/test_generation_open.py -v` → FAIL
- [ ] **Step 3: 实现** — _catalog_hits 换表 + origin 统一 open；open_gen 产出行构造调整。
- [ ] **Step 4: 跑测试确认通过** — 同命令 → PASS
- [ ] **Step 5: Commit** — `git commit -m "feat(v2): open 生成链归一命中改查 tag_define，origin 统一 open"`

### Task 8: 审核服务与路由（apply_review v2）

**Files:**
- Modify: `src/tagplatform/review/service.py`、`src/tagplatform/api/routers/review.py`
- Test: `tests/test_review_service.py`（重写）、`tests/test_api_review.py`

**Interfaces:**
- Consumes: Task 1 PreAssetTag/AssetTag/TagDefine、Task 5 `refresh_mounted_tag_names`（定义侧将来改名时挂载行同步的契约助手，本任务不调用）。
- Produces: `apply_review(session, pre_tag_id, action, reviewer, payload) -> PreAssetTag`（doc §6.2 映射表；终审行再审→ConflictError；modify=更新候选字段后直接 approve；approve/merge 终态复制 asset_tag（唯一键冲突=已挂载→幂等跳过或 ConflictError 取 v1 行为）；merge 两支：`merge_to_tag_id` 并入既有 / `new_tag_name+new_dir_id` 合入新建（建 TagDefine + change_log action='merge'，同名撞 ux 时 422 提示走并入既有——**Review Focus 3**））；review 路由（契约速查：items 过滤 dir/origin、filters {statuses, origins, dirs}、动作端点 URL 不动、ReviewItemOut 含 dir_name）。reviewer/review_comment/review_status 落行，无 ReviewRecord。

- [ ] **Step 1: 写失败测试** — test_review_service：approve→行 approved+复制挂载（tag_name 取 define 名）；reject；modify（候选名/描述更新+approved+挂载）；merge 并入既有（含**同名候选**——不建新 define、不撞唯一键）；merge 合入新建（define+change_log+挂载）；终态行再审→ConflictError；空 comment 合法。test_api_review：items 过滤组合（dir/origin/status/confidence）、filters 形状、单发/批量、409 文案。
- [ ] **Step 2: 跑测试确认失败** — `python -m pytest tests/test_review_service.py tests/test_api_review.py -v` → FAIL
- [ ] **Step 3: 实现** — service 按映射表重写；路由 ReviewItemOut 用 outerjoin(MediaAsset, TagDefine, TagCatalog, TagGenerationTask)（dir_name = define 目录名 or candidate_dir 名 or None；排序保持 confidence asc nulls-last, id asc）。
- [ ] **Step 4: 跑测试确认通过** — 同命令 → PASS
- [ ] **Step 5: Commit** — `git commit -m "feat(v2): 审核行即日志——四动作行操作 + merge 两支"`

### Task 9: metrics v2

**Files:**
- Modify: `src/tagplatform/metrics.py`、`src/tagplatform/api/routers/metrics.py`
- Test: `tests/test_metrics.py`（重写）

**Interfaces:**
- Consumes: Task 1/6/8 表。
- Produces: `compute_metrics`：pending_count=pre pending；review_stats={approved,rejected,merged}（前两者按 review_status 计数，merged 按 tag_change_log action='merge' 计数；分母=pre 总行数，merged 不入分母）；generated_pass_rate/evidence_coverage 公式同 v1、源换 pre；task_stats 源换 TagGenerationTask。`confidence_distribution` 源换 pre。`tag_dir_coverage`（端点更名 /api/v1/metrics/dir-coverage）：[{dir_name, total, approved}]，tag_id→define→dir；候选行看 candidate_dir_id；两者皆无→"未归置"桶。

- [ ] **Step 1: 写失败测试** — 造 pre 行（各 review_status）+change_log merge 行→三键计数与 ratio；空库→全 null/空列表；coverage 三种归属（define 目录/candidate_dir/未归置）+ total desc 排序；confidence 桶含零桶、None 排除。
- [ ] **Step 2: 跑测试确认失败** — `python -m pytest tests/test_metrics.py -v` → FAIL
- [ ] **Step 3: 实现** — 三函数改源；路由 response model 更名。
- [ ] **Step 4: 跑测试确认通过** — 同命令 → PASS
- [ ] **Step 5: Commit** — `git commit -m "feat(v2): metrics 换 review_status 终态分布 + 目录覆盖"`

### Task 10: reembed 与 ingest 脚本

**Files:**
- Modify: `src/tagplatform/reembed.py`、`scripts/ingest_movienet.py`（若 Task 3 未覆盖）
- Test: `tests/test_reembed.py`（重写）

**Interfaces:**
- Produces: `reembed_all -> ReembedSummary(dir_rows, tag_rows)`——目录文本 f"{dir_name}。{description}"、标签文本 f"{tag_name}。{description}"，均 passage 角色；context 通道删除。

- [ ] **Step 1: 写失败测试** — 双通道行数与 embedding 落库（MockEmbedder）；无 context 相关行为。
- [ ] **Step 2: 确认失败** — `python -m pytest tests/test_reembed.py -v` → FAIL
- [ ] **Step 3: 实现** → **Step 4: 通过** → **Step 5: Commit** — `git commit -m "feat(v2): reembed 拆目录/标签双通道"`

### Task 11: 后端全量绿 + 全新库冒烟

**Files:**
- Modify: 残留失败测试文件（e2e/装配类：`tests/test_e2e_vision_pipeline.py`、`tests/test_auth_api.py`、`tests/test_health.py`、`tests/conftest.py` 仅在必要时）

**Interfaces:**
- Consumes: Task 1–10 全部。
- Produces: 全量 `python -m pytest` 绿；全新库冒烟用例（create_app + TestClient + create_all：登录→导入层级标签→建资产导入内容→跑 closed 任务（MockLLM/MockEmbedder）→审核 approve→metrics 三端点有数）。

- [ ] **Step 1: 跑全量** — `python -m pytest` → 收集错误/失败清单（预期集中在 e2e 与跨域用例）
- [ ] **Step 2: 修残留** — e2e 链路按新表/新函数更新（不改产品代码，除非暴露真 bug——暴露则回对应域修）
- [ ] **Step 3: 写全新库冒烟** — `tests/test_smoke_fresh_db.py`：上述全链冒烟断言（挂载出现在 /assets/{id}/tags、metrics 非空）
- [ ] **Step 4: 全量绿** — `python -m pytest` → all passed，输出零 error/warning 新增
- [ ] **Step 5: Commit** — `git commit -m "feat(v2): 后端全量绿 + 全新库端到端冒烟"`

### Task 12: 前端 API client 与类型重约

**Files:**
- Modify: `frontend/src/api/assets.ts`、`review.ts`、`catalog.ts`、`imports.ts`、`metrics.ts`、`tasks.ts`（client/media/users/config/prompts 不动）
- Test: `frontend/src/api/assets.test.ts`、`review.test.ts`、`catalog.test.ts`、`imports.test.ts`、`tasks.test.ts`（对应更新；metrics 无 api 测试，由页面测试覆盖）

**Interfaces:**
- Consumes: Task 3–9 的 API v2 变更表。
- Produces: `fetchAssetContent(assetId) -> ContentContext`（GET /assets/{id}/content；删 fetchAssetContext）；`AssetDetailData{recognized}`；`AssetMount{id, tag_id, tag_name, origin, created_at}`；`TagNode{id, name, kind, dir_type?, description, parent_id, status, has_children, aliases}`；导入函数全部指向 /api/v1/import/tasks + task_id；`ReviewItem{dir_name?, origin}`、`ReviewFilters{statuses, origins, dirs}`、merge payload 两支类型；`GenerationTaskResult` 3 态 review_status + origin closed/open；`ReviewStats` 3 键；`fetchTagDirCoverage()`（dir-coverage）。ContentContext 内容形状不变（沿用现行 adapter 形状）。

- [ ] **Step 1: 写失败测试** — 各 api 测试按新端点/形状更新断言（路径、query、payload 键名）。
- [ ] **Step 2: 确认失败** — `npm test -- src/api` → FAIL
- [ ] **Step 3: 实现** — 类型与函数按契约速查重约；页面此任务不动（页面测试此时红属预期）。
- [ ] **Step 4: 通过** — `npm test -- src/api` → PASS
- [ ] **Step 5: Commit** — `git commit -m "feat(v2): 前端 API client 与类型按 v2 契约重约"`

### Task 13: 前端审核中心

**Files:**
- Modify: `frontend/src/pages/ReviewCenter.tsx`
- Test: `frontend/src/pages/ReviewCenter.test.tsx`（重写）

**Interfaces:**
- Consumes: Task 12 review API。
- Produces: 单「来源」列（origin closed→Tag 闭环 / open→Tag 开放；mode 列与 mode 过滤删除）；review_status 3 态 Tag；「目录」筛选 Select（filters.dirs）；修改弹窗两字段（候选名/描述）+ 备注（提交即通过）；合并弹窗两支（Radio：并入既有=搜索选择既有标签——`fetchTags(q, kind="tag")` 只搜标签 / 合入新建=选目录+新标签名+描述）；批量通过/拒绝保留；409 文案与刷新保留；EvidenceCard 不变。

- [ ] **Step 1: 写失败测试** — 默认过滤 status=pending；来源列渲染；目录筛选传参 dir=；修改弹窗 payload {candidate_name, candidate_description, comment} 后行变 approved；合并两支 payload 分别 {merge_to_tag_id} / {new_tag_name, new_dir_id}；批量；409。
- [ ] **Step 2: 确认失败** — `npm test -- src/pages/ReviewCenter.test.tsx` → FAIL
- [ ] **Step 3: 实现** → **Step 4: 通过** → **Step 5: Commit** — `git commit -m "feat(v2): 审核中心——来源列统一、目录筛选、merge 两支"`

### Task 14: 前端资产页

**Files:**
- Modify: `frontend/src/pages/AssetDetail.tsx`、`frontend/src/pages/AssetList.tsx`（轻改）
- Test: `frontend/src/pages/AssetDetail.test.tsx`（重写）、`AssetList.test.tsx`（微调）

**Interfaces:**
- Consumes: Task 12 assets API。
- Produces: AssetDetail 用 fetchAssetContent（失败/404→Alert"未完成内容识别"，播放器与挂载表仍渲染）；contexts 行删除；挂载表列 tag_name/来源(origin)/created_at（评分/证据/审核状态列与展开删除——证据查看走审核中心与任务详情）；SceneTimeline/VideoPlayer 不变（内容形状未变）；URL ?scene=&t= 跳转逻辑保留。AssetList：上传响应形状不变（微调断言即可）。

- [ ] **Step 1: 写失败测试**（重写 AssetDetail：content 加载、未识别降级、时间轴跳转、挂载表新列）→ **Step 2: 确认失败** → **Step 3: 实现** → **Step 4: 通过**（`npm test -- src/pages/AssetDetail.test.tsx src/pages/AssetList.test.tsx`）→ **Step 5: Commit** — `git commit -m "feat(v2): 资产详情读 asset content，挂载表只列已通过标签"`

### Task 15: 前端标签库与导入页

**Files:**
- Modify: `frontend/src/pages/TagCatalog.tsx`、`frontend/src/pages/TagImports.tsx`、`frontend/src/pages/Layout.tsx`（菜单文案）、`frontend/src/App.tsx`（路由不动，仅必要时）
- Test: `frontend/src/pages/TagCatalog.test.tsx`（重写）、`TagImports.test.tsx`（重写）、`App.test.tsx`（菜单断言）

**Interfaces:**
- Consumes: Task 12 catalog/imports API。
- Produces: TagCatalog 混合树（kind 徽标 目录/标签；TreeSelect 目录懒加载传 `kind="dir"` 只取目录）；详情抽屉（kind/path/aliases/status）；生命周期按钮仅对 kind=tag；导出按钮不变。TagImports：task_id 列 + 新路径 + 撤销/导出流程保留；菜单「标签导入」→「导入任务」（路由 /imports 保留）。

- [ ] **Step 1: 写失败测试**（混合树渲染/展开/搜索、详情、生命周期仅标签、导入页 task_id/状态/撤销）→ **Step 2: 确认失败** → **Step 3: 实现** → **Step 4: 通过**（`npm test -- src/pages/TagCatalog.test.tsx src/pages/TagImports.test.tsx src/App.test.tsx`）→ **Step 5: Commit** — `git commit -m "feat(v2): 标签库混合树 + 导入任务页"`

### Task 16: 前端质量/工作台/任务页 + 全量绿 + build

**Files:**
- Modify: `frontend/src/pages/Quality.tsx`（3 态统计卡 + dir-coverage 图）、`Dashboard.tsx`（字段源不变，微调）、`GenerationTasks.tsx`（结果面板 3 态 + origin）
- Test: `Quality.test.tsx`、`Dashboard.test.tsx`、`GenerationTasks.test.tsx`（更新）

**Interfaces:**
- Consumes: Task 12 metrics/tasks API。
- Produces: Quality 审核分布 3 卡（通过/驳回/合并）+「目录覆盖」BarChart（dir_name 维度，"通过 N" extra）；GenerationTasks 结果行 origin 闭环/开放 + 3 态状态。

- [ ] **Step 1: 写失败测试**（3 卡、目录覆盖图、null→暂无、任务结果面板）→ **Step 2: 确认失败** → **Step 3: 实现** → **Step 4: 全量绿** — `npm test`（24+ 文件全绿）+ `npm run build` → 成功
- [ ] **Step 5: Commit** — `git commit -m "feat(v2): 质量页终态分布 + 前端全量绿与构建"`

---

## 收尾（执行完成后）

finishing-a-development-branch → 合并 main → 部署：本地全量绿 → tar overlay → `remote_exec.py put` → 服务器 `alembic upgrade head`（0007 重建，业务数据已清零；**先做 DB 备份**）→ stop/start → 冒烟（login / tags 混合树 / import 层级样例 / content / closed 任务 / review / metrics）→ 用户重新导入 338 标签库与内容资产。
