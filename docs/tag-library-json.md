# 标签库（JSON 模拟）与动态标签生成 —— 已退役

> **状态（2026-11 合并 main 后）**：本文描述的 JSON 标签库已**退役**。
> 标签库现在只有一套真身：`tag_catalog`（目录）+ `tag_define`（标签），
> 生成/召回/审核/入库全部直接读写这两张表（`tagplatform/library.py`、
> `tagplatform/ingest.py`）；标签数据的入口是标签管理页的 Excel 导入
> （`POST /api/v1/tags/import`）。
> `data/taglibrary/*.json` 与 `scripts/build_tag_library.py` 仅作历史资料保留，
> 运行期不再读取。以下为原始设计记录，阅读时以"当时为什么这么做"为准。

本文对应 2026-11 这轮改造：动态标签生成**不读写** `tag_catalog` / `tag_define`，
改为读 `data/taglibrary/*.json`（由 `docs/movietags-template.xlsx` 生成），
审核通过的新词写到本地"入库预览"JSON，等正式表就绪后照单 INSERT。

## 1. 为什么用 JSON

- 正式表由外部（视频理解部门）提供，本期不能建、不能改、不能写；
- 目录/标签的语义（层级、可扩展性、挂载粒度）必须先落到生成逻辑里，
  否则"目录范围、新词归属、scene/character 挂载"都无处验证；
- JSON 与将来表结构一一对应，替换实现只改 `TagLibrary.load`。

## 2. 数据形状

`scripts/build_tag_library.py` 从模板 xlsx 生成两个文件：

| 文件 | 行 | 关键字段 |
|---|---|---|
| `data/taglibrary/dirs.json` | 37 | `dir_id` / `parent_id` / `level` / `dir_name` / `dir_type` / `applicable_scope` / `description` / `aliases` |
| `data/taglibrary/tags.json` | 514 | `tag_id` / `dir_id` / `tag_name` / `tag_type` / `description` / `aliases` / `status` |

- `dir_type`：`dir_static`（只能命中，不能新增） / `dir_dynamic`（允许扩充新标签）
- `applicable_scope`：`content`（影片级） / `scene`（场景级） / `character`（角色级）
  —— 直接决定 `pre_asset_tag.target_type`
- id 稳定可复现：`dir_<sha1(路径)[:8]>`、`tag_<sha1(dir_id + "/" + 名字)[:8]>`

当前模板统计：37 目录（4 个 L1 + 9 个 L2 + 24 个 L3；静态 21 / 可扩展 16）：
content 33、scene 2、character 2；标签 514（`val_fixed` 492 / `val_generalized` 22）。

## 3. 生成链路的变化

```
任务(policy: dir_ids / allow_new_tags / max_tags_per_dir)
  → 载入 JSON 库（TagLibrary）
  → library_index：范围内有效标签 + 向量（passage）
  → closed：按目录召回候选 → LLM 判定 → 每条候选按目录 scope 解析挂载点
  → open：多轮生成 → 对齐库内（同名/相似/新词）→ 新词 assign_dir 归属目录
  → persist_tags：
       命中库内 → approved（reviewer=system，不进人工队列）
       库外新词 → pending（必须带 dir_id + target_type/target_id）
```

- **重复**（与库内同名）：直接 `approved` + `reviewer_id="system"`，审核中心不显示；
- **相似**（BGE-M3 判定）：自动并入库内标签，模型原词写 `candidate_alias`，
  审核中心同样不显示（已终态），原名进别名预览；
- **新词**：`pending`，必须落在可扩展目录；审核通过后进入"入库预览"。

目录归属（`assign_dir` / `rank_dir_assignments`）：在范围内 `dir_dynamic`
目录中按描述相似度排序；最佳目录解析不出挂载点（例如 character 目录但证据里
没有唯一角色）时退到次优目录，避免整条丢弃。

## 4. 挂载粒度

`resolve_mount(scope, asset_id, content, evidence)`：

| scope | 挂载点 | 解析失败 |
|---|---|---|
| content | `asset_id` | 不会失败 |
| scene | 证据里第一个真实 `scene_id` | 返回 None → 丢弃该条 |
| character | 证据场景中**恰好一个** `character_id` | 0 或 ≥2 个 → 丢弃 |

## 5. 入库预览（模拟写表）

审核通过的新词/合并别名写到 `data/taglibrary/_pending/`：

- `tag_define_pending.json`：`tag_id` / `dir_id` / `dir_name` / `tag_name` /
  `tag_type` / `description` / `aliases` / `status` / `source`（资产、待审行 id、
  任务、审核人、时间）——按 `(dir_id, tag_name)` 幂等；
- `aliases_pending.json`：`tag_id` / `tag_name` / `dir_id` / `alias` / `source`
  ——按 `(tag_id, alias)` 幂等。

正式表就绪后，入库 = 读这两个文件逐行 INSERT，业务逻辑不用改。
接口：`GET /api/v1/ingest/preview`。

## 6. 表结构变更（迁移 0012）

`pre_asset_tag`：

- `tag_id` / `candidate_dir_id` **去掉指向 `tag_catalog` 的外键**（现在存的是
  JSON 库 id，不在旧表里，留着外键会插不进去）；
- 新增 `candidate_alias`（相似并入时模型原词，将来写 `tag_define.aliases`）；
- `asset_id` → `media_asset`、`task_id` → `tag_generation_task` 外键不变。

SQLite 不支持 DROP CONSTRAINT，迁移用 batch 重建表；`downgrade` 会补齐
外键并删列（先清掉库外 id，避免重建失败）。

## 7. 接口

| 接口 | 用途 |
|---|---|
| `GET /api/v1/library/stats` | 目录/标签/可扩展目录数与粒度分布 |
| `GET /api/v1/library/tree` | 目录树（含 `allows_new_tags`、直接标签数） |
| `GET /api/v1/library/tags?dir_id=&query=` | 标签列表（含子目录、别名搜索） |
| `GET /api/v1/ingest/preview` | 待写 `tag_define` 行 + 待写别名 |
| `GET /api/v1/review/filters` | `tag_types` / `dirs`（标签库来源） |
| `GET /api/v1/review/items?dir_id=` | 目录筛选（含子目录；库内行按标签目录算） |

生成任务创建支持 `policy.dir_ids` / `allow_new_tags` / `max_tags_per_dir`。

## 8. 已知边界

- 相似判定阈值 `library_similar_threshold=0.72` 与 BGE-M3 强相关，换模型要重标；
- 目录归属只看目录名+描述向量，词义漂移的候选可能归错目录——审核环节
  允许改 `candidate_dir_id`（modify），改完再入库；
- 入库预览是本地文件，多进程/多机部署会各写各的；正式表就绪后不存在这个问题。
