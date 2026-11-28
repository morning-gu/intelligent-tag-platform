# 开放评测评审归一设计：一把尺子、一个引擎、裁决复算

日期：2026-11-28 ｜ 状态：待用户确认 ｜ 分支：feat/open-eval-review-unification（自 origin/main @ 1378e6a）

## 意图与动机

开放评测一次 run 里，同一部片的标签被评审两次：管线内**生产 Judge**（过滤闸，
`mtg/judge.py` + `JUDGE_TEMPLATE`）与 run 后**整片独立评审**（`services/eval/open_review.py`
+ `REVIEW_PROMPT`，现 v13）。两处已共用统一三维评分标准
（`capabilities/prompts/_evaluation.py`），但**判定方式、证据核验、输出协议、解析与
重试实现**各自一套，造成三个已确认的痛点：

1. **数字不可比**：报告「评审通过率」（模型自主 pass，最终产出群体）与漏斗
   「judge 通过率」（程序阈值，合并候选群体）各说各话；
2. **维护成本**：两套提示词协议、解析器（`parse_judge_items` vs `_parse_reviews`）、
   重试/拆批/预算逻辑要同步维护；
3. **结论矛盾**：judge 放行而评审不保留（或分数相悖）时无法归因——是模型差、
   上下文差、证据核验口径差还是裁决规则差。

**拍板（2026-11-28 对话）**：走方案一——一把尺子、一个引擎，**裁决统一到生产
规则**；输出结构融合 judge 与评审两边字段；评审模型后续换独立模型（本轮不改默认
回落机制，同模警示保留）；评审样本维持 judge 通过者（不扩到被淘汰候选）。

## 现状对照（main @ 1378e6a）

| | 生产 Judge | 开放评审 |
|---|---|---|
| 角色 | 管线过滤闸（小节 4） | 独立复测（评测头条指标） |
| 评审单位 | 目录内候选批次 ≤20 | 整片全标签一次 + 「其他标签」上下文块 |
| 样本 | 合并候选（过滤前，含被淘汰） | 最终产出（过滤后幸存者） |
| 输出 | `verdicts`：裸分 + confidence + quote + scenes + 一句 reason | `reviews`：每维 {score,reason} + value 三方向带理由 + pass + 总评 reason + evidence_scene_ids |
| 通过判定 | 程序阈值 `rel≥3/sem≥3/val≥2`（`_JUDGE_SCORE_FLOORS`） | 模型自主 pass（含组内冗余、独立信息贡献） |
| 证据核验 | quote 逐字核验（失败→relevance 压 1 淘汰）；scenes 计数仅提示词约束 | 场景 ID 计数（不足→relevance 置 None 出分母）+ 理由文本挖 ID 兜底 |
| null 后果 | relevance/semantics null → 淘汰；value 方向 null → 协议错误重试 | null → 无法判断，不进分母（None≠0）；value 方向已不收 null（v13） |
| 模型 | `mtg_judge_model`（deepseek-v4.1-flash） | `open_eval_review_model` 默认空 → 回落主模型（与生成同模） |

## 方案（对话中比较过的四条，已拍板）

1. **一把尺子 + 一个引擎，裁决统一到生产规则（选定）**：评测从「独立意见」变成
   「独立打分员」——不再质疑规则本身，只测执行偏差；数字与生产同口径。
2. 尺子+引擎归一但保留两种裁决、报告双口径（未选：头条指标语义不变，报告更复杂）。
3. 反向归一：生产 judge 改模型 pass（未选：动生产闸门确定性与可审计性，推翻
   2026-11-23「生产 judge 继续使用自身评分筛选规则」拍板）。
4. 取消独立评审调用、报告直接复算 judge 分（未选：评测变纯自评，gate 错杀/放水
   不可测，研究效度不可接受）。

## 目标

1. 两处评审共用**同一输出 schema、同一解析器、同一重试/拆批/预算引擎**（策略参数化）；
2. 评审通过判定改为**程序阈值复算**（与生产同一条规则、同一个函数）；
3. 报告新增**同群体一致性对照**：judge 放行条目上的评审复算分歧率与逐维分差；
4. 评审请求瘦身：pass / 总评 reason / 「其他标签（上下文）」块随裁决退役一并删除。

## 非目标

- 不改三下限数值（`relevance≥3 / semantics≥3 / value≥2`）与聚类/配额/挂载等
  其余生产环节；
- 不把评审样本扩到 judge 淘汰候选（混淆矩阵另一半留后续）；
- 不在本轮拍板 judge 同模自评（待办另议）；评审模型默认回落主模型的机制与
  `same_model` 警示保留，部署侧换模型；
- 不动闭环评测与 verify 相似度复核；
- 不动 v13 已拍板的评审协议收紧（value 方向 0–4 整数不收 null、两步评分顺序）。

## 反目标（做砸了什么样）

- 生产 Judge 过滤率无解释地大幅漂移（新增程序化核验导致，但审计/文档没说清）；
- 评测口径静默变化：新旧 run 的通过率被直接横比（违反通用纪律 2「换口径必须换组」）；
- 引擎抽象过度：judge 的两级名称回配、评审的整片拆批被硬塞进失真的公共接口。

## 统一设计

### 1. 融合输出 schema（两处唯一协议）

模型只输出评分与证据，**不输出任何裁决字段**（pass 由程序复算）：

```json
{"verdicts": [{
  "id": "t0",
  "relevance": {"score": 4, "reason": "简短理由"},
  "semantics": {"score": 3, "reason": "简短理由"},
  "value": {"presentation": {"score": 3, "reason": "理由"},
            "search": {"score": 4, "reason": "理由"},
            "recommendation": {"score": 2, "reason": "理由"}},
  "quote": "10–25 字逐字原文摘录",
  "scenes": ["scene_001", "scene_007"],
  "confidence": 0.85
}]}
```

字段来源与两侧用法：

| 字段 | 来源 | 生产用法 | 评测用法 |
|---|---|---|---|
| `id` | 融合（judge 的 candidate_id / 评审的 tN） | 候选回配 | 行号回配 |
| 每维 `{score, reason}` | 评审形状（judge 新增逐维 reason） | 审计展示 | 报告明细 |
| `value` 三方向带理由 | 评审形状 | 聚合出总分 | 聚合 + 三方向明细 |
| `quote` | judge（评审新增） | 证据核验 | 证据核验 + 明细展示 |
| `scenes` | judge 命名（评审的 `evidence_scene_ids` 为容忍别名） | 证据核验 | 证据核验（报告层继续以 `evidence_scene_ids` 键透出，前端不动） |
| `confidence` | judge（评审新增） | 审核队列排序 | 一致性分析加权 |
| ~~`pass` / 总评 `reason`~~ | 评审（退役） | — | 程序复算 |

null 语义沿用 v13 拍板：relevance / semantics 允许显式 null（无法判断）；
value 三方向 0–4 整数，null/漏字段/非法 = 协议错误（重试），不与「无法判断」混同。

### 2. 证据核验与裁决复算（共享规则、共享函数）

**证据核验**（引擎后处理，relevance ≥ 3 的判决）：

1. `quote` 非空且在核验文本（`render_content(content, modalities)` 归一化后）中
   逐字可寻；excerpt/scene 级按生产现状语义限制在来源场景文本内核验；
2. `scenes` 计数：film 级 ≥ 2 个互异有效场景；scene 级 ≥ 1 个且 ∈ 该标签来源场景
   （评审侧现行的「允许场景交集」规则保留）；
3. 任一不满足 → **relevance 压 1**（沿用生产 `_require_quotes` 语义），reason 改写
   为核验失败说明。评审侧原「置 None 出分母」退役；「理由文本挖场景 ID」兜底
   （`_scene_mentions`）删除——quote 即逐字证据；
4. **生产侧由此新增程序化 scenes 计数核验**（原先仅提示词约束）：生产过滤会
   收紧，属有意变更，须在文档与审计淘汰原因中可见。

**裁决复算**（共享函数，两侧同源）：

- 三下限常量挪到共享位置，`verdict_pass(relevance, semantics, value) -> bool | None`：
  任一轴 null → None（评测不进分母；生产按未过线淘汰，与现状 null 淘汰一致）；
- 生产 `_filter_candidates` 与评测报告 `_group_stats` 都调它——通过率数字同源。

### 3. 共享评审引擎（一个模块，策略参数化）

新模块（落点候选：`services/generation/open/verdict_engine.py`，或沿 `value_scoring`
先例放 `services/tags/`——计划定；不得放 capabilities，因需 import services）：

- **单批编排**：发消息 → 融合解析 → id/名称两级回配（judge 的 `normalize_key`
  精确→唯一前缀匹配；评审的 tN 行号）→ 漏判处理；
- **整批格式失败重试一次**（共用 retry hint）；
- **缺漏策略**：评审侧拆批（budget `2n+2`、子批后单条复试）；生产侧不拆
  （批 ≤20 固定、salvage 部分判决、漏判淘汰）→ 以策略参数区分
  （`split_on_missing` / `budget` / `salvage`）；
- **判决后处理**：证据核验（上节）、semantics 缺释义规则（现散在
  `open_review.py` 200–241 行）、value 聚合（`aggregate_value_score`）；
- trace / 审计记录形状统一（`batches` / `raw` / `errors` 键族）。

**解析器融合**：`parse_judge_items` 与 `_parse_reviews` 合并为一个 `parse_verdicts`，
容错家族取并集（围栏剥除、首尾大括号抠对象、`verdicts`/`reviews`/裸列表、
id 行号容忍、重复 id 剔除、salvage 合格条目、协议错误与显式 null 区分）。

### 4. 提示词融合与版本

- `_evaluation.py` 继续是三维标准唯一权威源；新增共享「输出字段要求 + 输出格式
  块 + retry hint」（同驻该模块或新常量模块）；
- 两处模板各自保留 header 与材料区差异（judge：候选行/影片内容/目录定义；
  评审：整片材料 + 待评估标签表），共用输出协议块；
- 评审模板的「两步走 + 组内比较 + pass」段删除，**「该片其他标签（上下文）」
  请求块一并删除**（拆批时也不再携带，进一步省 token）；
- 版本 bump：`REVIEW_PROMPT_VERSION` → open-review-v14；`MTG_VERSION` →
  mtg-v15（后缀名计划定）。meta 照常记录。

### 5. 报告与前端

- review 行新形状：三维 {score,reason} + value 聚合（含 tag_kind/weights/三方向
  明细，沿用现键）+ `quote` + `evidence_scene_ids` + `confidence` + `evaluated`；
  顶层 `pass` 键保留但语义 = **复算判定**（前端键不动）；
- `_group_stats`：`passed = sum(pass is True)`、`judged = sum(pass is not None)`
  ——分母语义自动从「模型有结论」变为「复算可判」；
- **新增一致性对照**（回答痛点 3）：judge 放行条目上 judge vs 评审的
  verdict 分歧率、逐维均差、完全一致率——进 summary 与 metrics.csv；
- `meta.review.pass_mode`："model" → "threshold"；
- `OPEN_REPORT_SCHEMA_VERSION` 3 → 4；lazy upgrade 沿用：旧 run 的 raw review
  行按存储分数复算出新 pass（旧「证据不足置 None」行在新规则下复算为 None 出
  分母——**口径近似**，banner/warning 标注 legacy 复算口径，新旧数字不可横比）；
- 前端：「通过」列文案改为阈值判定语义、明细可选加 quote 列（小改）；
- rereview 端点不变（材料快照重评走新协议）。

### 6. mock 与测试

- mock `_quality_output`：judge/review 两模式统一输出融合形状（review 模式不再
  返回 pass）——分流标志串维持现状（`## 审核任务` / `## 标签质量评估`）；
- 测试面：`test_generation_open`（判决形状/过滤收紧）、`test_eval_open_review`
  （解析/拆批/预算/证据核验新规则）、`test_open_eval`（编排）、`test_api_eval`、
  前端 `OpenEvalReport.test.tsx`；`quality_metrics` 输入形状不变（三维
  {score,reason} 键族未动）。

### 7. 兼容与版本

- 两个 prompt version bump，meta 记录；
- 生产 audit `verdicts` 行新增逐维 reason 键（读侧按键存在性容忍旧审计）；
- 旧 run 报告：schema 4 lazy upgrade（见上）；历史列表口径标注。

## 口径变更声明（报告/文档必须写明的四条）

1. 评审通过率 = 生产规则阈值复算（不再是模型保留意见）——头条指标语义变化；
2. 证据不足：评审侧 relevance 由「置 None 出分母」改为「压 1」（对齐生产）；
3. 生产侧新增程序化 scenes 计数核验——**生产过滤收紧**（合并前后过滤率对比
   须在上线验证中量化）；
4. 新旧 run 通过率不可横比（通用纪律 2），legacy 复算口径随报告标注。

## 我做的三个小决定（可否决）

1. **生产 judge 也逐维输出 reason**：多花输出 token，换审计丰富度与协议完全对称
   （也使 mock/解析只需一种形状）；
2. **评审 pass、总评 reason、组内比较指令、「其他标签」上下文块全部退役**：
   裁决复算的自然结果，评审请求显著瘦身；组内冗余信号就此放弃（生产由 verify
   相似度复核兜底）；
3. **评审也输出 confidence**：judge/评审两侧都有逐条把握度，一致性分析可加权；
   解析容忍缺失。

## 风险与开放问题

- **生产过滤收紧幅度未知**（新增 scenes 计数核验）：计划中应包含对既有 run 的
  audit verdicts 做离线复算，量化新旧过滤率差再上线；
- 整片评审输出变大（quote + confidence + 逐维 reason）：`max_tokens=8192` 与拆批
  兜底已覆盖，但要观察整批成功率变化；
- 引擎抽象的失真风险（反目标 3）：以「策略参数 + 两侧现有语义不变」为验收线。

## 留给实现的

- 引擎模块落点与函数签名细节；
- parser 容错家族并集的具体取舍与测试矩阵；
- 报告一致性对照段的精确字段与 CSV 行；
- mock 确定性采样细节；
- 测试改造清单与前端文案。
