# 闭环任务评测设计（前端页签 + 干跑报告，DB 零写入）

> 状态：待用户批准（2026-11-20）
> 前序：`docs/eval.md`（三类既有评测口径，本设计为其第四类，纪律全部继承）、
> `docs/superpowers/specs/2026-10-31-intelligent-tag-platform-design.md`

## 1. 意图与为什么

闭环生成（`ClosedTagProducer`）目前唯一的可观测面是人工审核：跑完一批任务，
标签选得准不准，除了翻 `pre_asset_tag` 没有数字化的回答。本设计给闭环任务一个
**可重复、可对比的量化标尺**：以 `/cache/MovieTags` 基准（131 部片 +
MovieLens Genome 情绪标签 0–1 相关性）为真值，把闭环产出当作一次「检索」，
在不同相关性阈值 θ 与截断深度 K 下计算 P@K / R@K 等排序指标。

形态不是离线脚本，而是**平台的一个前端页签**：管理员在页面上选数据集、选影片
集、起跑；后台**干跑**整条闭环管线（召回 → LLM 判定 → Sf 终分），全程在内存中
完成，产出数据报告落盘。**不写数据库是硬约束**：评测是实验行为，不允许污染生产
数据（media_asset / 生成任务 / pre_asset_tag / audit 一概不碰）。

成功的样子：改一版 prompt，重跑一轮，在历史列表里看到 nDCG@10 的变化趋势，
且每份报告的每个数字可复核（raw_results.json 留痕，离线可重算）。

## 2. 目标 / 非目标 / 反目标

**目标**

- θ×K 网格下的 P@K / R@K，外加 nDCG@K（连续增益）、Spearman 秩相关、
  覆盖率分布、随机 + 全局先验两条基线、bootstrap 95% 置信区间
- 多次运行多次记录：每轮独立目录，历史可查，报告自含全部上下文
- 数据集**运行级指定**：后端发现式列出可选 gt，前端创建任务时下拉选择；
  各 gt 与当前标签库的交集数在列表里直接可见
- DB 零写入；报告文件目录是唯一持久层
- 跑批增量落盘，中断可看部分结果

**非目标**

- 不评 open 模式、judge 环节、人工审核后结果（均为后续变体）
- 不做两轮并排对比视图（历史表的关键指标列先行，真需要再加）
- 不支持前端提交任意服务器路径（数据集根路径在 .env，见 §5）
- 评测结果不入库、不进工作台指标

**反目标（技术上成功即算失败的情形）**

- 评测跑一趟，审核队列 / 资产表多出一堆测试数据——违反「不写库」
- 指标没有随机基线与置信区间，绝对值被直接当结论引用
- 不同口径（换 LLM / 换嵌入 / 换模板 / 换库）的分数被横向比较

## 3. 数据事实（均已核实）

**远端 `/cache/MovieTags`（v1.0，2026-11）**

- 131 部片（1954–2019），17,311 个场景，282,411 帧；`data/<ttID>/content.json`
  = 分场景结构化输入（asset.asset_id/title + content.description/characters[]/
  scenes[]，场景含 scene_id/start_ms/end_ms/description/dialogues[]/frames）
- `gt/` 三份 CSV，统一三列 `movieId,tagId,relevance`，movieId 为 IMDb tt ID：
  - `genome-scores.csv`：131×1,128，147,768 行
  - `genome-emotion-scores.csv`：131×83，10,873 行，**稠密无缺失**
  - `genre-scores.csv`：131×19，0/1 二值
- content.json 与平台上游 schema（`schemas/upstream.py`）基本同构；差异：
  台词字段名 `speak_id`（上游是 `speaker_id`）、frames 列导入层忽略
- **银标准警示（README §6.5 原文）**：genome relevance 是相关性**模型输出**，
  近似而非人工标注。报告必须明写此口径

**平台侧**

- 对齐键天然成立：线上 `tag_define.tag_id` 就是 MovieLens tagId 字符串
  （已连库验证：'18'→absurd…，83/83），`content.json` 的 asset_id 就是 gt 的
  movieId——**零映射直连**
- 闭环终分 Sf = G·(0.2·Semb + 0.5·Sapp + 0.3·Sevi)（`generation/closed.py`），
  生产链路落在 `pre_asset_tag.relevance_score`；**producer 本身不写库**
  （写库是执行器职责）——评测直调 producer 即天然干跑
- 天花板要正视：召回只取 embedding **top-50**（当前库共 83 标签）、
  applicability≥3 门控、单目录 max_tags 截断——**R@K 上限被产出行数封死**，
  所以覆盖率是一等指标（§7）
- `eval/ranking.py` 已有纯标准库指标原语（P@K/R@K/nDCG/MRR/bootstrap/
  解析随机基线，分级相关度 + `relevant_grade` 阈值参数）——直接复用，
  θ 就是 relevant_grade
- 当前线上库：2 目录（情感标签/变化范围）+ 83 标签，与
  `genome-emotion-scores.csv` 完全对齐

## 4. 总体架构

```
前端「闭环评测」页（admin）
  │ GET  /api/v1/eval/closed/datasets        发现式数据集列表
  │ POST /api/v1/eval/closed/runs            起跑（后台线程），返 run_id
  │ GET  /api/v1/eval/closed/runs            历史列表（扫磁盘）
  │ GET  /api/v1/eval/closed/runs/{id}       状态/进度（轮询）
  │ POST /api/v1/eval/closed/runs/{id}/cancel 取消
  │ GET  /api/v1/eval/closed/runs/{id}/report 报告渲染
  ▼
EvalRunService（后台线程，进程内注册表）
  只读：标签库 + prompt 模板（DB）
  只读：content.json + gt CSV（文件）
  内存：组装 spec → ClosedTagProducer.produce（不落库）
  增量：每片完成追加 progress.jsonl
  ▼
data/eval/closed/<run_id>/
  meta.json / progress.jsonl / raw_results.json /
  metrics.csv / report.json / report.md
```

**干跑语义清单**：不建 media_asset、asset_build_task、tag_generation_task、
pre_asset_tag、tag_quality_evaluation；不写 data/audit；DB 只读
（tag_catalog / tag_define / prompt_template）。写盘只发生在
`data/eval/closed/<run_id>/`。LLM / embedding 走服务现配置
（settings，不新增端点配置）。

`/cache/MovieTags` 只读；`/cache/MovieNet` 不碰（既有红线）。

## 5. 数据集与标签库的指定

**数据集 = 一份 gt CSV（+同根目录的 content.json）。**

- `.env` 增 `TAGPLT_EVAL_DATASET_ROOT`（默认 `/cache/MovieTags`）——只定扫描根
- `GET /eval/closed/datasets`：扫描根下 `gt/*.csv`；合法 = 首行三列
  `movieId,tagId,relevance` 且数据行可解析。每项返回：
  `{id（文件 stem）, gt_path, 影片数, 标签数, 与当前库交集标签数, 可用影片数
  （data/<tt>/content.json 存在 ∩ gt 有分）}`——换库后哪个 gt 还能用一眼可见
- 进程内缓存（按 mtime 失效）；genome-scores 147k 行首次读取约秒级，可接受
- POST 起跑带 `dataset: <id>`；选中项的 id / 路径 / 行数 / gt 文件 sha256
  前 8 位记进 meta——历史报告自证当时评的是哪份数据
- `/cache/MovieTags/gt/` 三份 gt 天然成为三个可选项：将来导入全量 1128 标签库
  就选 `genome-scores`
- **不支持**前端提交任意路径（防误指；换数据集根 = 改 .env）

**标签库：不另指定，就是当前线上库（只读快照）。**

- 运行时读 DB 的 active 标签 + 目录；库指纹（目录清单 + 标签清单的 sha256
  前 8 位 + 计数）记进 meta。换库后跑的评测自然用新库，历史报告不失效
- **指标计算域 = 库标签 ∩ gt 标签**。当前 83∩83 全交集；将来部分重叠时，
  交集外的产出标签**不计分只计数**（「无 gt 覆盖 N 条」单独报出——既不白给
  precision 也不冤枉 recall）

## 6. 评测运行

- **spec 组装与生产同路**：把执行器组装 GenerationSpec 的代码（模板选择/渲染、
  policy 默认值）抽成公共函数，执行器与评测运行器共用，避免两处漂移；
  `ClosedTagProducer.produce` 直调（session 传只读会话——producer 本就不写库）
- 影片集三选一（**用户已确认：先 30 部试点，再放全量**）：
  - `preset30`：对该数据集可用影片排序后以 seed=20261120 抽 30，清单记进 meta
  - `all`：全部可用影片
  - `custom`：tt ID 列表（前端选择/粘贴）
- 并发：线程池，默认 4（起跑参数可调）；llm client 线程安全性在实施时验证，
  不满足则退并发度 1（功能不受影响，只是慢）
- **单片失败处理**：produce 抛错（LLM 重试耗尽 / 契约解析违规 / 空候选）→
  原地重试 2 次；仍失败记为**失败片**，继续后续影片。**连续 5 片失败**判定
  系统性故障（如 LLM 端点失联），中止整轮（status=aborted，已完成部分照常
  组装部分报告）
- **样本核算响亮**（eval.md「不允许少评几条把平均拉高」的运行器版）：指标
  只在成功片上计算，但每个聚合计都带「成功 N / 总 M」，失败影片清单入报告；
  失败率 > 10% 时报告头部警示小样本口径——绝不悄悄缩水
- 增量落盘：每片完成（**无论成败**）追加 `progress.jsonl` 一行
  `{tt, ok, produced, elapsed_ms, error?}`
- **取消与部分报告**：cancel 时已完成部分照常组装完整四件套，meta.status=
  cancelled + completed N/M，报告头明写「部分运行：指标仅覆盖完成影片，与
  全量口径不可直接横比」；「中断」（进程死亡）的 run 无 report.json，历史
  扫描以 meta.status=running 且无活跃注册表项推断——两种终态在磁盘上可区分
- **并发限制**：同一时刻只允许一个活跃 run，已有活跃 run 时第二个 POST 返回
  409 并附 active_run_id（LLM 端点是共享生产设施；「多次记录」靠顺序多跑）
- 状态机：running → succeeded | cancelled | aborted | interrupted（磁盘推断）
- 状态注册表：进程内 dict（run_id → 状态/进度/失败数/错误）；重启后历史从
  磁盘恢复
- run_id = `run-YYYYMMDD-HHMMSS-<4 随机字符>`（服务器时钟）
- 时间预算（预期管理）：每片 1 次嵌入批 + 1–2 次 LLM 组调用（当前库 83 标签
  实际落在一个目录组、召回后 ≤50 候选），单片约 1–3 分钟；并发 4 时
  试点 30 部约 15–30 分钟，全量 131 部约 1–2 小时

## 7. 指标口径（继承 eval.md 三条纪律：None≠0 / 换口径必须换组 / 基线必须）

**样本构造**（每部影片一条 `RankingSample`，复用 `eval/ranking.py` 原语）：

- `ranked` = 该片产出标签按 Sf 降序（并列按 tag_id 字典序稳定化）；
  短列表即短列表——P@K 分母固定为 K，缺位算未命中（与 ranking.py 口径一致）
- `relevance` = `{tag_id: gt 分}`，仅交集内标签（gt 连续 0–1 原值入表）

**网格与指标**：

| 指标 | 定义 | 说明 |
|---|---|---|
| P@K | 前 K 中 gt≥θ 占比 | `precision_at_k`，`relevant_grade=θ` |
| R@K | 前 K 覆盖 gt≥θ 集合比例 | `recall_at_k`；受产出行数封顶，须与覆盖率同读 |
| nDCG@K | gt 连续分作增益 | `ndcg_at_k`，不过阈值——排序质量的无损读法 |
| Spearman | 每片 83 对（系统分， gt 分）秩相关 | 未产出标签记 0 分并列底部（平均秩）；新实现进 `eval/closed_ranking.py`（纯标准库，含并列处理） |
| 覆盖率 | 产出行数 / 交集标签数 | 一等指标：解释 R@K 天花板；报 min/中位/max 分布 |
| 随机基线 | `random_baseline` 解析解 | relevance 全表覆盖（稠密矩阵天然满足） |
| 先验基线 | 按标签 gt 均分降序的固定排序 | 用**该数据集全部影片**算（非仅选片集），衡量「不看电影只看先验」能到哪 |
| bootstrap 95% CI | `bootstrap_ci`，宏平均上 | 与随机基线是否分得开，才是判据 |

- 默认档 θ ∈ {0.2, 0.4, 0.6, 0.8}、K ∈ {5, 10, 20, 40}（起跑参数可改，
  K 超过交集大小时该档无效并警告）
- 聚合：**宏平均**（每片等权，主口径）+ P@K 的全池**微平均**（辅）
- 「无相关标签」影片（该 θ 下相关集为空）：计入「无效影片数」单独报，
  不混进平均（None≠0）
- 报告头明写口径三件套：LLM/embedding 端点 + prompt 模板 id/版本 + 库指纹，
  并注明「不同口径的分数不可跨组比较」

## 8. 报告产物（`data/eval/closed/<run_id>/`）

| 文件 | 内容 | 消费者 |
|---|---|---|
| `meta.json` | run 参数、数据集 id/路径/指纹、库指纹、模板版本、端点、影片清单、时间、状态 | 历史列表 / 追溯 |
| `progress.jsonl` | 每片一行 `{tt, ok, produced, elapsed_ms, error?}`（成败都落行），增量追加 | 中断恢复 / 进度 / 失败核算 |
| `raw_results.json` | 每片 {tt, title, produced: [{tag_id, tag_name, sf, confidence, evidence 数}], gt 向量, 耗时} | **留痕可复核**：离线重算指标不必重烧 LLM |
| `metrics.csv` | 聚合级长表（θ, K, metric, macro, CI, 基线值） | Excel/后续分析 |
| `report.json` | 前端渲染结构（汇总卡 + 样本核算「成功/失败/无效」+ θ×K 双表 + 基线 + 覆盖分布 + 每片附表 + 失败清单） | 页签 |
| `report.md` | 人读版（同样表格的 markdown） | 人 / 存档 |

## 9. API 契约（全部 admin 权限，与 model-config 同栏）

```
GET  /api/v1/eval/closed/datasets                 → [{id, gt_path, movies, tags, library_overlap, usable_movies}]
GET  /api/v1/eval/closed/datasets/{id}/movies     → [{tt, title}]（custom 选择器数据源；进程内缓存按 mtime 失效）
POST /api/v1/eval/closed/runs                     → {run_id}；已有活跃 run 时 409 {active_run_id}
     body: {dataset, movies: {mode: preset30|all|custom, ids?},
            thetas?, ks?, concurrency?}            # 详见 §6/§7，全部有默认
GET  /api/v1/eval/closed/runs                     → [历史条目]（扫磁盘 meta，含进行中/中断）
GET  /api/v1/eval/closed/runs/{run_id}            → {status, done, total, failed, started_at, error?}
POST /api/v1/eval/closed/runs/{run_id}/cancel     → 组装部分报告（语义见 §6）
GET  /api/v1/eval/closed/runs/{run_id}/report     → report.json
GET  /api/v1/eval/closed/runs/{run_id}/files/{name} → 下载 report.md / metrics.csv / raw_results.json
```

## 10. 前端页面

- 新页 `pages/ClosedEval.tsx` + `api/eval.ts`（各配测试）；App.tsx 挂
  `/eval-closed` 于 AdminRoute；Layout 的 ADMIN_MENU_ITEMS 增「闭环评测」
- **零新依赖**：antd 表单/表格/进度条 + 既有 `components/BarChart.tsx`
  （画 R@K 随 θ 的曲线族）
- 视图四块：
  1. 创建表单：数据集下拉（datasets API，选项里带交集数提示）、影片集
     （试点 30 / 全部 / 自定义多选——数据来自 datasets/{id}/movies，
     按标题搜索）、θ/K 默认档可编辑、并发度；有活跃 run 时起跑按钮禁用
     并提示（对应 §6 单活跃 run 限制）
  2. 进行中卡片：进度条 done/total + 取消按钮
  3. 历史表：时间、数据集、影片数、状态、关键指标列（nDCG@10、
     P@10@θ=0.4、R@20@θ=0.4、覆盖率中位）——多轮趋势在表里直接可见；
     点行进完整报告
  4. 报告视图：汇总卡 → θ×K 双表（P/R）→ 基线对照 → 覆盖分布 →
     Spearman → 每片明细（可折叠）→ 下载链接
  （报告头固定渲染银标准警示与口径三件套）

## 11. 决定清单

**用户已确认**：先 30 部试点再放全量；前端页签形态；评测只出数据报告不写
数据库；前端创建任务时支持指定数据集。

**我的默认决定（可推翻，实施前请过目）**：

1. 页签放 admin 专属菜单（烧 LLM tokens，与「模型配置」同栏）
2. θ/K 默认档 {0.2, 0.4, 0.6, 0.8} × {5, 10, 20, 40}
3. preset30 抽样 seed=20261120，清单入 meta（可复现）
4. 并发默认 4；llm client 线程不安全则退串行
5. 评测读审核前语义（无 judge / 人工环节；judge 后变体留扩展）
6. 交集外标签不计分只计数
7. 先验基线用数据集全影片计算
8. 支持取消运行（cancel 标志，已完成部分照常落盘）
9. 报告文件是唯一持久层（含历史），不建任何 DB 表
10. 数据集发现式列表；不支持前端传任意路径
11. 单片失败：重试 2 次后记失败继续；连续 5 片失败中止整轮；指标只算
    成功片，但样本核算响亮（N/M + 失败清单 + 失败率 >10% 警示）
12. cancel 组装部分报告，显著标注「部分运行」口径，与全量不可直接横比
13. custom 影片集 = 带标题搜索的多选选择器（为此新增
    `datasets/{id}/movies` 端点）
14. 同一时刻仅一个活跃 run（第二个 POST 返回 409）

**留给实施者**：spec 组装公共函数的抽取边界（执行器 ↔ 运行器）；llm client
线程安全验证；report.json 具体字段名与前端布局细节；metrics.csv 行格式；
自定义影片集的交互形态（多选 vs 粘贴）；gt CSV 解析健壮性（引号/空行）；
标签侧嵌入缓存机制确认（reembed_catalog 的现状，无缓存则在运行器内加）。

**未来扩展（非本版）**：并排对比视图；Semb-only 消融基线；judge/审核后变体；
open 模式评测；`docs/eval.md` 增第四节「闭环排序评测」口径（实施时随代码一并
补文档）。

## 12. 约束与红线

- `tagplatform/eval/` 纯标准库红线不破：新指标代码（Spearman、网格装配、
  基线）进 `eval/closed_ranking.py`，不 import fastapi / numpy / 平台模块
- `ClosedTagProducer` 不改语义；spec 组装只做「抽公共」，不改行为
- 不写 DB / 不写 data/audit；`/cache/MovieTags` 只读；`/cache/MovieNet` 不碰
- 部署面：只动 8100 实例；同事 8101 开发实例不动；评测运行不要求重启服务
