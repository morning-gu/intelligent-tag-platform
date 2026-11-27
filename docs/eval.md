# 评测方法：一致性、消融与排序

本文说明五类评测的**口径**，以及为什么口径要写死。数字只有口径一致才可比较；
换模型、换轮数、换嵌入模型之后的分数不能直接对比，只能在同一口径内做回归。

评测代码分两层：

- `src/tagplatform/services/eval/` 中的纯标准库指标模块（`consistency.py` /
  `ablation.py` / `ranking.py` / `closed_ranking.py`）：仅依赖标准库
  （`math` / `random`），不 import fastapi、numpy 或业务存储模块（分层红线），
  可被脚本、测试与离线工具独立复用；同目录下的 `service.py` / `open_service.py` /
  `report.py` / `open_report.py` / `open_review.py` 为评测服务与报告层。
- `scripts/eval_*.py`：只读的报表脚本，负责取数、聚合与打印。

---

## 1. 一致性评测（`services/eval/consistency.py` + `scripts/eval_consistency.py`）

回答的问题：**同一份输入跑 N 轮，结果稳不稳？**

旧版开放式生成曾对同一影片重复运行相同提示词，一致性指标用于衡量这类历史重复采样的稳定程度。当前开放生成已改为逐目录两阶段流程（不再包含旧版重复采样分支），脚本会跳过非重复采样格式的报告；评估当前版本的稳定性需要对同一影片重复执行整个任务，再比较最终输出。

### 数据来源

直接读历史审计报告 `mtg_report_dir/<task_id>.json` 里的 `runs[*]["tags"]`
（由早期重复采样版本写入）。**离线计算，不重跑模型**：
重跑既昂贵，又每次都是新样本，反而测不出"同一设置下的稳定程度"。

### 指标

| 指标 | 含义 | 主/辅 |
|---|---|---|
| `mean_pairwise_jaccard` | 轮与轮之间标签集合两两 Jaccard 的均值 | **主** |
| `stable_share` | 所有轮次都出现的标签 / 标签并集（稳定产出率） | **主** |
| `consistency_std` | 每条标签跨轮次的**总体**标准差（ddof=0） | 辅 |
| `icc_2_1` | ICC(2,1)，单次测量的组内相关系数 | 辅 |

`stable_share` 是审核端最该关注的一条：只有稳定出现的标签才值得沉淀成库内词条，
只出现一轮的多半是这次采样的偶然产物。

### 两条必须写明的口径

**ddof=0**。`consistency_std` 用**总体**标准差，与 numpy `std(axis=1)` 一致。
3 轮时总体标准差约为样本标准差的 0.816 倍（相差 √(3/2)≈1.2247）。口径不说清楚，
同一份数据会得出两套数字。

**ICC 分母为 0 时返回 `None`，不是 `0.0`**。这是唯一有意偏离上游
（`genometag-mllm` 的 `metrics.py` 返回 `0.0`）的地方。分母为 0 意味着样本里
没有可分离的变异（全表同值，或每个受试项的各轮均值都相同），属于"算不出"，
不是"完全不一致"。记成 0 会让报表得出**相反结论**。两种退化情形：

- 全表同值 → 所有平方和为 0；
- `MSR = 0`（各行均值相同）时，`denom = MSE + k·(MSC − MSE)/n` 恰好抵消为 0。

同样地，聚合时 `_mean()` 会先滤掉 `None` 再求宏平均——把 `None` 当 0 混进去会
把"算不出"拉成"很差"。

### ICC(2,1) 公式

记号：`n` = 受试（行，一条标签或一部片），`k` = 评分者（列，第几轮）。

```
MSR = k·Σ(行均值 − 总均值)² / (n−1)
MSC = n·Σ(列均值 − 总均值)² / (k−1)
SSE = Σ(x − 总均值)² − k·Σ(行均值−总均值)² − n·Σ(列均值−总均值)²
MSE = SSE / ((n−1)(k−1))
ICC = (MSR − MSE) / (MSR + (k−1)·MSE + k·(MSC − MSE)/n)
```

分子是"受试间真实差异"，分母额外减去"评分者系统性偏移"与残差，所以某一轮
普遍打高分不会把 ICC 抬高。**单次测量**（而非平均测量）意味着它可以直接读作
"只跑一轮有多可信"。

在 0/1 存在矩阵上，ICC 会被"多数标签只出现一次"拉向负值，**只作辅助读法**，
不要与上游的电影级连续分数横向比较。

---

## 2. 消融实验（`services/eval/ablation.py` + `scripts/eval_ablation.py`）

回答的问题：**每种输入模态各值多少？**

### 纪律：只改一个因子

上游五份配置（`configs/ablation/*.yaml`）**只有一个字段不同**（`annotation.modalities`），
其余参数逐字相同。本地把这条纪律固化成代码：

1. `Variant` 只描述"改了什么"，其余参数由调用方从同一份 `Settings` 拷贝；
2. `compare()` 只吃两个变体的逐项原值，**要求同一批受试项**——缺项直接报错，
   不允许"少评几条把平均拉高"；
3. 差异用**配对 bootstrap** 判定，而不是比较两个点估计的大小。

因此消融**只允许改 `TAGPLT_MTG_MODALITIES`**，其余设置一律不动。

### 五个变体

| 变体 | 模态 | 关掉了 |
|---|---|---|
| `full` | video, script, dialogue | ——（对照组） |
| `video-only` | video | script, dialogue |
| `script-only` | script | video, dialogue |
| `dialogue-only` | dialogue | video, script |
| `video-script` | video, script | dialogue |

模态的本地映射：`video` = 场景关键帧；`script` = 摘要 + 人物 + 场景描述 +
氛围/情绪/地点/音乐；`dialogue` = 场景内的 `台词：` 行。

两点实现细节：

- 关掉 `script` 时 `render_content` 返回空串——台词离开场景上下文后无处归属，
  一并去掉；模型看不到内容缺失（提示词里不会宣告"某模态已关闭"），这与上游一致。
- 关掉 `video` 时 `build_generation_messages` **强制清空帧表**，不依赖调用方
  自觉传空——否则"传了帧但忘了清"会静默退化成 `full`。

judge 的引用核对也使用同一 `modalities` 口径：关掉台词后再拿全文去核，会把
"引用了模型没看过的台词"误判为有效。

### 差值与显著性

`compare(full, variant)` 的差值方向**固定为 `full − variant`**：

- 正数 → 全模态更好（关掉这项模态损失了性能）；
- 负数 → 该模态不但没用还在添乱；
- `p ≥ 0.05` → 分不开，不要声称"有影响"。

方向写反是这类报表最常见的误读，所以定死在代码里而不是留给调用方选。

配对 bootstrap：对逐项差值重采样 `n_resamples` 次（默认 5000），
`p = 2·min(P(均值≤0), P(均值≥0))` 并夹到 1.0，同时给出 2.5%/97.5% 分位的
95% 区间——区间宽度比 p 值更能说明"差异有多大"。样本少于 2 项时返回中性
`(0.0, 1.0, (0.0, 0.0))`，不造 p 值。

逐资产指标全部取自审计报告，不需要调模型：

- `accepted`：最终接受条数；
- `judge_pass`：judge 通过率 = 接受 / 合并候选；
- `new_ratio`：接受结果里的新词占比（衡量"是否只是复述库"）；
- `frames`：该变体实际喂了多少帧（关掉 video 应恒为 0，用来验证配置真生效）。

---

## 3. 目录归属排序评测（`services/eval/ranking.py` + `scripts/eval_dir_assignment.py`）

回答的问题：**标签自动归到哪个目录，排得准不准？**

- **NDCG@k**：分级相关度（精确目录 2 分、同父兄弟目录 1 分、其他 0 分）；
- **Precision@k / Recall@K / HitRate@k / MRR**；
- **Random Baseline**：同候选集、同相关度下均匀随机排序的解析期望值。
  **没有这条基线，绝对分数不可解读**——候选只有 3 个目录时 NDCG@5 天然就高；
- **95% 置信带**：自举得出，用来看"与随机基线是否真的分得开"；
- **不可归属率**：真实目录不可扩展的样本占比，单独报，不混进平均。

`--strict` 把所有非精确目录降为 0 分，得到"Top-1 严格准确率"；默认口径（宽松）
把同父兄弟目录算半分，衡量"大方向对不对"。

---

## 4. 闭环排序评测（`services/eval/closed_ranking.py` + 评测服务三分）

回答的问题：**闭环任务给一部片打的标签，排得准不准、分数有没有排序意义、覆盖全不全？**

以 `/cache/MovieTags` 基准（131 部片 + MovieLens genome 0–1 相关性）为真值，
把闭环产出当作一次检索：每部成功片一条样本，在（相关门槛 θ × 截断深度 K）
网格上算 P@K / R@K / nDCG@K 宏平均，另报 Spearman 秩相关、覆盖率分布与
随机/先验两条基线。指标装配在 `services/eval/closed_ranking.py`（纯标准库，与前三节
同款红线）；原单文件 `closed_eval.py` 已四层重构三分进 `services/eval/`：
数据集读与选片在 `dataset.py`、干跑运行器（`ClosedEvalService`）在
`service.py`、报告组装/渲染/落盘在 `report.py`（服务层，与生产共用
`assemble_spec` → `ClosedTagProducer.produce` 组装段，全程 DB 零写入，
报告只落 `settings.eval_output_root/<run_id>/`）。

**真值本身是银标准**。genome relevance 是相关性**模型输出**，近似而非人工
标注（MovieTags README §6.5）——所有指标衡量的是「系统与该模型口径的一致
性」，不是「与人工真理的一致」。report.json / report.md 头部固定渲染这条
警示；跨口径（换 LLM / 嵌入 / 模板 / 库 / 数据集）的分数不可直接横比。

### 样本构造

每部成功片一条样本（`ClosedSample`）：

- `ranked` = 产出行中**库∩gt 交集内**的条目，按 (-Sf, tag_id) 排序；
- `relevance` = 该片 gt 向量限交集（连续 0–1 原值入表）；
- `system_scores` = {tag_id: Sf}，Spearman 里未产出的交集标签按 0 分补位；
- **交集外产出条目不计分只计数**（`sample_accounting.no_gt`）：交集外条目
  占着 K 位会白给 precision，算进应产出集合会冤枉 recall——都不许。

### 五条必须写明的口径

**θ 是 gt 的二值化档，与系统分无关**。gt 分 ≥ θ 才算「相关」；拍一个 θ 报
一组数，结论随拍法漂移，所以整条默认网格 θ∈{0.2, 0.4, 0.6, 0.8} ×
K∈{5, 10, 20, 40} 一起算，「结论是否随 θ 稳健」本身就是结果的一部分。
nDCG 用连续增益、不按 θ 二值化——排序质量的无损读法。

**None ≠ 0**（通用纪律第 1 条的网格版）：该 θ 下相关集为空的影片不进该格
平均（`n_valid` 记实际参与数），格值为 None 表示「算不出」，不是「得 0 分」；
无交集标签的影片只计 `invalid`，不混进任何平均。

**随机 + 先验基线必读**。随机基线是不放回均匀抽 k 的解析期望（E[P@k]=r/n、
E[R@k]=min(k,n)/n），逐片算好再宏平均；先验基线 = tag_universe 按 gt **全
影片**均值降序的固定排序（衡量「不看电影只看先验能到哪」），与系统样本同
机制喂 `evaluate_closed`。没有基线的绝对分数不可解读——系统与基线（含 95%
自举 CI）分不分得开，才是判据。

**覆盖率封顶 R@K**。闭环只打它认为值得打的标签（召回 top-50、适用性门控、
目录截断），R@K 的上限被产出行数封死：`coverage = 产出行数 / 交集标签数`
是一等指标，报 min/中位/max 分布。读 R@K 必须同读覆盖率——天花板不是
「模型差」。

**部分运行不横比**。取消/中止的 run 照常出报告（已完成片照算），但报告头
banner 明写「部分运行：指标仅覆盖完成影片，与全量口径不可直接横比」；失败
率 > 10% 追加小样本警示。指标只在成功片上算，但样本核算（total/ok/failed/
invalid/no_gt）永远响亮——不允许少评几条把平均拉高。

### 产物与历史

每个 run 落 `<run_id>/` 七件：meta.json（口径快照：数据集 sha8 / 库版本 /
模板版本 / 模型端点 / 选片清单）、progress.jsonl（增量落盘）、live.json
（实时进度明细——运行中增量发布的部分行集，见下节）、
raw_results.json（逐片留痕，离线重算指标不必重烧 LLM）、report.json
（前端契约结构）、report.md（人读版）、metrics.csv（长表
`theta,k,metric,macro,ci_low,ci_high,random_baseline`）。历史列表只扫磁盘：
meta.status=running 且无进程内注册表项 → `interrupted`（进程死亡在磁盘上
可辨认）；report.json 缺失或截断的 run 按无报告处理，摘要列为「暂无」。

### 实时进度明细（live.json）

回答的问题：**run 还没跑完，逐片明细能不能先看？** movies / detail /
llm-raw 三端点原只认终态的 raw_results.json，运行中一律 404；live.json
让运行中的 run 也能打开逐片明细（写侧照抄开放评测报告的 live.json 模式）。

机制五条：

1. **起跑写空集**：start 落 meta 骨架后同步写 `live.json = []`——读侧从
   t=0 起统一口径（运行中访问得 200 `rows==[]`，不 404）；
2. **每片完成原子增量发布**：与 progress.jsonl 同位（完成序收敛处）把
   **部分行集**整体重写进 live.json（tmp + replace 原子替换，读侧不撞半截
   文件）；行集与 raw_results.json 严格同形（同一 `_result_rows` 构造：
   gt 并入、produced 五键、成功片带 trace 键），发布失败只记日志不拖垮
   评测（旁路产物容错口径）；
3. **终态删除**：报告三件套落盘后删除 live.json（写序 meta → raw →
   report → unlink——raw 优先级已盖过 live，删是为目录语义干净；清理收尾
   在正典产物之后，unlink 抛错不连坐报告三件套）；进程中断的残留
   live.json 即部分明细，照常可读；
4. **读侧回落序 raw → live → 404**：`_movies_read` 先读
   raw_results.json，缺失/截断/形状非法再回落 live.json，两处都无合法源
   才 404「无单片明细」——单片明细 / detail / llm-raw 三端点经同一读侧
   自动随动，失败置顶、θ 门、trace/lost_at 全链路行为不变；
5. **前端入口三处**：running/interrupted 历史行点击、活跃卡「查看进度明细」
   按钮 → 进度明细视图（运行横幅 + 整块单片明细表，报告与明细互斥、互开即
   替换）；活跃 run 的 2s 轮询每 tick 驱动明细表带当前 θ 门静默重拉（行只增
   不改——不清展开/明细缓存、失败静默下一 tick 重试），run 推进终态末次
   bump 把 raw 全量送入仍开着的明细，报告就绪才收明细切报告（404 写序窗口
   期明细保留）；interrupted 行打开不轮询（无活跃快照推进，明细一次性）；
   横幅含状态/已完成/失败数，interrupted 行的 done/failed 渲染「—」（磁盘
   meta 起跑骨架恒 0，真实进度只在 progress.jsonl——根修留后续）。

### 管线追踪（漏斗口径）

回答的问题：**一部片的标签从「召回在册」到「最终产出」，每一环各丢多少、漏检的标签死在哪一环？**

闭环干跑在既有生产流程的七处采集点上顺路采集漏斗踪迹（producer 注释
编号 1–7；`generation/trace.py` 的 `ProduceTrace` 被动收集器，可选参数
`trace=None` 走与旧代码逐字节相同的路径——采集器纯旁观，不参与任何判定）：
`set_max_tags` 记全局输出上限 M（策略原值）、`add_candidates` 召回后按批
入册（`recall` = 召回余弦 Semb）、`note_batch` 批次解析成功落册、`resolve`
在门控/挂载/选择各点回填结局（全局选择环节 kept 与 m_cap 是两个 resolve
调用位）、`note_fabricated` 记编造/越组 id。漏斗计数由候选行**纯派生**
（`funnel_counts`——落盘只存逐候选行，单一事实源），经逐片摘要的
`funnel` 键、单片明细的 `trace` 包层与 misses 的 `lost_at` 列透出。

**outcome 五值；dir_asset 剔除不设独立 outcome（已拍板）**。候选结局
枚举 `kept | unmentioned | gate | unmountable | m_cap`：

| outcome | 语义 |
|---|---|
| `kept` | 最终产出（`applicability`/`sf` 随行留痕） |
| `unmentioned` | 召回在册但模型未提及（无显式上报点，快照时对结局为 None 的候选归一补齐） |
| `gate` | 适用性门控丢弃（A≤3 → Sapp=(A−1)/4 < 0.75，Sf=0 整条丢弃） |
| `unmountable` | 挂载解析失败（选择前淘汰，不占 M 名额；sf 已算出随结局留痕） |
| `m_cap` | 可挂载但被全局上限 M 截下 |

dir_asset 目录（元数据/联网搜索来源）的候选在**召回阶段**就剔除——发生在
入册之前，根本进不了 trace，因此 outcome 不设独立值，`n_recall` 计的是
**过滤后在册数**；该过滤后续上移召回侧。漏检交叉的「未召回」
（`lost_at = "unrecalled"`）语义随之覆盖 dir_asset 剔除：不在 trace 候选
= 召回就没捞到（含目录剔除），不再细分。`lost_at` 口径：在候选 → outcome
原样透传（`gate`/`unmountable`/`m_cap`/`unmentioned`）；`kept` 却出现在
漏检里属数据不自洽（kept 必产出），防御性回 None；trace 缺失 → None
「暂无」——**没数据 ≠ 没召回**，不得与 `unrecalled` 混淆。

**三处落盘，原始输出走独立文件与专用端点**。① `progress.jsonl` 每片完成
锁内追加的增量行：成功片带 `trace` 键；② `raw_results.json` 终态正典行：
成功片行带 `trace` 键（None 才省略——消费方以**键存在性**区分「无追踪」
与「追踪为空」），原始输出不进此文件、不进 API 载荷；③
`llm_raw/{tt}.json` 每片独立文件（池线程落盘，不持锁）：`raw_rows()` 即
批次行加 `content` = 成功份全文。tt 写盘前过白名单 `_TT_FILENAME_RE`
（`^[A-Za-z0-9_-]{1,64}$`），不过则跳过 raw 文件（trace 照常落行，宁缺
勿险）——路径穿越/空串/怪字符进不了文件名。读取走专用下载端点
`GET /api/v1/eval/closed/runs/{run_id}/movies/{tt}/llm-raw`（仅 admin，
FileResponse 直下；run 目录越界 / tt 不过白名单 / tt 不在 raw 行或文件
不存在，三重守卫任一不过 404——404 是既定语义不是异常，写侧本就跳过
非法 tt 与失败片）。

**重试批次只存成功份（已拍板）**。每批解析成功才 `note_batch` 一次，
`raw` = **最终成功那次**输出的全文；`parse_retried` 布尔如实记录首败重调
过。首败原文仅存前 400 字截断摘录于 producer 的 warning 日志
（`last_raw[:400]`），不落任何产物——对账以成功份为准，排查勿去日志找
全文。

**老 run 全链路「暂无」**。trace 是 2026-11-26 起的新增键：老 run 的
raw 行无 `trace` 键 → 摘要 `funnel`、明细 `trace`、`lost_at` 全 None
（键恒在，通用纪律第 1 条），llm-raw 端点 404，前端渲染说明文案。失败片
同理：失败 attempt 的采集器整体丢弃（半截追踪不落），`trace=None`——
「暂无」不是「漏斗为空」。

**snapshot / raw_rows 形状**。`snapshot()` 深拷贝出 dict，候选行序 =
召回批次序，批次行不带 `content`；`raw_rows()` 专供 llm_raw 文件落盘：

```json
{
  "max_tags": 40,
  "candidates": [
    {"tag_id": "tag_001", "tag_name": "功夫", "dir_id": "dir-action",
     "recall": 0.82, "batch": 0, "outcome": "kept",
     "applicability": 4, "sf": 0.91}
  ],
  "batches": [
    {"dir_id": "dir-action", "batch": 0, "n": 12, "parse_retried": false}
  ],
  "fabricated": ["tag_999"]
}
```

`batch` = 目录内批次序（切片起点 ÷ 批大小）；`fabricated` 按判定出现
次序累积、不去重。漏斗十键计数（全 int）：`n_recall`（在册候选数）、
`n_mentioned`（`applicability` 非 None 的候选数——模型给了适用度才算
提及）、`n_kept`/`n_gate`/`n_unmountable`/`n_m_cap`（四类 outcome 各自
计数，`unmentioned` 不单设键 = n_recall − 其余四类）、`n_fabricated`、
`n_batches`、`parse_retries`（批次里 `parse_retried` 真值计数）、
`max_tags`（策略原值 M）。

---

## 5. 开放评测口径（`services/eval/open_service.py` + `open_report.py` + `open_review.py`）

回答的问题：**开放生成给一部片打的标签，质量过不过得了独立评审？新词补充了多少？**

零漂移干跑整条开放管线（`build_spec` + `OpenTagProducer.produce`，与正式
生成同一路径，只是结果不入库），对每片全部产出（库内命中 + 新词）做
**三维独立评审**：模型逐维评 relevance、semantics 和 value（0–4 或 null），
然后单独给出建议保留、不保留或无法判断，以及一条说明最终决定的总评理由。
三维分为判断提供参考。生产 judge 继续使用自身的评分筛选规则。
干跑运行器在 `open_service.py`，报告组装/渲染/落盘在 `open_report.py`，
评审调用、解析和场景引用核验在 `open_review.py`（报告只落
`settings.open_eval_output_root/<run_id>/`）。

**2026-11-23 口径变更**：闭环遗留的 gt 加载、θ 网格、集合准确率、nDCG、
随机/先验基线、标定（一致率 + FP/FN）整体移除（`open_ranking.py` 已删）——
开放生成没有排序真值任务，genome 相关性也不该当开放标签的质量真值。
头条指标改为**评审通过率**（新词/命中分开）与**新词占比**，配三维均分/
满分率/分布；开放评测报告仅在存在部分运行、同模评审或样本不足时展示对应警示，
不再附带闭环银标准 `note` 提示。

### 样本构造

- 每部 ok 片的产出计 `total_tags` / `new_ratio`（新词占比微口径 =
  新词数 / 全部产出）；**评审成功片**的产出才进通过率与三维指标分母；
- **produce 失败片不进样本**：进 `failed_movies` 单独核算
  （`sample_accounting.failed`）；
- **评审失败片契约**：produce 成功但评审失败（ReviewRequestError 或任何
  异常）时**产出不作废**——hits/new_words 行原样保留（review=null）、
  review_error 落 raw，该片仍计 ok；通过率/三维指标整体排除出分母
  （None≠0：不是评审 0 分），计入 `sample_accounting.review_failed`；
- 评审回复缺少条目或调用失败，计入漏判或失败。有效回复中的 null 分表示无法判断。
  模型给出的保留结论为 null 时，该条不进入通过率分母。评分覆盖率分母为全部产出标签。

### 整片评审口径

使用起跑时选择的模态、最终标签名称、通用释义、挂载目录路径与定义、标签种类（`target_type`）、用途层级和来源场景整片盲评（提示词版本 `open-review-v12`，与 Judge 共用 `capabilities/prompts/_evaluation.py` 中的统一三维评价标准）。
库内标签取起跑时词库中的正式释义与所属目录定义。评审高相关分须给出有效场景 ID，场景级须命中标签的来源场景（仅依据标签自身 `scene_ids`，不回退挂载点），全片级至少两处。标签价值由模型对内容呈现、内容搜索、内容推荐三个方向分别打 0–4 整数分，程序按标签种类权重聚合为保留两位小数的价值分（0–4 分布按四舍五入归档）。报告前端与 `report.md` 均展示生成阶段（一阶段/二阶段）、挂载目录、挂载层级（全片级/场景级/角色级）、生成来源与评审引用及三方向价值明细。结构化引用不足时，可以使用评审理由中明确写出的有效场景 ID 补充。
生产 judge 的分数、理由和摘录只在报告审计中展示。评审结合整组标签判断重复与独立保留价值。

通过率统计经过生产筛选的产出中，模型建议保留的条数占有明确结论条数的比例。比较生成策略时一起查看每片产出量、自滤保留率、评分覆盖率和通过条数。
生产 judge 与开放评测评审在内容相关性、语义准确性（融合名称清晰度、释义一致性与目标目录匹配度）和标签价值（三方向加权聚合）上采用完全一致的评分标准。

默认每片一次请求。整批格式错误时重试一次，仍缺判就拆批。输出截断直接拆批，
部分合格只补送漏判的子批。拆批仍保留其他标签作为重复判断的上下文。拆出的子批
先分别评审，单条复试在其他子批都获首轮机会后执行。调用总量上限是 `2×标签数+2`，
未发出的恢复请求会标记为预算耗尽。传输失败记录客户端错误。
如果完整影片材料超出上下文窗口，报告记录评审失败原因。评审客户端默认 `reasoning_effort=low`、`max_tokens=8192`，
同一开放评测服务最多 4 条评审请求并行。对应环境变量为
`TAGPLT_OPEN_EVAL_REVIEW_REASONING_EFFORT`、`TAGPLT_OPEN_EVAL_REVIEW_MAX_TOKENS`、
`TAGPLT_OPEN_EVAL_REVIEW_CONCURRENCY`。端点不支持推理强度时设空串。
评审模型、网关环境变量名和密钥环境变量名可单独指定。空项沿用主客户端配置，
评审参数仍生效。运行元数据记录实际客户端配置和保留判定方式。

### 产物与重评

`produced/<tt>.json` 保存最终标签与当时的材料，`review/<tt>.json` 保存该片逐批
评审原始响应与失败原因（成功片同样保留）——报告里只有一句 `review_error` 时，
据此还原是漏字段、id 错误还是顶层结构漂移。重评接口
`POST /runs/{id}/rereview` 从原运行的完成影片和快照另存一份报告，
返回新 `run_id` 和 `source_run_id`。原报告和生成样本状态保留。缺少材料快照的
旧运行需要重新起跑。报告接口只提供当前格式的报告。

`GET /runs/{id}/movies/{tt}/diagnostics` 按片透出生产审计与评审现场（两份都缺
→ 404）。报告页逐片明细里的「生产审计」展开时加载，显示逐候选的三维分、目录
判决、摘录和去向。judge 中断时仍显示送审候选。逐次 judge 和独立评审响应可展开查看，
未完成的评审条目会列出对应的失败或预算耗尽原因。

`live.json` 在影片产出和整片评审完成时更新。前端轮询直到取得最终报告。
最终产物包括 `meta.json`、`raw_results.json`、`report.json`、
`report.md`、`metrics.csv`、生产 `audit/` 和评审 `review/`。CSV 中每个比例的 `n`
对应其实际分母。报告提示部分运行、共享模型可能带来的偏差和样本不足。

---

## 6. 三条通用纪律

1. **`None` ≠ 0**。算不出就报"暂无"并排除在聚合外。把无样本、分母为 0、
   算不出 当作 0，是评测代码最常见的错误来源，且会让结论方向反转。
2. **换口径必须换组**。不同嵌入模型、不同 LLM、不同轮数的分数不可直接比较，
   只在自己这一组内部做前后回归。
3. **消融只改一个字段**。改两处之后无论结果如何都无法归因，实验等于白跑。