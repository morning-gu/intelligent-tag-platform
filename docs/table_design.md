# 智能标签系统数据库表结构设计 v2（SQLite 落地 / PostgreSQL 迁移目标）

> 共 11 张表。**概念模型与语义规则为引擎中立定稿；当前按 SQLite 实现，后续迁移 PostgreSQL。**
> v1（线上 @ 1f28eb9）→ v2 的动机：现结构冗余、定义不清——一个资产劈两表、目录/标签混一张表、审核状态两处、结果行身兼两职、挂载粒度隐式。

---

## 1. 决策记录

| 决策点 | 结论 | 归属 |
|---|---|---|
| 存储引擎 | 概念模型照本文档定稿，SQLite 先落地，PostgreSQL 后续迁移；DDL 遵守引擎中立规矩（§8） | 拍板 |
| 挂载粒度 | `target_type` 三粒度（content/scene/character）**结构预留**，流水线与审核本期只跑 content 级 | 拍板 |
| 标签取值 | `val_generalized`（泛化值）**结构预留**，本期只跑 `val_fixed` | 拍板 |
| 重跑语义 | 幂等唯一索引按 asset×target×tag（候选名）；旧行 approved/rejected 跳过（终审），pending 删旧插新（§6.1） | 拍板 |
| 存档口径 | pre 表 = asset×tag 去重后的生成与审核记录；**全量原始档案由 mtg 审计文件兼任**（data/audit/mtg） | 拍板 |
| 实施范围 | **一次性全平台切换**：表 + ORM + 服务 + API + 前端 + 测试同步改绿，服务器一次 `alembic upgrade head` 后平台即恢复可用 | 拍板 |
| API 兼容口径 | **随 v2 重设计**：语义未变的 URL 保持稳定，概念变了的改（前端同步改）；无外部 API 消费者 | 拍板 |
| `review_record` 删除 | 行即日志：新模型下每行至多一审且终态不被覆盖，reviewer/comment/status 落行即完整审核事实（§6.2） | 裁量 |
| `asset_tag.tag_name` 刷新 | tag_define 改名/合并时同步刷新全部挂载行，不留漂移；tag_change_log 留证 | 裁量 |
| `candidate_dir_id` 可空 | 文档原定 open 模式生成时必填；实际生成端（mtg-v1 自拟分类）不知道目录体系，改为**合入标签库时赋值** | 裁量 |
| review_status 枚举 | 简化为 pending/approved/rejected 三值；v1 的 modify/merge 是审核动作而非行状态，映射见 §6.2 | 裁量 |
| 迁移策略 | 0007+ 重建系列（§9）；服务器业务数据已清零（2026-11-05），仅保留账号与模板种子 | 裁量 |
| 识别状态表达 | media 合并后不设 recognition_status 列，`content_json IS NULL` 即未完成识别 | 裁量 |
| 时间戳 ORM 类型 | DateTime（datetime 对象）沿用 v1，DDL 列 DATETIME；PG 目标 TIMESTAMPTZ | 裁量 |
| merge 两支语义 | v1 merge 能力保留：并入既有（pre 行赋 tag_id，不建新标签）+ 合入新建（建 tag_define）；`open_match_threshold` 语义不变（mtg 归一命中阈值） | 裁量 |
| 沿用三表不动 | user_account / prompt_template / asset_build_task 本期物理不动、ORM 保持 v1 形状；DDL 所示 CHECK/索引为 PG 目标形态 | 裁量 |
| 审核指标口径 | 动作分布统计改为按 review_status 终态分布；merge 次数从 tag_change_log 补 | 裁量 |

---

## 2. 概念模型

### 2.1 v1 → v2 变更对照

| 变更 | v1（线上） | v2（本文档） |
|---|---|---|
| 资产劈两表 | `media_asset` + `media_context`（+ data/assets JSON 文件三份） | 合并为 `media_asset` 一张，`content_json` 落列 |
| 目录/标签混表 | `tag_catalog` 单表混目录节点与叶子标签，`tag_type` 为自由文本分类，与 `asset_tag.candidate_type` 同名不同义 | 拆 `tag_catalog`（目录，dir_static/dir_dynamic）+ `tag_define`（标签，val_fixed/val_generalized） |
| 审核状态两处 | `asset_tag.review_status` + `review_record` 动作表 | 折进 `pre_asset_tag` 行（reviewer/comment 内嵌），`review_record` 删除 |
| 结果行两职 | `asset_tag` 既是生成产出又是正式挂载，靠 review_status 过滤 | `pre_asset_tag`（生成+审核）与 `asset_tag`（干净正式挂载）拆开 |
| 粒度隐式 | 表上只有 asset_id，"挂影片级、场景做证据"是代码约定 | `target_type`/`target_id` 显式三粒度（本期只启用 content） |
| 导入批次 | `import_batch` | `tag_import_task`（改语义名，结构同） |
| 生成任务外键 | `generation_task.context_id` → media_context | `tag_generation_task.asset_id` → media_asset（随合并） |

表数不变（11）：−`media_context` −`review_record` +`tag_define` +`pre_asset_tag`。

### 2.2 核心关系链

```
tag_catalog ──1:N──> tag_define ──1:N──> pre_asset_tag ──审核通过──> asset_tag
  (目录树)             (标签定义)          (生成+审核，去重)          (正式挂载)

media_asset (自引用 parent_id; content_json 含 scenes/characters)
  ├── tag_generation_task (asset_id)
  ├── pre_asset_tag       (asset_id + target)
  ├── asset_tag           (asset_id + target)
  └── asset_build_task    (asset_id)

tag_import_task  → 导入写入 tag_catalog / tag_define
tag_change_log   → 审计 tag_define 全生命周期（无外键，可指向已废弃/合并标签）

独立表: prompt_template (mode+version 版本化) / user_account (admin/reviewer)
```

**多粒度挂载（本期仅 content）：**

| target_type | target_id 取值 | 状态 |
|---|---|---|
| `content` | `media_asset.asset_id` | **本期启用** |
| `scene` | `content_json.content.scenes[].scene_id` | 结构预留 |
| `character` | `content_json.content.characters[].character_id` | 结构预留 |

`tag_catalog.applicable_scope` 声明目录适用范围，挂载时 target_type 与之对应。

---

## 3. 表定义（SQLite 落地 DDL）

> 时间戳在 ORM 层建模为 **DateTime（datetime 对象，应用层赋值）**，不设数据库默认（沿用 v1 代码风格）；SQLite 落 DATETIME 列，PG 目标 TIMESTAMPTZ。
> 业务实体主键为应用层生成 ID（`asset_`/`dir_`/`tag_`/`task_` 前缀 + uuid 片段）；结果/日志行用 INTEGER 自增。
> 向量列为 BLOB（float32 小端，维度由应用配置固定——multilingual-e5-base 为 768）。

### 3.1 media_asset — 媒体资产

v1 `media_asset` + `media_context` 合并。上传时填物理元数据，识别完成后填 `content_json`。

```sql
CREATE TABLE media_asset (
    asset_id      TEXT PRIMARY KEY,
    asset_type    TEXT NOT NULL CHECK (asset_type IN ('movie', 'series', 'scene')),
    parent_id     TEXT REFERENCES media_asset(asset_id),
    title         TEXT NOT NULL,
    duration_ms   INTEGER,
    video_uri     TEXT,
    content_json  TEXT,      -- JSON 串（PG 目标 JSONB）；NULL = 未完成内容识别
    source        TEXT,      -- 识别结果来源（外部项目 / 上传构建）
    created_at    DATETIME
);

CREATE INDEX idx_media_asset_parent ON media_asset(parent_id);
CREATE INDEX idx_media_asset_type   ON media_asset(asset_type);
```

**语义约定：**

- 识别状态不设列：`content_json IS NULL` = 未识别；重新识别直接**覆盖** content_json（历史版本由 mtg 审计文件留档）。
- 识别产出的模型/协议信息写进 `content_json` 元数据（`schema_version` 等，见 §4），不单设 model_version 列（生成任务侧的模型/提示词版本记录在 `tag_generation_task`）。
- v1 `media_context.embedding`（内容向量缓存列）不再保留：内容向量在生成任务启动时实时计算（与 §7 检索策略一致）；性能需要时再议缓存。

### 3.2 tag_catalog — 标签目录（树）

目录树，声明目录类型与适用范围。目录本身不是标签。

```sql
CREATE TABLE tag_catalog (
    dir_id           TEXT PRIMARY KEY,
    dir_name         TEXT NOT NULL,
    dir_type         TEXT NOT NULL CHECK (dir_type IN ('dir_static', 'dir_dynamic')),
    description      TEXT NOT NULL,
    parent_id        TEXT REFERENCES tag_catalog(dir_id),
    aliases          TEXT,      -- JSON 数组串
    applicable_scope TEXT DEFAULT 'content'
        CHECK (applicable_scope IN ('content', 'scene', 'character')),
    embedding        BLOB,      -- 目录描述向量（open 候选归置/目录检索用）
    created_at       DATETIME,
    updated_at       DATETIME,
    CONSTRAINT ux_tag_catalog_parent_name UNIQUE (parent_id, dir_name)
);

CREATE INDEX idx_tag_catalog_type  ON tag_catalog(dir_type);
CREATE INDEX idx_tag_catalog_scope ON tag_catalog(applicable_scope);
```

**dir_type 值语义：**

| 值 | 含义 | 示例 |
|---|---|---|
| `dir_static` | 静态目录 — 子节点不可由开放生成动态扩充 | "类型"（喜剧、悬疑等固定枚举的父目录） |
| `dir_dynamic` | 动态目录 — 子节点可由开放生成审核入库后动态扩充 | "整体情绪基调"（可扩展"浪漫甜蜜"等） |

**已知语义（沿 v1 0006 口径）：** SQLite/PG 的 UNIQUE 对 NULL 互不区分——根目录（parent_id NULL）同名在 SQL 层不拦，由服务层校验，与 v1 行为一致。

### 3.3 tag_define — 标签定义

具体标签，隶属于目录。closed 模式召回的 passage 向量持久化在此列。

```sql
CREATE TABLE tag_define (
    tag_id      TEXT PRIMARY KEY,
    tag_name    TEXT NOT NULL,
    tag_type    TEXT NOT NULL CHECK (tag_type IN ('val_fixed', 'val_generalized')),
    description TEXT NOT NULL,
    dir_id      TEXT NOT NULL REFERENCES tag_catalog(dir_id),
    aliases     TEXT,          -- JSON 数组串
    status      TEXT DEFAULT 'active' CHECK (status IN ('active', 'deprecated')),
    embedding   BLOB,          -- 标签语义向量（closed 召回 passage）
    created_at  DATETIME,
    updated_at  DATETIME,
    CONSTRAINT ux_tag_define_dir_name UNIQUE (dir_id, tag_name)
);

CREATE INDEX idx_tag_define_status ON tag_define(status);
CREATE INDEX idx_tag_define_dir    ON tag_define(dir_id);
```

**tag_type 值语义：**

| 值 | 含义 | 示例 | 本期 |
|---|---|---|---|
| `val_fixed` | 固定值标签 — 值由定义给出 | "喜剧"、"悬疑" | **启用** |
| `val_generalized` | 泛化值标签 — tag_id 指向定义，实际值存结果行 | tag_id→"执行导演"，tag_name="张三" | 结构预留 |

**与 tag_catalog 的关系：** 目录（`dir_type`）决定子标签能否动态扩充；标签（`tag_type`）决定值是固定还是泛化。标签适用范围继承所属目录的 `applicable_scope`，不在本表重复声明。

### 3.4 tag_generation_task — 标签生成任务

```sql
CREATE TABLE tag_generation_task (
    task_id        TEXT PRIMARY KEY,
    asset_id       TEXT NOT NULL REFERENCES media_asset(asset_id),
    mode           TEXT NOT NULL CHECK (mode IN ('closed', 'open')),
    model_version  TEXT,
    prompt_version TEXT,
    policy_json    TEXT,
    status         TEXT NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
    error          TEXT,
    created_at     DATETIME,
    finished_at    DATETIME
);

CREATE INDEX idx_gen_task_asset  ON tag_generation_task(asset_id);
CREATE INDEX idx_gen_task_status ON tag_generation_task(status);
```

外键从 v1 的 `context_id`（→media_context）改为 `asset_id`（随 media 合并）。closed 模式仍消费 prompt_template；open 模式由 mtg-v1 内置流水线接管（不走模板）。

### 3.5 pre_asset_tag — 待审核标签（生成结果 + 审核队列）

生成任务的产出落地此表；审核通过后复制到 `asset_tag`。本表是 **asset×tag 去重**后的生成与审核记录（重跑语义见 §6.1），全量原始轮次档案由 mtg 审计文件兼任。

```sql
CREATE TABLE pre_asset_tag (
    id                    INTEGER PRIMARY KEY,
    asset_id              TEXT NOT NULL REFERENCES media_asset(asset_id),
    target_type           TEXT NOT NULL DEFAULT 'content'
        CHECK (target_type IN ('content', 'scene', 'character')),
    target_id             TEXT NOT NULL,      -- content 级 = asset_id 本身
    tag_id                TEXT REFERENCES tag_define(tag_id),
    candidate_name        TEXT,
    candidate_dir_id      TEXT REFERENCES tag_catalog(dir_id),
    candidate_description TEXT,
    origin                TEXT NOT NULL CHECK (origin IN ('closed', 'open')),
    relevance_score       REAL,
    confidence            REAL,
    evidence_json         TEXT,
    task_id               TEXT REFERENCES tag_generation_task(task_id),
    review_status         TEXT NOT NULL DEFAULT 'pending'
        CHECK (review_status IN ('pending', 'approved', 'rejected')),
    reviewer_id           TEXT,
    review_comment        TEXT,
    created_at            DATETIME,
    updated_at            DATETIME
);

-- 幂等：同一资产同一实体不重复挂同一标签定义
CREATE UNIQUE INDEX ux_pre_tag_catalog ON pre_asset_tag (asset_id, target_type, target_id, tag_id)
    WHERE tag_id IS NOT NULL;

-- 幂等：同一资产同一实体不重复收录同一候选名
CREATE UNIQUE INDEX ux_pre_tag_candidate ON pre_asset_tag (asset_id, target_type, target_id, candidate_name)
    WHERE tag_id IS NULL AND candidate_name IS NOT NULL;

CREATE INDEX idx_pre_tag_asset  ON pre_asset_tag(asset_id);
CREATE INDEX idx_pre_tag_task   ON pre_asset_tag(task_id);
CREATE INDEX idx_pre_tag_review ON pre_asset_tag(review_status);
```

**字段说明（closed / open 两模式）：**

| 字段 | closed 模式 | open 模式 |
|---|---|---|
| `tag_id` | 必填（来自 `tag_define`） | 初始为空，审核合入标签库后赋值 |
| `candidate_name` | 空 | 必填 |
| `candidate_dir_id` | 空 | 生成时可空（mtg-v1 自拟分类不知目录体系），**合入时赋值** |
| `candidate_description` | 空 | 仅 generated 标签填写 |
| `origin` | `closed` | `open`（不可变，记录产出来源） |

v1 的 `candidate_type`（LLM 自拟分类）不再入表——生成端分类留在 mtg 审计文件，入库后的规范归属以目录为准。

### 3.6 asset_tag — 正式标签挂载

审核通过的标签复制到此表，只保留挂载关系与展示值，不含评分/证据（留在 pre 表）。

```sql
CREATE TABLE asset_tag (
    id           INTEGER PRIMARY KEY,
    asset_id     TEXT NOT NULL REFERENCES media_asset(asset_id),
    target_type  TEXT NOT NULL DEFAULT 'content'
        CHECK (target_type IN ('content', 'scene', 'character')),
    target_id    TEXT NOT NULL,
    tag_id       TEXT NOT NULL REFERENCES tag_define(tag_id),
    tag_name     TEXT NOT NULL,
    created_at   DATETIME,
    updated_at   DATETIME,
    CONSTRAINT ux_asset_tag UNIQUE (asset_id, target_type, target_id, tag_id, tag_name)
);

CREATE INDEX idx_asset_tag_asset ON asset_tag(asset_id);
CREATE INDEX idx_asset_tag_tag   ON asset_tag(tag_id);
```

**tag_name 取值规则：**

- `val_fixed` → 拷贝 `tag_define.tag_name`（如"喜剧"）；定义改名/合并时**同步刷新**本表（§6.3）。
- `val_generalized`（预留）→ 存实际值（tag_id→"执行导演"，tag_name="张三"）。唯一键含 tag_name 正是为泛化值预留：同一 tag_id 可在同一资产挂多个实际值（多位导演）。

### 3.7 tag_import_task — 标签导入任务

```sql
CREATE TABLE tag_import_task (
    task_id        TEXT PRIMARY KEY,
    file_name      TEXT NOT NULL,
    status         TEXT NOT NULL CHECK (status IN ('processing', 'completed', 'failed', 'revoked')),
    result_summary TEXT,
    created_at     DATETIME
);

CREATE INDEX idx_import_task_status ON tag_import_task(status);
```

v1 `import_batch` 改名，结构不变（含 revoke/导出语义）。

### 3.8 tag_change_log — 标签变更日志

审计 `tag_define` 的全生命周期变更。`tag_id` 不加外键（需指向已废弃/合并的标签）。

```sql
CREATE TABLE tag_change_log (
    log_id      INTEGER PRIMARY KEY,
    tag_id      TEXT NOT NULL,
    action      TEXT NOT NULL CHECK (action IN ('create', 'update', 'merge', 'deprecate', 'import')),
    before_json TEXT,
    after_json  TEXT,
    operator    TEXT,
    created_at  DATETIME
);

CREATE INDEX idx_change_log_tag ON tag_change_log(tag_id);
```

### 3.9 prompt_template — Prompt 模板

按 `mode + version` 版本化；同 mode 恰一个 `is_active = 1`。

```sql
CREATE TABLE prompt_template (
    id            INTEGER PRIMARY KEY,
    mode          TEXT NOT NULL CHECK (mode IN ('closed', 'open')),
    version       INTEGER NOT NULL,
    template_text TEXT NOT NULL,
    is_active     INTEGER NOT NULL DEFAULT 0 CHECK (is_active IN (0, 1)),
    created_at    DATETIME,
    CONSTRAINT ux_prompt_mode_version UNIQUE (mode, version)
);

CREATE INDEX idx_prompt_active ON prompt_template(mode, is_active);
```

沿用 v1 现有 3 行种子（closed v1 激活、open v1、open v2 过时标记——open 已由 mtg-v1 接管）。**本期表物理不动、ORM 保持 v1 形状**；上表 DDL（含 v1 没有的 CHECK）为 PG 迁移目标形态，届时统一对齐。

### 3.10 user_account — 用户账号

```sql
CREATE TABLE user_account (
    user_id       TEXT PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('admin', 'reviewer')),
    created_at    DATETIME
);
```

沿用 v1 表与现有数据，**本期表物理不动、ORM 保持 v1 形状**；上表 DDL（含 v1 没有的 CHECK）为 PG 迁移目标形态。

### 3.11 asset_build_task — 资产构建任务

上传视频后自动生成资产的后台任务，前端轮询进度。

```sql
CREATE TABLE asset_build_task (
    task_id      TEXT PRIMARY KEY,
    asset_id     TEXT NOT NULL REFERENCES media_asset(asset_id),
    movie_id     TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
    total_scenes INTEGER NOT NULL DEFAULT 0,
    done_scenes  INTEGER NOT NULL DEFAULT 0,
    error        TEXT,
    created_at   DATETIME,
    finished_at  DATETIME
);

CREATE INDEX idx_build_task_asset  ON asset_build_task(asset_id);
CREATE INDEX idx_build_task_status ON asset_build_task(status);
```

沿用 v1（PR #2 引入），**本期表物理不动、结构不变**；上表 DDL 所示 CHECK 与 status 索引为 PG 迁移目标形态。

---

## 4. content_json 结构约定

> **本期契约不变**：content_json 沿用现行适配器产出形状（`content.summary`、`scenes[].time_range` 等，契约定义见 `src/tagplatform/schemas/content.py`）；下述结构是外部视频理解适配器的**目标契约**（阶段 1 Task 12，样例 JSON 待定），本期不实现。

角色在内容层统一定义，场景通过 `character_ids` 引用；资产物理元数据由表列管理，不在 JSON 中重复。

```json
{
  "schema_version": "1.0",
  "content": {
    "description": "刑警张三调查连环失踪案，发现线索指向十年前仓库火灾，唯一幸存者李四隐瞒真相，两人从信任走向对立。",
    "characters": [
      {
        "character_id": "character_001",
        "name": "张三",
        "aliases": ["张警官"],
        "description": "刑警队队长，十年前参与火灾调查，心存疑虑。"
      }
    ],
    "scenes": [
      {
        "scene_id": "scene_001",
        "start_ms": 192000,
        "end_ms": 220000,
        "description": "张三深夜到李四办公室质问火灾证据，双方激烈争论，李四情绪失控拍桌警告。",
        "location": "李四科技公司办公室",
        "character_ids": ["character_001", "character_002"],
        "emotion": [{ "name": "紧张压迫", "code": "TENSE_OPPRESSIVE" }],
        "events": [
          {
            "event_id": "event_001",
            "description": "张三将调查报告放在桌面质问李四。",
            "participant_ids": [
              { "character_id": "character_001", "emotion": [{"name": "愤怒", "code": "ANGER"}] },
              { "character_id": "character_002", "emotion": [{"name": "恐惧", "code": "FEAR"}] }
            ]
          }
        ],
        "dialogues": [
          {
            "dialogue_id": "dialogue_001",
            "speaker_id": "character_001",
            "text": "十年前仓库里的事情，你真的以为没人知道吗？",
            "start_ms": 200000,
            "end_ms": 204000
          }
        ]
      }
    ]
  }
}
```

**可用于 embedding 计算的文本字段（本期仅内容级启用）：**

| 粒度 | 字段路径 | 用途 | 状态 |
|---|---|---|---|
| 内容级 | `content.summary` + 场景描述（现行 `embed_content_texts` 提取规则） | 整片语义，closed 模式 query 向量 | **本期启用** |
| 角色级 | `content.characters[].name` + `aliases[]` + `description` | 角色语义，角色标签召回 | 预留 |
| 场景级 | `content.scenes[].description` + `location` | 场景语义，场景标签召回 | 预留 |
| 证据 | `content.scenes[].dialogues[].text` + `start_ms` + `end_ms` | 标签证据回溯 | 启用（证据展示） |
| 氛围 | `content.scenes[].emotion[]` + `events[].participant_ids[].emotion` | 情绪/氛围标签生成素材 | 预留 |

---

## 5. 向量检索策略

| 数据 | 向量存储 | 计算时机 | e5 角色 |
|---|---|---|---|
| `tag_define` 标签 | `embedding` 列持久化（BLOB） | 标签入库/更新时 | passage |
| `tag_catalog` 目录 | `embedding` 列持久化（BLOB） | 目录入库/更新时 | passage |
| `media_asset` 内容 | 不存储 | 生成任务启动时实时计算 | query |

closed 模式召回流程（沿用 v1 已验证的 e5 前缀契约）：

1. 按 `embed_content_texts` 现行规则提取摘要与场景文本（本期仅内容级）
2. 用配置的 embedding 模型编码为 L2 归一化向量（**query: 前缀**）
3. 与 `tag_define.embedding`（**passage: 前缀**）做余弦相似度 top-k 检索
4. 召回候选注入 LLM 上下文做相关性判断

SQLite 实现下余弦相似度在应用层计算（BLOB 解包 + numpy）；PG 迁移后换 pgvector 原生算子，检索逻辑收敛在向量存储接缝（VectorStore）后面。

---

## 6. 语义规则

### 6.1 重跑与幂等

新任务对同一资产生成出与旧行相同的标签（tag_id 或候选名）时：

| 旧行 review_status | 处理 |
|---|---|
| `approved` | **跳过**（已挂载到 asset_tag，判定终审） |
| `rejected` | **跳过**（驳回即终审；翻案走人工改库，不靠重跑） |
| `pending` | **删旧插新**（最新一轮的证据/分数进审核队列） |
| 无旧行 | 直接插入 |

实现：任务写结果前先删除该资产**同 origin** 的全部 `pending` 行（沿用 v1 重跑语义——closed 重跑不清 open 的待审行），再逐行 `INSERT OR IGNORE`。open 候选（candidate_name）同一套规则。唯一索引即幂等边界——跨任务去重（非 per-task 存档），pre 表因此是 asset×tag 去重视图，每资产每标签至多一行。

跨轮次生成质量分析（含阈值重校准）不查本表，查 mtg 审计文件（每轮原始输出全量落盘）。

### 6.2 审核动作 → 行操作映射（行即日志）

`review_record` 表删除。理由：新模型下每行至多一审——approved/rejected 是终态且行不被覆盖（§6.1），reviewer_id / review_comment / review_status / updated_at 落在行上即完整审核事实。库级变更仍由 `tag_change_log` 审计。

v1 的审核动作映射为行操作（merge 保留 v1 的两支语义——并入既有 / 合入新建）：

| 审核动作 | 行操作 |
|---|---|
| approve | pre 行标 `approved` + reviewer/comment → 复制到 `asset_tag` |
| reject | pre 行标 `rejected` + reviewer/comment |
| modify（改候选后通过） | 更新 pre 行字段（candidate_name/description/dir）→ 走 approve 流程，comment 记录修改内容 |
| merge·并入既有（v1 merge） | 审核员认定候选 = 库内已有标签：pre 行赋 `tag_id`（candidate_name 原文保留可溯）→ 走 approve 流程，**不新建标签** |
| merge·合入新建 | 库内确无对应：建 `tag_define` 行（change_log 记 `merge`）→ pre 行赋 `tag_id`/`candidate_dir_id` → 走 approve 流程 |

open 生成时的归一命中沿用 v1：mtg `_catalog_hits` 按 `open_match_threshold` 与库内标签向量匹配，命中行生成时即带 tag_id（origin 仍为 open）；审核端合并对话框由审核员自行搜索目标标签。

**指标口径：** v1 按 ReviewRecord.action 的审核动作分布统计改为按 `review_status` 终态分布（modify/merge 的终态均为 approved）；merge 次数可从 `tag_change_log.action = 'merge'` 补出。

### 6.3 tag_name 冗余拷贝刷新

`asset_tag.tag_name` 是 `tag_define.tag_name` 的冗余拷贝（val_fixed）。定义侧 rename / merge 时同步刷新全部挂载行，`tag_change_log` 的 before/after 留证，不留两处漂移。

### 6.4 candidate_dir_id 赋值时序

open 生成端（mtg-v1）产出的是自拟分类，不知道平台目录体系：生成时 `candidate_dir_id` 可空；审核 merge 入库时由合并流程赋值（此时起必填）。目录归属的唯一规范来源是 `tag_catalog`，生成端自拟分类不入库。

---

## 7. 引擎中立规矩（SQLite 当前 / PostgreSQL 目标）

**规矩：**

1. DDL 经 SQLAlchemy ORM + alembic 表达，不手写引擎专有方言；本文档 DDL 为 SQLite 落地参照。
2. 不使用 SQLite 专有特性；JSON 解析/构造一律在应用层（不依赖 JSON1 SQL 函数）。
3. CHECK / UNIQUE / 外键 / 部分唯一索引只用两引擎共同支持的语法（`CREATE UNIQUE INDEX ... WHERE ...` 双方均支持）。
4. SQLite 连接须开 `PRAGMA foreign_keys = ON`（连接级开关，代码层保证）。
5. 向量存取收敛在 VectorStore 接缝后面，PG 迁移只换实现。

**类型映射：**

| 概念 | SQLite（当前） | PostgreSQL（目标） |
|---|---|---|
| JSON 字段 | TEXT（JSON 串，应用层处理） | JSONB |
| 向量 | BLOB（float32 小端，维度应用配置） | `vector(N)`（N = 模型维度） |
| 时间戳 | DateTime ORM（DATETIME 列，应用层赋值） | TIMESTAMPTZ |
| 结果/日志主键 | INTEGER PRIMARY KEY | BIGSERIAL |
| 布尔 | INTEGER 0/1 | BOOLEAN |

向量维度切换模型时需迁移列并重编码全部标签向量（同 v1 口径）。

---

## 8. 迁移策略（0007+ 重建系列）

**现状：服务器业务数据已清零（2026-11-05 两轮 wipe），仅存 user_account 1 行 + prompt_template 3 行种子。**

- 0007+ 系列重建 v2 变更所及的业务表（新结构 + 新表名）；`user_account` / `prompt_template` / `asset_build_task` **本期物理不动、ORM 保持 v1 形状**（§3.9–3.11 所示 DDL 与 v1 的差异留待 PG 迁移时统一对齐）。
- 旧表（media_context / review_record / v1 asset_tag / v1 tag_catalog / import_batch / generation_task）业务数据为空，直接 drop。
- 注意：旧迁移 0002/0005 从 `tagplatform.db.models` import 种子常量（`PROMPT_V1_SEED` / `PROMPT_DEPRECATED_OPEN_SEED`）——重写 models.py 必须保住这两个导出，否则全新库 0001→head 升级链断裂。
- 服务器升级流程不变：`alembic upgrade head`（沿用既有部署管线）。
- 迁移按既有**带数据彩排法**验证：本地建库升到 0006 → 手工插父/子表数据（含外键行、账号、模板）→ upgrade head → 验证行数 / PRAGMA foreign_key_check / 约束。注意该环境 alembic 为非事务 DDL，PRAGMA foreign_keys=OFF 在迁移内真实生效。
- **338 标签库在 v2 上线后再导入**（避免导两次）；分层 Excel 的层级列落到 tag_catalog（目录）+ tag_define（标签）两级，导入适配在实施计划中定义。

---

## 9. 分阶段落地约定

| 能力 | 表结构 | 流水线/审核 UI |
|---|---|---|
| content 级挂载 + val_fixed 标签 | ✅ | ✅ **本期** |
| closed（目录召回 + LLM 判定）/ open（mtg-v1 流水线） | ✅ | ✅ 本期 |
| scene / character 级挂载 | ✅ 预留（target_type/target_id + applicable_scope） | 后续 |
| val_generalized 泛化值 | ✅ 预留（tag_type + asset_tag 唯一键含 tag_name） | 后续 |
| PostgreSQL + pgvector | —（§7 映射） | 后续迁移 |

预留项不进流水线、不进审核界面，仅表结构与约束就位；启用属新需求，走新一轮设计。

表中的「本期」指 v2 切换后该能力必须照常可用（属于改造范围，不是新建功能）。实施已定为**一次性全平台切换**（数据层 / 服务与 API / 前端同步改绿）；任务拆分与顺序由实施计划文档定义。
