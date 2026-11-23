# 评测方法：一致性、消融与排序

本文说明五类评测的**口径**，以及为什么口径要写死。数字只有口径一致才可比较；
换模型、换轮数、换嵌入模型之后的分数不能直接对比，只能在同一口径内做回归。

评测代码分两层：

- `src/tagplatform/eval/`：**纯标准库**（`math` / `random`），不 import fastapi、
  numpy 或任何平台模块（设计文档 §9.4 的分层红线）。因此这些指标可以被脚本、
  测试、甚至未来的离线工具独立复用。
- `scripts/eval_*.py`：只读的报表层，负责取数、聚合与打印。

---

## 1. 一致性评测（`eval/consistency.py` + `scripts/eval_consistency.py`）

回答的问题：**同一份输入跑 N 轮，结果稳不稳？**

旧版开放式生成对同一影片重复运行相同提示词，一致性指标衡量这些重复采样的稳定程度。`mtg-v4` 的两轮分别提炼场景级和影片级特征，输入和目标各异，脚本会跳过这类报告。评估新版的稳定性需要对同一影片重复执行整个任务，再比较最终输出。

### 数据来源

直接读审计报告 `mtg_report_dir/<task_id>.json` 里的 `runs[*]["tags"]`
（由旧版 `pipeline.run_open_generation` 逐轮写入）。**离线计算，不重跑模型**：
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

## 2. 消融实验（`eval/ablation.py` + `scripts/eval_ablation.py`）

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

## 3. 目录归属排序评测（`eval/ranking.py` + `scripts/eval_dir_assignment.py`）

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

## 4. 闭环排序评测（`eval/closed_ranking.py` + `services/eval/` 三分）

回答的问题：**闭环任务给一部片打的标签，排得准不准、分数有没有排序意义、覆盖全不全？**

以 `/cache/MovieTags` 基准（131 部片 + MovieLens genome 0–1 相关性）为真值，
把闭环产出当作一次检索：每部成功片一条样本，在（相关门槛 θ × 截断深度 K）
网格上算 P@K / R@K / nDCG@K 宏平均，另报 Spearman 秩相关、覆盖率分布与
随机/先验两条基线。指标装配在 `eval/closed_ranking.py`（纯标准库，与前三节
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

每个 run 落 `<run_id>/` 六件：meta.json（口径快照：数据集 sha8 / 库版本 /
模板版本 / 模型端点 / 选片清单）、progress.jsonl（增量落盘）、
raw_results.json（逐片留痕，离线重算指标不必重烧 LLM）、report.json
（前端契约结构）、report.md（人读版）、metrics.csv（长表
`theta,k,metric,macro,ci_low,ci_high,random_baseline`）。历史列表只扫磁盘：
meta.status=running 且无进程内注册表项 → `interrupted`（进程死亡在磁盘上
可辨认）；report.json 缺失或截断的 run 按无报告处理，摘要列为「暂无」。

---

## 5. 开放评测口径（`services/eval/open_service.py` + `open_report.py` + `open_review.py`）

回答的问题：**开放生成给一部片打的标签，质量过不过得了独立评审？新词补充了多少？**

零漂移干跑整条开放管线（`build_spec` + `OpenTagProducer.produce`，与正式
生成同一路径，只是结果不入库），对每片全部产出（库内命中 + 新词）做
**三维独立评审**：评审模型按 tag-quality-v4 rubric 逐维评
relevance/semantics/value（0–4 或 null），每条附生产 judge 的三维分作
**参考**（judge_reference，明确仅供参考、须独立核对材料）。通过与否由
评审层按阈值推导（`REVIEW_PASS_FLOORS`：relevance≥3 / semantics≥3 /
value≥2，与生产 judge 保留阈值对齐），模型只给分不回 pass/fail。
干跑运行器在 `open_service.py`，报告组装/渲染/落盘在 `open_report.py`，
评审调用/解析/摘录核验在 `open_review.py`（报告只落
`settings.open_eval_output_root/<run_id>/`）。

**2026-11-23 口径变更**：闭环遗留的 gt 加载、θ 网格、集合准确率、nDCG、
随机/先验基线、标定（一致率 + FP/FN）整体移除（`open_ranking.py` 已删）——
开放生成没有排序真值任务，genome 相关性也不该当开放标签的质量真值。
头条指标改为**评审通过率**（新词/命中分开）与**新词占比**，配三维均分/
满分率/分布。银标准警示（note）保留：评审分仍是模型输出，不是人工标注。

### 样本构造

- 每部 ok 片的产出计 `total_tags` / `new_ratio`（新词占比微口径 =
  新词数 / 全部产出）；**评审成功片**的产出才进通过率与三维指标分母；
- **produce 失败片不进样本**：进 `failed_movies` 单独核算
  （`sample_accounting.failed`）；
- **评审失败片契约**：produce 成功但评审失败（ReviewRequestError 或任何
  异常）时**产出不作废**——hits/new_words 行原样保留（review=null）、
  review_error 落 raw，该片仍计 ok；通过率/三维指标整体排除出分母
  （None≠0：不是评审 0 分），计入 `sample_accounting.review_failed`；
- 条目级 pass=null = 评审漏判：排除出通过率分母，计 `review_missing`；
  显式 null 分（无法判断）是有效评分，pass=False 且计 undetermined，
  与漏判不同。

### 五条必须写明的口径

**通过率语义**。通过率 = 评审通过条数 / 有判定条数（微口径），「通过」
= 三维分同时过阈值（relevance≥3 / semantics≥3 / value≥2）——衡量「独立
评审认为达到生产保留标准的标签占比」。新词/命中分开报；产出规模
（produced_stats 的 min/中位/max）必须同读。

**judge 参考不是评审结论**。评审清单附生产 judge 的三维分与理由
（judge_reference），提示词明确定位为仅供参考——评审必须独立核对材料，
可同意可推翻。judge 审计段（自滤保留率 / 三维分聚合 / 淘汰原因 / 库内
分档）单独呈现生产侧行为，不与评审通过率混算。

**None ≠ 0**。无产出、评审失败片、漏判条目——一律 null（算不出/排除），
不是 0 分；计数类（review_missing / 各核算数）如实记 0。

**摘录必核**。评审给 relevance≥3 必须附 10–25 字原文摘录，经归一化
渲染文本核验（8 字连续窗口，对齐生产 judge 判法）；核不中降档到 1
（理由记「摘录未命中原文」），防评审员空口给高分。

**同模评审警示**。评审与生成同模型时（`review_same_model`）报告头
banner 明写：通过率绝对值系统性偏高，趋势对比可用、绝对值需打折解读。
评审端点经 `open_eval_review_model/_base_url_env/_api_key_env` 三键
独立配置，全空回落主 LLM（同模）。

### 产物与历史

每个 run 落 `<run_id>/` 六件 + audit/ + produced/：meta.json（口径快照）、
progress.jsonl（增量落盘）、raw_results.json（逐片留痕，含三维评审与
judge 参考）、produced/<tt>.json（产出快照，供重评审复用）、report.json（前端契约结构）、report.md（人读版）、
metrics.csv（长表 `metric,value,n`：通过率族 + 三维均分/满分率 +
新词占比），`audit/<tt>.json` 为 mtg 管线自落的评审官审计（报告 judge
段跨片聚合读它）。报告头 banner 聚合三档警示：部分运行（status 未全量
成功完成——取消/中止/失败/缩量都不可与全量横比）、同模评审、小样本
（failed/review_failed 任一占比 >10%）。历史列表只扫磁盘：
meta.status=running 且无进程内注册表项 → `interrupted`；摘要五键
（pass_rate / new_pass / hit_pass / new_ratio / three_high）直读
report.json 的 summary，缺失或截断 → null（宁缺勿假）。**与闭环评测
独立口径组不横比**；同片集下只做定性对照。

**重评审（rereview）**：`POST /runs/{id}/rereview` 复用 produced/ 快照
重新评审、重写报告，**不重跑生成**——换评审模型或阈值后想看新口径效果
时用。评审客户端设 `max_tokens=8192` 上限（镜像质量评估），防推理模型
无上限推理撞 SDK 超时；超限截断走既有二分切分。

---

## 6. 三条通用纪律

1. **`None` ≠ 0**。算不出就报"暂无"并排除在聚合外。把无样本、分母为 0、
   算不出 当作 0，是评测代码最常见的错误来源，且会让结论方向反转。
2. **换口径必须换组**。不同嵌入模型、不同 LLM、不同轮数的分数不可直接比较，
   只在自己这一组内部做前后回归。
3. **消融只改一个字段**。改两处之后无论结果如何都无法归因，实验等于白跑。