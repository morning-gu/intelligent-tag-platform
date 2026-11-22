# 前端页面合一设计（短设计）

日期：2026-11-22 · 状态：待用户批准
上游：2026-11-21-four-layer-refactor-design.md（后端四层已上线 f31787c）
执行分支：自 `refactor/four-layer` 堆叠新分支（PR #39 合入后自然重排）

## 意图

四层重构把"7 模块"落在了代码目录与后端，导航与页面仍是老的 10 项平铺。
本次把**页面级真合一**补上：一个业务模块 = 一个路由一页，子功能收成页内
tab；同时砍掉内容重叠的页面。用户动机：页面应该重构合一，而不是简单分组。

## 已拍板决策（2026-11-22 用户）

1. **工作台吸收质量评估**：两页同吃 `GET /metrics/summary`，工作台（73 行）
   是质量评估（108 行）的子集。终态工作台 = 全量指标看板（四卡 + 置信度
   分布图 + 目录覆盖图 + 审核状态卡）+ 快捷入口（进入审核 / 创建任务）。
   删 `/quality` 独立页与菜单项。审核中心不动、专注审核。
2. **资产详情保留独立子路由** `/assets/:id`：播放器 + 场景时间轴需要整页
   宽度；审核中心证据卡深链 `?scene={id}&t={ms}` 继续有效。不做抽屉化。

## 终态结构（10 菜单 → 7）

| 菜单 | 路由 | 容器内容 | 消化的旧页面 |
|---|---|---|---|
| 工作台 | `/` | 总览看板 + 快捷入口 | Dashboard + Quality |
| 标签管理 | `/tags` | Tabs：标签库 \| 导入批次（`?tab=` 同步） | TagCatalog + TagImports |
| 内容管理 | `/assets` | 列表+上传+目录导入（不变）+ 详情子路由 | 不动 |
| 内容打标 | `/tasks` | 生产任务（闭环/开放已合一，不变） | 不动 |
| 审核中心 | `/review` | 标签审核工作区（不变） | 不动 |
| 闭环评测 | `/eval-closed`（admin） | 评测流程（不变） | 不动 |
| 配置管理 | `/config`（admin） | Tabs：用户 \| 模型与 Prompt（`?tab=` 同步） | Users + ModelConfig |

菜单命名对齐用户 7 模块方案（原"内容资产"→内容管理、"生产任务"→内容打标；
审查时可改回）。开放评测（预留）无菜单入口，维持后端目录占位。

## 删除清单

- `/quality` 路由与菜单项（内容并入工作台）
- `/imports` 路由与菜单项（TagImports 组件原样收进标签管理 tab）
- `/users`、`/model-config` 路由与菜单项（组件收进配置管理 tab）
- 旧路由 `Navigate` 重定向保书签：`/imports→/tags?tab=imports`、
  `/quality→/`、`/users→/config?tab=users`、`/model-config→/config?tab=model`

## 范围与红线

- **API 端点契约零变化**——全部端点、请求、响应结构冻结；本改动只动
  路由/容器/菜单与工作台页面本身
- TagCatalog / TagImports / Users / ModelConfig / ReviewCenter /
  GenerationTasks / ClosedEval / AssetList / AssetDetail **组件代码不动**，
  只换挂载容器
- tab 状态同步到 URL query（`?tab=`），刷新与深链不丢状态
- 测试随迁：Dashboard/Quality/App/Layout 测试更新，tab 容器新增测试

## 方案备选（已否决）

- 菜单分组（antd children 子菜单、路由不动）：用户明确要求页面级真合一
- 质量看板并入审核中心双 tab、删工作台：被"工作台吸收质量评估"否决
- 资产详情抽屉化：被"保留独立子路由"否决
