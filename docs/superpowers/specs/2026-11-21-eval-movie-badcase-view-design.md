# 闭环评测单片明细（badcase 视图）设计

日期：2026-11-21 · 状态：待批准
前置：2026-11-17-closed-loop-eval-design.md（闭环评测本体）、2026-11-21-eval-run-lifecycle-design.md（run 生命周期）

## 意图

run 跑完后做 **badcase 分析**：逐片对照「系统产出 vs GT 相关性」，定位偏在哪——
命中（产出∩相关）、噪声（产出但 GT 没有）、漏检（相关未产出）三类一眼分清。
回答的是「偏没偏、偏在哪」；「为什么偏」（提示词/响应级）数据未落盘，不在本设计。

用户已拍板：**只做终态 run**（运行中的不看——读时只碰 raw_results.json，
progress.jsonl/内存 job 一概不读）；入口在报告视图内。

## 目标

1. 报告视图新增「单片明细」：每片一行，默认**坏例优先**（失败片置顶，
   成功片按 P@K 升序，指标 null 的排最后，同分按 tt）；列可点排序。
2. 行：tt、标题、状态、Spearman、P@K、R@K、单片耗时、产出标签数、
   GT 相关标签数。
3. 展开行（懒加载）：**计分清单**（产出滤到 GT 标签空间，按系统分序，带
   sf / confidence / evidence_count / 该标签的 GT 分 / 命中标记），
   漏检清单（相关未产出，GT 分降序，带标签名），
   「GT 空间外」组（系统产出但 genome 无此标签的条目，不计指标，
   单独成组可见），失败片显示 error 原文。

## 非目标

- 运行中查看（用户明确不要）。
- prompt/响应级诊断、单片重跑（另一件事，backlog）。
- 改 report.json / raw_results.json 的落盘结构（本设计纯读侧增量）。
- 与 report 网格聚合数的逐片对账视图。

## 约束（继承红线）

- DB 零写入：全部读 eval_output_root 下的既有产物。
- 读侧容错：raw_results.json 缺失/截断 → 404「该 run 无单片明细」，不 500
  （与 report 端点同口径；运行中/写盘中途死的 run 自然落到这里）。
- 端点全部 admin-only；11 个现有端点不动，只增。
- null ≠ 0：无 relevance 交集/失败片的指标一律 null → 前端「暂无」。

## 方案取舍

- **A 读时现算（选）**：单片指标从 raw_results.json（produced + gt 内联）
  + meta.json（thetas/ks）读时计算。**历史 run 即时可用**——用户刚跑完的
  131 部不用重跑就有单片视图；落盘侧零改动。
- B 写进 report.json：assemble_report 时算好落盘。只有未来 run 受益，
  且要么把 produced/gt 搬进 report（体积翻倍）要么两端点仍要读 raw。
- C 前端下载 raw_results.json 自己算：指标口径复制到 TS，漂移风险，弃。

## 接口（router 新增 2 个，均 admin-only）

`GET /api/v1/eval/closed/runs/{run_id}/movies` → 单片摘要（包一层带实际
选档，前端表头如实显示 P@10/P@8；服务端排好序）：

```json
{"theta_used": 0.4, "k_p": 10, "k_r": 20,
 "rows": [{"tt": "tt0113277", "title": "Se7en", "ok": true, "error": null,
  "spearman": -0.12, "p_at_k": 0.3, "r_at_k": 0.15,
  "elapsed_ms": 87000, "n_produced": 32, "n_relevant": 8}]}
```

`GET /api/v1/eval/closed/runs/{run_id}/movies/{tt}` → 单片对照明细：

```json
{"tt": "tt0113277", "title": "Se7en", "ok": true, "error": null,
 "elapsed_ms": 87000, "theta_used": 0.4, "k_p": 10, "k_r": 20,
 "produced": [{"tag_id": "ml_1042", "tag_name": "悬疑反转", "sf": 0.92,
   "confidence": 0.6, "evidence_count": 3, "gt_score": 0.61, "hit": true}],
 "out_of_space": [{"tag_id": "ml_5b66", "tag_name": "霓虹美学", "sf": 0.71}],
 "misses": [{"tag_id": "ml_0644", "tag_name": "犯罪", "gt_score": 0.81}]}
```

- `produced` = **计分清单**（GT 空间内，按 sf 序，`gt_score` 为该片 gt
  原分——低于 θ 的原样展示，不在 gt 中为 null；`hit` = ≥θ）；
  `out_of_space` = 空间外条目（不计指标，带 sf 展示）；`misses` =
  relevant 未产出（库过滤后，gt 分降序）。

- run_id 守卫复用 `_run_dir`；tt 只做 JSON 行内查找（不参与任何路径拼接，
  未知 tt → 404）。
- misses 的 tag_name 经 `try_load_library` 现库 join（produced 行自带
  tag_name 不用 join）；库查不到 → null，前端显示 tag_id。

## 指标口径（读时现算；对账口径，用户已拍板）

目标：单片数与 report 网格同族——网格宏平均 ≈ 单片值的平均，全页一套数。

- **计分清单**：produced 先滤到「GT 标签空间」再取 top-K，与
  assemble_report 的样本构造同法。GT 标签空间读时重建 = raw_results
  各行 gt 键的并集（genome 稠密 → 与 run 时 tag_universe 等价）。
  空间外产出条目**不进指标、展示单独成组**（「偏门产出」照样看得见，
  但不占 top-K 坑位——网格口径本就如此剔除）。
- relevant = 该片 gt 中 **≥θ 且在当前标签库** 的标签（misses 同域过滤，
  否则会把系统永远产不出的标签当漏检——误导）。
- θ = run 的 thetas 中最接近 0.4（并列取小）；K_P = ks 中最接近 10、
  K_R 最接近 20 的档；meta 缺 thetas/ks → DEFAULT。两端点响应都回传
  实际用的 θ/K（前端如实显示，如 P@8；选档规则不复制进 TS）。
- P@K = |top-K 计分清单 ∩ relevant| / K；R@K = hits / |relevant|。
- Spearman 与 assemble_report 同式（计分清单的 sf，未产出的交集标签
  按 0 补位）——与报告页既有「每片 Spearman」同值，同页自洽。
- **null 规则（None≠0）**：失败片、无交集片全指标 null；有交集但
  θ 下相关集空的片 P@K/R@K null（对齐网格「排除出该 θ 平均」的
  规则——记 0 会把它们顶上坏例榜首、淹没真坏例），Spearman 不受
  θ 影响照算。null 排序垫底。
- 已知取舍（记档）：relevant/misses 的「当前标签库」是读时库，run 后
  库有增删时 P/R 分母与 miss 清单有微小漂移；GT 空间并集在稀疏 gt 下
  是近似（genome 稠密 → 精确）。网格数仍以 report.json 为准。

## 前端

- `ClosedEvalReport.tsx` 加「单片明细」节，新组件
  `ClosedEvalMovieTable.tsx`（报告视图已 260 行，分出去）：
  antd Table + expandable，展开时才 fetch detail；列可排序（antd sorter，
  服务端默认序为初始态）。
- 失败片行展开只显示 error 原文。
- api/eval.ts 新增两个函数与类型。

## 测试

- 服务层：指标计算单测（命中/漏检/噪声三分类、**空间外条目不进指标**、
  relevance 空片与 θ-空相关集片的 null 规则、失败片、θ/K 档位选择、
  misses 库过滤）；排序（失败置顶、P 升序、null 最后）；与
  assemble_report 网格的对账一致性（同一批样本两路过算，宏平均对上）。
- router：两端点 happy path（含包层 theta_used/k_p/k_r）、无 raw_results
  404、未知 tt 404、admin 拒非 admin、目录守卫。
- 前端：默认序渲染、展开懒加载与命中/漏检/空间外三组渲染、失败片
  error、名称缺失回退 tag_id、表头按回传档位显示。

## 留给执行者

- produced 明细的展示截断（前 N + 展开全部 vs 全量滚动）——组件内自定。
- 摘要行排序键的并列规则微调。

---

## 增补（2026-11-22 批准）：θ 可调——P/R 各一门

**意图**：badcase 分析需要在松门槛下看 P（噪声惩罚）、紧门槛下看 R（核心标签覆盖）。
用户拍板「页面可调」而非改死默认。

**接口**：两端点加可选 query 参数 `theta_p` / `theta_r`（float）：

- 缺省 = 各自最接近 0.4（即原行为，P/R 同门）；
- 校验：值必须 ∈ 该 run 的 thetas，否则 400 带合法值清单；
- 响应：`theta_used` 拆为 `theta_p` / `theta_r`；摘要 payload 增 `thetas` 数组
  （选择器选项来源）。

**口径**：p_at_k 与坏例排序用 θ_p；r_at_k 用 θ_r；**展开明细的命中标记与漏检
列表跟 θ_r**（漏检=召回侧「相关未产出」；GT 原分行内可见，别的门槛肉眼可判）；
`n_relevant` 计数跟 θ_r。null 规则按各 θ 独立判（θ_p 空相关集 → p null、
θ_r 空 → r null，各对齐各 θ 的网格排除规则）。**对账性质保持**：非空 p 均值
精确等于网格 (θ_p, K_P) 格、非空 r 均值等于 (θ_r, K_R) 格。Spearman 不受
θ 影响。K 不可调。

**前端**：单片明细节头部两个选择器（θ_P、θ_R，选项 = payload.thetas，
默认 = 服务端回传的当前值）；**切换即重拉摘要 + 清空明细缓存 + 收起展开行**
（θ_r 变更使缓存里的 hit/漏检过期）；表头字面量带门：`P@10(θ0.2)`、
`R@20(θ0.8)`。

**非目标**：报告网格、汇总五卡、历史表的 0.4 档约定不动；K 不做参数。
