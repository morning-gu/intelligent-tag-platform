# 评测数据集根路径可配置（页面级）设计

日期：2026-11-20 ｜ 状态：待用户批准 ｜ 基点：feat/closed-loop-eval（PR #27 在途，堆叠分支）

## 意图

admin 在「闭环评测」页直接切换数据集根（如 `/cache/MovieTags` ↔ `/cache/MovieTagsV2`），免去 SSH 改 `.env` + 重启。方案已拍板：**服务端可变设置**（进程内 override，重启回落 `.env` 默认）。

## 现状与依据（代码事实）

- `settings.eval_dataset_root`（`.env` 的 `TAGPLT_EVAL_DATASET_ROOT`）是唯一根来源
- `ClosedEvalService.start()` 起跑时解析根（`closed_eval.py:481`）；但 **worker 懒加载 content.json 时读运行时 settings（`closed_eval.py:787`）**——运行中改 settings 会污染在跑 run，快照机制必须显式做
- `get_closed_eval` 返回 `app.state.closed_eval` **app 级单例**（`api/__init__.py:92`）——POST 变更同一实例，跨请求生效
- meta.json 的 `dataset.gt_path` 已含根路径（run 可追溯）

## 设计

**后端（2 端点 + 快照）**
- `GET /api/v1/eval/closed/dataset-root` → `{"root": <生效值>, "is_default": bool}`（生效值 = override ?? settings 默认）
- `POST /api/v1/eval/closed/dataset-root` body `{"root": str}` → 校验序：非空字符串 → 目录存在（`is_dir`）→ `scan_datasets(root, set())` ≥1 个有效数据集；任一不过 → 400 带原因；通过 → 写入 `ClosedEvalService._dataset_root_override`，返回 `{"root": ...}`
- 服务加 `effective_root` 属性；`start()` 的 scan/select 改用 effective_root，并把**快照根传入 worker**（闭包/参数），`_run_movie` 的 `load_movie_content` 用快照而非 `self.settings.eval_dataset_root`——运行中切根不影响在跑评测（红线）
- 均为 admin-only（沿用 eval 路由 `require_admin`）

**前端（ClosedEval 页）**
- 顶部一行：`数据集根路径` Input + `应用` 按钮；挂载时 GET 回填当前生效值；应用成功 → 重新拉取数据集列表 + message.success；400 → message.error 显示服务端原因
- localStorage 记上次输入值仅作 Input 预填（真相以 GET 返回为准）

**部署**：单 tar（后端 2 文件 + 前端 dist 重建）；**时机等当前评测 run 结束**（重启杀 run）。

## 非目标

- 跨重启持久化（重启回落 `.env`，页面 2 秒重设）
- V2 数据集内容结构适配（若 `content.json` 键名有变，另行独立处理）
- 切换历史审计

## 验收

- 页面应用 `/cache/MovieTagsV2`（存在且有效时）→ 数据集列表变为 V2 的；起跑的 run 其 meta `gt_path` 指向 V2
- 应用不存在路径 / 无有效数据集目录 → 400 + 页面报错，原生效值不变
- 运行中切根：在跑 run 的影片继续按起跑时根加载（测试钉死）
