# 数据清空与资产删除设计（标签管理 + 内容管理页）

日期：2026-11-24 · 状态：待批准
动因：**重跑实验**——同一套内容反复生成标签时，需要把上一轮结果（挂载、待审核、
生成任务历史）一键清干净。现状：内容侧**零删除接口**（只能进不能出）；标签侧只有
单标签 DELETE（被引用 409）；没有任何批量清空手段，重置只能手写 SQL。

## 1. 五操作矩阵（洋葱式包含）

| # | 操作 | pre_asset_tag | asset_tag | 生成任务+质量评估 | asset_build_task | media_asset | 标签库 | 排队任务 | 在途导入 |
|---|---|---|---|---|---|---|---|---|---|
| ① | 清空单内容标签 | 该资产 | 该资产 | 该资产+**queued 一并删** | — | — | 保留 | 删 | — |
| ② | 清空全部内容标签 | 全部 | 全部 | 全部+**queued 一并删** | — | — | 保留 | 删 | — |
| ③ | 清空标签库 | 全部 | 全部 | 全部+**queued 一并删** | — | — | **清空** | 删 | **409 拒绝** |
| ④ | 删除单内容资产 | 该资产 | 该资产 | 该资产+**queued 一并删** | 该资产+**queued 一并删** | 该资产+子场景 | 保留 | 删 | — |
| ⑤ | 删除全部内容资产 | 全部 | 全部 | 全部+**queued 一并删** | 全部+**queued 一并删** | 全部 | 保留 | 删 | — |

- ③ = ② + tag_define + tag_catalog（外键决定清库必须先清全部挂载）
- ⑤ = ② + asset_build_task + media_asset（含子场景行）
- ③+⑤ 连按 = 彻底重置：全库只剩 user_account、tag_import_task（导入批次）、
  tag_change_log（审计日志）、闭环/开放评测磁盘产物
- "该资产" = 顶层资产行 + 经 parent_id 递归的全部后代行（本期 target 只写
  content 级，但按后代闭包删，未来场景级标签数据不会被漏清）

## 2. 目标 / 非目标 / 反目标

**目标**：五个清空/删除操作，两个页面各就各位；每操作单事务、返回确切删除计数；
危险操作带输入"清空"的确认弹窗与行数预览。

**非目标**：软删除/回收站/定时清理/自动备份；清空 undo；按筛选条件批量删
（只有"单个"与"全部"两档）；删除视频源文件。

**反目标**（技术上失败即算失败）：
- 碰视频源文件（上传副本与目录导入路径一律不删——目录导入的是服务器上
  别人的源文件；资产行删了可重新导入）
- 碰闭环/开放评测数据（评测 run 是纯磁盘产物，eval 服务零 DB 写入——已验证
  `services/eval/dataset.py` 无 session 依赖；所有操作不触碰 run 目录）
- 碰 user_account / tag_import_task / tag_change_log
- 改任何既有 API 契约（本设计全部是新增端点）
- 任何 DB schema 变更（无迁移，alembic 版本不动）

## 3. 方案（已拍板：路线 A，按资源分端点）

**备选 B（单一 `POST /admin/reset {scope}` 重置端点）已否决**：五分支 god-endpoint，
确认弹窗按 scope 拼数字，非资源语义。五半径是稳定的资源形状而非会扩充的配置面，
级联复用下沉 services 层而非 API 层。

### 3.1 端点契约（全部 admin 专属）

content router 的既有现实：prefix `/api/v1`，资产路径挂 `/assets/*`（GET /assets、
GET /assets/{id}…），`/content/*` 下是 import/browse/scan。新端点对齐该分工：
**①④⑤ 落 `/assets` 族**（与既有资产 GET 同族），**② 落 `/content` 族**（无路由
冲突，语义即"content 域标签数据"）；tags 侧与既有 `/api/v1/tags` 前缀吻合。

| 端点 | 操作 | 说明 |
|---|---|---|
| `DELETE /api/v1/assets/{asset_id}/tags` | ① | 资产的 tags 子集合清空 |
| `DELETE /api/v1/content/tags` | ② | content 域全部标签数据 |
| `DELETE /api/v1/tags` | ③ | 标签库集合级 DELETE（与既有 `DELETE /tags/{tag_id}` 单删并存，无路由冲突） |
| `DELETE /api/v1/assets/{asset_id}` | ④ | 单资产（含子场景） |
| `DELETE /api/v1/assets` | ⑤ | 资产集合级 DELETE |
| `GET /api/v1/content/clear-preview?scope=asset-tags\|all-tags\|asset\|all-assets&asset_id=…` | 预览①②④⑤ | 同 counts 形状，只读 |
| `GET /api/v1/tags/clear-preview?scope=library` | 预览③ | 同 counts 形状，只读 |

**路由注册顺序**：字面量路径先于 `{param}` 注册——`/tags/clear-preview` 先于
`/tags/{node_id}`（照既有 `/tags/export` 先例）、`/assets/tags` 若与
`/assets/{asset_id}` 同法竞争时同理（② 落 `/content/tags` 已避开此冲突）。

**响应形状统一**：counts 键集 = `{pre_asset_tag, asset_tag, generation_tasks,
quality_evaluations, asset_build_task, media_assets, scenes, tag_define, tag_catalog}`，
每个操作填其子集（缺省 0 不省略——前端弹窗按同一形状渲染）。
**口径**：`media_assets` = 顶层行数（movie/series，不含场景），`scenes` = 子场景
行数，两键不重叠，相加即删除的 media_asset 总行数。`generation_tasks` 含被一并
删除的 queued 任务。DELETE 响应 `{ok: true, counts: {...}}`；preview 响应
`{scope, counts: {...}}`。

**错误**：`404` asset_id 不存在（①④）；`409` 有活跃任务（见 §3.3）；`401/403`
走既有 `get_current_user`/`require_admin` 依赖。

### 3.2 级联服务（四层归位：services 层，不 import fastapi）

`src/tagplatform/services/tags/reset.py`：
- `clear_tag_data(session, asset_ids: Collection[str] | None) -> Counts` —— 删
  pre_asset_tag → asset_tag → tag_quality_evaluation → tag_generation_task
  （FK 子先父后；asset_ids=None 即全库；任务按 asset_id ∈ 作用域）
- `clear_library(session) -> Counts` —— clear_tag_data(None) 之后删 tag_define
  → tag_catalog

`src/tagplatform/services/content/reset.py`：
- `delete_assets(session, asset_ids: Collection[str] | None) -> Counts` ——
  clear_tag_data(作用域) + asset_build_task + media_asset（后代行先删再删顶层，
  parent_id 闭包递归）；None = 全库
- 守卫查询：`active_task_counts(session, asset_ids | None) -> dict` —— 统计
  作用域内 queued/running 的 tag_generation_task 与 asset_build_task

orchestration 层（`orchestration/tags.py` / `orchestration/content.py`）只做
装配：守卫检查 → 调服务 → 响应模型转换。事务提交模式沿用各 router 既有做法。

### 3.3 守卫（409，先于任何删除）

只有**正在执行**的写方触发 409——排队中的不算（见下）：

- ①④：作用域（资产+后代）内存在 **running** 的生成或构建任务 → 409
- ②③⑤：库内任一 **running** 上述任务 → 409
- ③ 追加：存在 **processing** 的 tag_import_task（后台导入线程正在向
  tag_catalog/tag_define 插行，清库与其并发会 FK 崩）→ 409
- 409 detail 文案带活跃任务数（"有 N 个生成/构建/导入任务正在执行，请先等待其
  完成"——平台没有任务取消端点，不承诺"或停止"）

**queued 任务是删除半径的一部分，不是守卫对象**：清空语义天然取消排队工作
（重跑实验的本意就是丢掉上一轮）；删除 queued 行 = 干净取消（runner 按 DB 查询
取任务，行没了任务自然不跑）。这同时消灭了孤儿死锁：进程崩溃遗留的 queued
任务（启动既不复位也不重排，永远停在 queued）若被挡在守卫后面，②③⑤ 将永久
409——与"不用手写 SQL 重置"的目的直接冲突。实现期验证项：确认 runner 取任务
的方式对"行已删"健壮（预取场景下任务标记失败可接受，不 crash runner）。

- 评测 run **不加活跃守卫**：评测执行零 DB 依赖（磁盘自包含），清库不影响
  在途评测；上述 DB 守卫覆盖全部真实写方（生成/构建 runner + 导入线程）

### 3.4 不写 change_log（已拍板）

一次清 5000+ 行逐标签 delete 日志没有审计价值——清空是重置动作不是标签生命
周期动作；changelog 表本身不动（历史行指向已删标签是既有契约允许的）。

## 4. 前端视图

**内容管理页（AssetList）**：
- 新增"操作"列（固定右侧）：**仅 movie/series 顶层行**渲染两个 danger link
  按钮 [清空标签] [删除]；**场景行不渲染操作按钮**（标签数据全部挂 content 级，
  场景行上做 ① 是零计数 no-op；单抠一个场景也不在本次语义里）
- 工具栏（[上传视频][从目录导入] 同一 Space 右侧）追加两个 danger 按钮：
  [清空全部标签] [删除全部资产]
- **非 admin 不渲染任何危险按钮**（对齐既有"闭环评测/配置管理菜单仅 admin
  可见"模式；role 经 Layout Outlet context 取得）——reviewer 看到的是普通页面

**标签管理页（TagCatalog 工具栏，"标签导出"旁）**：
- 追加 danger 按钮 [清空标签库]（同样仅 admin 渲染）；其确认弹窗内附"先导出
  备份"链接（复用既有标签导出能力）

**共享确认弹窗 `frontend/src/components/ClearConfirmModal.tsx`**（两页复用）：
- 打开即拉 preview 计数，展示"将删除"清单（各表行数）
- 描述文案按 scope 固化（④⑤ 注明"视频源文件不删除，可随时重新导入"）
- Input 输入**"清空"**二字方可点亮红色确认按钮（五个操作统一，含删除资产）
- 确认 → DELETE → message.success 带 counts 摘要 → 刷新所在页数据 → 关弹窗
- 409 错误原样透出（弹窗内 Alert）；preview 加载失败独立错误态

## 5. 测试策略

**后端**（新增测试文件，全走 client fixture）：
- 每操作 happy path：fixture 造数据 → 调端点 → 断言各表行数归零/保留 +
  counts 精确匹配（含 ③ 后 import_batch/changelog 幸存、② 后库与资产幸存、
  ④ 后子场景级联删除、① 后仅该资产清空）
- 守卫：**running** 生成/构建任务存在 → 409（各 scope 各一例）；③ 另加
  processing 导入任务 → 409；**queued 任务被一并删除且不 409**（专测一例）
- 权限：未登录 401、reviewer 403（每端点至少一例）；前端非 admin 不渲染按钮
- ①④ asset_id 不存在 → 404
- FK 顺序正确性：全程无 IntegrityError（含清库后 FK 干净）
- preview 与 DELETE counts 形状一致性

**前端**（vitest + Testing Library）：
- 按钮渲染与位置；modal 输入错误字按钮禁用；确认调用正确端点；
  成功 toast + 刷新；409 透出；preview 失败态不阻塞弹窗骨架

**基线**：后端 959 + 前端 260 既有测试全绿（契约只增，零改动面）。

## 6. 已拍板决定

| 决定 | 来源 |
|---|---|
| 五操作矩阵、②独立按钮 | 用户（重跑实验场景） |
| 生成任务历史一律删，仅保留闭环/开放评测数据 | 用户 |
| 输入"清空"确认足够（无更重防护） | 用户 |
| 路线 A 按资源分端点 | 用户（AskUserQuestion 拍板） |
| ① 也删该资产生成任务（"重跑这部片"语义） | 我的判断 |
| 视频源文件一律不删 | 我的判断（目录导入=别人的源文件） |
| 不写逐标签 change_log；批次/日志表不动 | 我的判断 |
| 评测无活跃守卫（磁盘自包含已验证） | 我的判断 |
| preview 端点 ×2（分属 content/tags router） | 我的判断 |
| modal 统一输入"清空"（含删除资产场景） | 我的判断（沿用用户原话） |
| **queued 任务不设防、随清空一并删除**（守卫只挡 running/processing；消灭孤儿死锁） | 我的判断（builder check 发现死锁后重设计） |
| **①④⑤ 落既有 `/assets` 族、② 落 `/content` 族**（修正初稿的 `/content/assets` 前缀错误） | 我的判断 |
| ③ 守卫追加 processing 导入任务 | 我的判断（builder check 发现的 FK 竞态） |
| 场景行不渲染操作按钮 | 我的判断 |
| counts 口径：media_assets 与 scenes 不重叠 | 我的判断 |
| 非 admin 前端隐藏危险按钮（走既有 role 门控模式） | 我的判断 |

## 8. 已核事实（计划可直接引用，builder check 验证）

- §3.2 删除顺序与 models.py 的 FK 方向完全一致（pre_asset_tag.task_id、
  tag_quality_evaluation.task_id → tag_generation_task；tag_define.dir_id →
  tag_catalog）
- 守卫状态词 running/processing 与三张任务表 CHECK 枚举吻合
- ③ 后 services/tags/library 的指纹缓存自失效，无陈旧库问题
- 字面量先于 `{param}` 的路由注册顺序有 `/tags/export` 先例
- services 跨域 import 有先例（services/content/ingest.py import
  services/tags/library），reset 服务归位无分层障碍
- 事务模式（服务层内部 commit）有 run_governance 先例可循
- 平台无生成/构建/导入任务取消端点（仅评测 run 有 cancel）——409 文案不承诺"停止"

## 7. 留给实现者

- 删除的 ORM 具体写法（bulk delete vs iterate）与既有事务提交模式对齐
- ClearConfirmModal 的 props 形状与各页装配细节
- 操作列 fixed right 与表格既有列的布局协调
- 测试文件切分（按 router 或按操作）
- scopes 枚举的 pydantic 字面量类型定义位置（api/schemas.py vs 各 router）
