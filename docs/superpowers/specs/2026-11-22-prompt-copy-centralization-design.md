# Prompt 文案集中管理设计（路线 B：集中代码包）

日期：2026-11-22 ｜ 状态：待批准
前置：四层重构 Task 3（prompt 本地化，0018 迁移 drop `prompt_template` 表，git 即版本）
分支：`refactor/prompt-centralization` 自 `main` @ `7f2c08e`

## 意图与背景

当前 prompt 文案分散在四处：`capabilities/prompts/`（closed/open 两条 mode 模板）、
`services/generation/open/mtg/`（生成/judge/释义/复核 5 段 + 各自重试提示）、
`services/content/assets/builder.py`（场景分析/整片摘要）、`services/eval/tag_quality.py`
（质量评分 rubric）。用户要求"统一配置"；已拍板走**路线 B：全部指令性文案集中到
`capabilities/prompts/` 一个包，组装逻辑原地不动，git 继续当版本机制**——不逆转
Task 3 的"文案是开发者资产"决策，不做运行时热改。

集中后的收益：调优活动（badcase rubric 落地、judge quote 拍板）只改一处文件；
配置页经聚合端点一页看全部（吸收路线 A 的只读视图）。

## 目标 / 非目标 / 反目标

**目标**
- 所有指令性文案常量归入 `capabilities/prompts/`（按流水线分组）
- 流水线组装函数改为 import 常量，行为零变化（逐字搬运）
- 新增只读聚合端点 + 前端配置页"流水线文案"区块，一处可看全部
- 既有测试全绿，协议串/marker/mock 咬合不动

**非目标**
- 不做运行时热改 / DB 版本机制（路线 D 否决：逆转 Task 3，评测复现性靠 git 溯源）
- 不做配置文件外置 / 环境覆盖（路线 C 否决：无 8100/8101 文案差异需求，且形状 lint
  代替不了测试钉死）
- 不改任何文案内容本身；不动 prompt_samples 审计；不动 OPEN_TEMPLATE_TEXT 存续现状

**反目标**
- 搬运过程中任何文案字面量被改动（"搬文案"与"改文案"分开提交）
- 把 prompt-解析器协议对拆到必须跨三个包才能改一次输出形状

## 约束（红线）

- **分层红线**：`capabilities/prompts/` 纯常量 + 纯函数，不得 import
  fastapi / sqlalchemy / services（现有红线沿用，新模块同守）
- **API 契约只增不改**：现有 `GET /api/v1/prompts`（mode 过滤、排序、405/404 锁）
  行为零变化；新增端点为纯增量
- **无 DB 变化、无迁移**
- **协议不动**：GENERATION_MARKER / JUDGE_MARKER / VERIFY_MARKER、场景行/候选行/
  图注格式、MOODS/FEELINGS 词表、parse 契约、mock 咬合全部原位原样
- **逐字搬运**：搬运提交里文案 diff 为纯位移（删一处 + 原样加一处），reviewer 按
  此验收

## 搬运边界线（本设计的核心决策）

**搬：无参数槽的完整指令文案块**——告诉模型做什么、怎么评、输出什么的段落。
组装函数改为引用常量，拼接行为不变。

**留：含参数槽的行、协议串、渲染格式、双向词表、逻辑**
1. 含参数槽的行（输出格式 JSON 行嵌 `scope`/`dir_field`/`max_tags`，builder 头部
   嵌 title/genres/字幕块等）——留在组装函数内，避免对 JSON 字面量做 `{{ }}`
   转义手术（转义=改字面量，违反逐字红线）
2. 协议串与渲染格式：三个 MARKER、场景行/候选行格式、keyframe 图注、
   `render_scene` 的字段标签（电影：/摘要：/台词：…）——与 mock `_SCENE_LINE_RE`
   及 parse 端咬合
3. MOODS/FEELINGS 词表——**双向协议**：进 prompt（`prompt_catalog`）也在解析端
   归一输出（`to_emotions`、`_BY_NAME/_BY_CODE`），`content_template` 与
   `test_assets_emotions` 直接消费 → 留 `emotions.py`
4. 逻辑与预算：`DIMENSIONS`/`_SCOPE_LABELS`（展示映射）、`_MAX_*`、
   `ASSET_BUILDER_VERSION`（产物版本，非 prompt 版本）、`build_*` 组装/解析函数

新搬常量**不经 `render_prompt`/`str.format` 渲染**——组装函数直接引用或自行
format 指定槽位；`{content}` 占位符 + 花括号转义约定仍只属于 closed/open
mode 模板（`templates.py` 现状不变）。

## 搬运清单

| 来源 | 搬什么 | 去向 |
|---|---|---|
| `mtg/prompts.py` | `JUDGE_ROLE_BRIEF`、`JUDGE_CRITERIA` | `prompts/mtg.py` |
| `mtg/prompts.py` | `_generation_instructions` 的 scene/film 两段 goal 文案、"如何填写" bullets、生成角色行（"你是影视内容分析专家…"） | `prompts/mtg.py`（`SCENE_GOAL_TEXT`/`FILM_GOAL_TEXT`/`HOW_TO_FILL_TEXT`/`GENERATION_ROLE_TEXT`） |
| `mtg/prompts.py` | `_dir_hint_block`/`_library_hint_block` 的头尾说明文案 | `prompts/mtg.py` |
| `mtg/descriptions.py` | 释义补写指令 + `_RETRY_HINT` | `prompts/mtg.py` |
| `mtg/verify.py` | 复核指令 + `_RETRY_HINT` | `prompts/mtg.py` |
| `mtg/judge.py` | `_RETRY_HINT` | `prompts/mtg.py` |
| `closed/producer.py` | `_RETRY_HINT` | `prompts/templates.py`（`CLOSED_RETRY_HINT`） |
| `eval/tag_quality.py` | `_PROMPT` + `PROMPT_VERSION` | `prompts/quality.py` |
| `builder.py` | `scene_messages`/`summary_messages` 的无参数指令段（要求 1-6、只输出 JSON 行、摘要改写要求） | `prompts/assets.py`（带占位符断点拆段） |

留原地（除边界线四类外）：closed 候选分组 JSON 拼接（producer produce 内）、
`OPEN_TEMPLATE_TEXT`（已不消费，口径存续）。

## 包结构

```
capabilities/prompts/
  __init__.py    — 现有 mode 契约（get_prompt/render_prompt/MODES/…）不变，追加导出
  templates.py   — closed/open 模板（不变）+ CLOSED_RETRY_HINT
  mtg.py         — 开放流水线指令文案 + 三个重试提示
  assets.py      — 资产构建指令文案段
  quality.py     — tag-quality rubric + PROMPT_VERSION
  registry.py    — list_prompt_registry()：只读聚合清单（供 API/前端一页看全部）
```

## 聚合端点契约

`GET /api/v1/prompts/registry`（只读、无状态、与 `/prompts` 同门禁口径——现状无
admin 门禁）：

```json
[
  {"module": "closed",  "title": "闭环生成",     "items": [{"name": "CLOSED_TEMPLATE_TEXT", "text": "..."}, {"name": "CLOSED_RETRY_HINT", "text": "..."}]},
  {"module": "mtg",     "title": "开放流水线",   "items": [{"name": "SCENE_GOAL_TEXT", "text": "..."}, …]},
  {"module": "assets",  "title": "内容资产构建", "items": […]},
  {"module": "quality", "title": "标签质量评估", "items": [{"name": "RUBRIC_TEXT", "text": "..."}, {"name": "PROMPT_VERSION", "text": "tag-quality-v4"}]}
]
```

条目 = 常量名 + 文案本体；registry.py 从同包各模块静态聚合，与
`/api/v1/prompts` 的 mode 契约并存互不影响。

## 前端视图

`ModelConfig` 的 Prompt 模板区下新增"流水线文案"区块：`getPromptRegistry()` →
Collapse/Accordion 按 module 分组，条目只读展示（常量名 + `pre-wrap` 文案）。
无编辑能力（红线：文案=代码）。

## 测试策略

- **搬运保真**：既有 mock 咬合、parse 契约、`test_api_prompts_admin` 等测试即保真
  网（文案漂移会红）；全量回归绿 = 搬运无字面量改动
- **新增**：registry 端点契约测试（形状、四个 module 覆盖、只读）；
  `test_prompts_local` 增分层红线断言（新模块不得 import fastapi/sqlchemy/services）；
  前端 registry 区块渲染测试（mock registry → 分组与条目断言）
- 预估：后端 ~930 + 前端 ~260，新增约 6-8 个用例

## 工作量与任务切分（预估，计划阶段细化）

3 任务：
1. mtg + closed 文案搬运（含 templates.py 的 CLOSED_RETRY_HINT），import 更新，全绿
2. assets + quality 搬运 + `registry.py` + 聚合端点 + 契约测试
3. 前端 `getPromptRegistry` + 配置页区块 + 测试

分支 `refactor/prompt-centralization` 自 `main` @ `7f2c08e`；执行方式沿用内联 TDD
+ 终末独立 reviewer（用户既有偏好）。

## 留给计划的细节

- 各常量精确命名与拆分断点（以"无参数槽完整块"为最小单元）
- registry 条目是否收录 CLOSED/OPEN 模板（建议收录，形成唯一全量清单——closed
  条目与 `/prompts` 端点内容同源同包，无双写）
- 前端区块的视觉细节（分组折叠默认态等）
