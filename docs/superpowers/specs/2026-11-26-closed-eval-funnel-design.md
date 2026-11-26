# 闭环评测管线追踪（funnel trace）设计

日期：2026-11-26 · 状态：待批准 · 前置讨论：brainstorming（用户已选 A+C 方案）

## 1. 意图与动机

闭环评测跑完后，点开一部产出很少的影片，**目前无法归因**：逐片明细只有最终
produced/空间外/漏检三表，标签死在管线哪一层（召回没进？模型没提及？门控杀？
挂载失败？M 上限截断？）只活在 warning 日志的口头计数里
（`dropped_gate`/`dropped_fabricated`/`dropped_unmountable` 算完即弃）。

本设计让评测干跑**顺带把管线解剖数据结构化落盘**，读侧在单片明细透出：
漏斗计数、逐候选踪迹、漏检表的「丢失环节」交叉列、每批 LLM 原始输出文件。

用户目标原话：**事后深挖某片为什么只出这么几个标签**。

## 2. 目标 / 非目标 / 反目标

**目标**
- 单片明细可回答：该片标签在 召回→提及→门控→挂载→截断 各层各死了多少、
  **具体哪个标签死在哪层**；
- 漏检表可回答：GT 相关却未产出的标签，是**未召回**还是**召回后死于某层**；
- 每批 LLM 原始输出全文可下载（批级诊断：模型对该批的原始判定文本）。

**非目标**
- report.json 网格指标、聚合统计、历史摘要端点：零改动（只在单片明细与
  逐片摘要主表加一列）；
- 开放评测侧：零改动（open producer 仅兼容签名）；
- 运行中的实时逐片视图：另一个方向，本设计不做（trace 落盘本身是增量的，
  未来可加读端点）；
- 证据文本核验（open 侧「摘录核验」的闭环对应物）：不做，原始输出文件
  已覆盖人工核对的原材料。

**反目标**
- 采集不得改变任何打分/选择行为——trace 是纯观察者，门控/编造/挂载的
  丢弃决策一行不动（从「日志口头警告」升级为「结构化数据」，不是新增逻辑）；
- 不为历史 run 补数据（真实 LLM 干跑随机，重跑是新样本，解释不了旧 run——
  采集只能发生在 run 当时）。

## 3. 约束与既定事实

- **DB 零写入红线**：全部产物落 `eval_output_root` 下本 run 目录。
- **采集必须跑时做**：`_run_movie` 里 produce 的逐批判定/丢弃只在运行时
  存在；落盘后不可重建。
- **老 run 容错**：raw_results 行无 `trace` 键 → 读侧一律 None/「暂无」/
  404，宁缺勿崩（None≠0 纪律）。
- **生产路径零行为变化**：`execute.py` 调 produce 不传 trace，closed producer
  对 `trace=None` 走旧路径；
- **协议兼容**：`TagProducer.produce` 增可选参数后，closed/open 两个实现
  都要能接受（open 接受并忽略，保持协议调用方统一）。
- 闭环单片丢标签环节（producer.py 现状，按管线序）：
  ① 召回（候选没进 top-k）② 模型未提及（整批候选只判了一部分——
  产出少的头号嫌疑）③ 门控 A≤3 ④ 编造/越组 tag_id ⑤ 挂载失败
  ⑦ M 上限截断（全局选择每组保底 1 + Sf 降序补额）。
  整片失败（批解析两次都挂 → 抛错）是另一码事：该片 ok=false、trace 不落。
- **dir_asset 剔除不入 trace（拍板 2026-11-26）**：producer 在召回后
  过滤 dir_asset 目录（元数据/联网来源）候选，该剔除**属召回口径**——
  计划上移到召回侧直接不召回（用户确认后续会做，非本设计范围）。trace
  的候选登记在过滤后：漏斗「召回 n₁」= 过滤后候选数，misses 的
  「未召回」语义如实覆盖 asset 剔除，不设独立 outcome，docs/eval.md
  写明该口径。

## 4. 方案（讨论已定：A + C）

- **A. 逐候选踪迹落盘 + 明细视图**（核心）：producer 加可选 trace 收集器；
  每片落阶段计数 + 逐候选行；明细端点透出；前端漏斗条 + 踪迹表 + misses
  「丢失环节」列。——被否的轻量版 B（只落计数）答不了「具体哪个标签
  为什么没」，且采集钩子反正要建，增量主要是落盘粒度和一张表，不值省。
- **C. 每批原始输出落盘**（补充件）：per-movie 文件 + 下载端点，不进 UI
  解析。A 的钩子建好后几乎免费。已否：日志捞取（耦合日志格式）、重跑补采
  （随机性，语义不通）。

## 5. 设计

### 5.1 采集契约（`services/generation/trace.py` 新模块）

```python
@dataclass
class TraceCandidate:
    tag_id: str
    tag_name: str | None
    dir_id: str
    recall: float          # 召回候选自带分（candidate.score）
    batch: int             # 组内批次序（0 起）
    outcome: str | None = None    # 见 5.2，snapshot 时 None → "unmentioned"
    applicability: int | None = None  # 模型判 A（1–5）；未被提及 → None
    sf: float | None = None        # 最终 Sf；未算出 → None

class ProduceTrace:
    """被动收集器：producer 各环节写入，snapshot() 出落盘 dict。"""
    def add_candidates(self, rows: list[TraceCandidate]) -> None
    def note_batch(self, dir_id: str, batch: int, n: int,
                   parse_retried: bool, raw: str) -> None
    def note_fabricated(self, tag_id: str) -> None
    def set_max_tags(self, m: int) -> None
    def resolve(self, tag_id: str, outcome: str, *,
                applicability: int | None = None,
                sf: float | None = None) -> None
    def snapshot(self) -> dict        # → raw_results 行的 "trace" 键
    def raw_rows(self) -> list[dict]  # → llm_raw/{tt}.json 内容
```

- `snapshot()` 把 outcome 仍为 None 的候选归一成 `"unmentioned"`（未提及
  是「没发生」不是「丢失事件」，不要求 producer 显式发事件）；
- `raw_rows()` 与 `snapshot()` 分离：原始输出**不进** trace dict（尺寸纪律：
  raw_results 与 API 不背批文本）。

### 5.2 候选 outcome 枚举与批次事件

- `kept`：全局选择入选（produced）；
- `unmentioned`：候选在批里但模型没给判定；
- `gate`：A≤3 门控丢弃（resolve 带 applicability）；
- `unmountable`：挂载解析失败（全局选择前淘汰，不占名额）；
- `m_cap`：挂载成功但全局选择未选上。
- 去重保最高分不是丢失通道（tag 必存活），不发事件。
- `resolve` 重复调用（同 tag_id 跨层出现，如先入 mounted 再落 m_cap）：
  **末次为准**——终局 resolve 全部发生在全局选择之后，天然末次；快照时
  不再校验历史，读侧只信终态。
- 批次事件（`batches`）：`{dir_id, batch, n, parse_retried}`——解析重试
  一次成功的事件可见；两次都失败 → produce 抛错 → 整片失败、trace 不落
  （该片已有 error 语义，不混入部分 trace）。
- **重试批次的原始输出只存成功份（拍板 2026-11-26）**：llm_raw 的
  `content` = 该批最终成功、实际用于解析的那次输出；首次坏输出仍只活在
  warning 日志前 400 字（v7 既有行为不动）。`parse_retried` 标志讲事实、
  坏输出原文不采集。
- `fabricated`：编造/越组 tag_id 清单（非候选、不占候选表，单独计数）。

### 5.3 producer 改动（`closed/producer.py`）

- `produce(spec, session, llm, embedder, trace: ProduceTrace | None = None)`；
  `trace is None` 时零行为变化（每处采集点都形如 `if trace is not None:`）。
- 采集点：候选分批时 `add_candidates`（分批切片处顺手记 batch 序，行为
  中性重构）；每批解析后 `note_batch`；门控/编造丢弃处 `resolve`/
  `note_fabricated`；挂载失败处 `resolve("unmountable", sf=…)`；全局选择
  后对入选集 `resolve("kept", …)`、落选集 `resolve("m_cap", …)`。
  （勘误 2026-11-26 终审：实现落定为**七处**采集调用位（producer 注释
  编号 1–7：`set_max_tags` / `add_candidates` / `note_batch` / 门控
  `resolve` / `note_fabricated` / 挂载 `resolve` / 全局选择 kept 与
  m_cap 两个 `resolve` 调用位）——上列语义不变，计数以实现为准。）
- `OpenTagProducer.produce` 签名加同款可选参数，接受并忽略（协议调用方
  统一；文档注明 open 侧语义未实现）。
- `TagProducer` 协议（base.py）produce 增可选参数。`register_producer`
  的注册期校验不动（本就不查方法参数形状）。

### 5.4 eval 服务落盘（`services/eval/service.py`）

- `_run_movie`：每次尝试新建独立 sink（失败尝试的半截 trace 一律丢弃，
  只有成功尝试的 snapshot 进 `MovieResult.trace`；整片失败 → trace=None）。
- 落盘三处，与 produced 字段同款纪律：
  - **worker 线程**（不持锁，每片独立文件）：`run_dir/llm_raw/{tt}.json`
    ← `sink.raw_rows()`（`[{dir_id, batch, n, parse_retried, content}]`，
    content = 该批 LLM 原始输出全文；prompt 不落——可从 spec+库重建，尺寸
    纪律优先）；
  - **`_record_result`**（锁内）：progress.jsonl 行增 `"trace"` 键（非 None
    时）——增量落盘，崩溃不丢已完成片的解剖数据；
  - **`_write_artifacts`**：raw_results.json 行增 `"trace"` 键（读侧唯一
    正典来源）。
- 文件名守卫：tt 进文件名前过白名单正则 `^[A-Za-z0-9_-]{1,64}$`（数据集
  tt 形如 tt0068646；不过 → 该片 raw 文件不落，trace 照常）。

### 5.5 读侧纯函数（`services/eval/movie_breakdown.py` 扩展）

- `funnel_counts(trace: dict | None) -> dict | None`：从候选行派生
  `{n_recall, n_mentioned, n_kept, n_gate, n_unmountable, n_m_cap,
  n_fabricated, n_batches, parse_retries, max_tags}`——落盘只存逐候选行，
  计数一律读侧派生（单一事实源，纯函数可测）。
- `summarize_movies` 行增 `"funnel"` 键（counts dict 或 None）：主表
  「漏斗」列数据源。
- `breakdown_movie`：
  - detail 增 `"trace"` 键：`{counts, candidates, batches, fabricated,
  max_tags}`（候选行原样透传，明细端点本就逐片懒加载，载荷可控）；
  - misses 行增 `"lost_at"`：交叉派生——miss 的 tag_id **不在** trace
    候选中 → `"unrecalled"`；在 → 取其 outcome（gate/unmountable/m_cap/
    unmentioned；`kept` 出现在 miss 里属数据不自洽，防御性回 null）；
    trace 为 None（老 run/失败片）→ null。
- 分层红线不变：movie_breakdown 只用标准库。

### 5.6 API（`orchestration/eval.py` + `api/routers/eval.py`）

- `MovieSummaryRowOut` 增 `funnel: FunnelCountsOut | None`；
- `MissTagOut` 增 `lost_at: str | None`（枚举原样透传，前端做中文映射——
  与 STATUS_LABELS 同模式）；
- `MovieDetailOut` 增 `trace: MovieTraceOut | None`（候选行结构：
  `{tag_id, tag_name, dir_id, recall, batch, outcome, applicability, sf}`，
  全可空字段 None 安全）；
- 新端点 `GET /api/v1/eval/runs/{run_id}/movies/{tt}/llm-raw`（路由形态
  对齐既有 movies 明细端点，实现期以 router 现状为准）：
  - 守卫：`_run_dir` 路径守卫 → tt 白名单正则 + 该 tt 必须存在于
    raw_results 行（双重，防 tt 形态路径拼接）→ 文件存在；
  - 任一不过 → 404「该 run 无此影片的原始输出」（老 run/失败片/文件缺失
    同口径，不 500）；
  - 返回 FileResponse（文件名 `{tt}.json`）。
- 既有 files 白名单五件套**不动**（llm_raw 是 per-movie 子目录，走专用
  端点，不进白名单）。

### 5.7 前端（`ClosedEvalMovieTable.tsx` + `api/eval.ts`）

- **主表新列「漏斗」**：`召回 n₁ → 提及 n₂ → 产出 n₃`（funnel null →
  「暂无」；title 提示浮全链计数含门控/挂载/截断/编造）；
- **明细区（DetailGroups）扩展**，trace 非 null 时渲染三块，null 时渲染
  一行 secondary 说明（「本 run 早于管线追踪功能，无解剖数据」——显式
  优于神秘消失）：
  1. 漏斗条：一行统计（召回/提及/门控过/挂载成/产出 + 编造丢弃/截断/
     解析重试 secondary 计数）；
  2. 候选踪迹表：列 标签/召回分/目录/批次/A/Sf/结局 + 结局 Select 筛选
     （全部/kept/unmentioned/gate/unmountable/m_cap），服务端序（召回序）
     不重排，结局列中文映射；
  3. LLM 原始输出下载：`<a download>` 指向新端点（trace null 不渲染；
     404 → message 提示，不崩视图）；
- **漏检表加「丢失环节」列**：未召回/未提及/门控丢弃/挂载失败/上限截断/
  暂无（null）；
- `api/eval.ts` 增对应类型与 `getRunMovieRaw` 的 URL 拼装（沿用 fileUrl
  模式）；θ 门交互、代际守卫、懒加载缓存全部不动（trace 随 detail 一次
  拉回，不新增请求）。

## 6. 兼容与容错口径汇总

| 场景 | 行为 |
|---|---|
| 老 run（无 trace 键） | 主表漏斗列「暂无」；明细 trace null → 说明文案；misses lost_at「暂无」；llm-raw 404 |
| 失败片 | trace 不落（error 语义已覆盖）；llm-raw 404 |
| tt 文件名非法字符 | raw 文件不落，trace 照常，端点 404 |
| 生产生成路径 | produce 不传 trace，零行为变化 |
| 开放评测 | producer 签名兼容，语义未实现 |

## 7. 测试要点

- producer：mock LLM 下逐通道采集（门控/编造/未提及/挂载失败[scope 构造]/
  m_cap[max_tags 调小]/批解析重试/去重保最高不发事件）；trace=None 时
  输出与旧版逐字节一致（行为中性）；
- eval service：三处落盘形状（progress 行/raw 行/llm_raw 文件）；失败尝试
  的 sink 丢弃（重试后成功只留末次）；tt 非法字符不落 raw 文件；
- movie_breakdown：funnel_counts 派生对账（各通道计数=手工构造候选行）；
  lost_at 交叉四象限（未召回/死于各层/老 run null/不自洽防御 null）；
- API：响应模型新键 None 安全；llm-raw 端点守卫（路径越界/tt 注入/
  tt 不在 run/文件缺失全 404 不 500）；
- 前端：漏斗条/踪迹表筛选/misses 列渲染、trace null 的说明文案、主表
  「暂无」、下载 404 提示。

## 8. 任务切分草案（供 writing-plans 细化）

1. `trace.py` 采集器 + closed producer 采集点 + open producer 签名兼容
   （含协议与既有测试回归）；
2. eval service 落盘三处 + MovieResult 扩展；
3. movie_breakdown 读侧派生（funnel_counts + lost_at）+ summarize/
   breakdown 扩展；
4. API 响应模型扩展 + llm-raw 端点（守卫与容错）；
5. 前端类型 + 漏斗条/踪迹表/misses 列/主表列/下载；
6. docs/eval.md 增「管线追踪」一节（口径与暂无纪律；含「未召回」语义
   覆盖 dir_asset 剔除、重试批次原始输出只存成功份两条拍板口径）。

## 9. 留给实现者

- 批次序的记录方式（分批切片处顺手记 vs 事后回填）——行为中性前提下自选；
- 漏斗条的具体排版（Typography 统计行 vs antd Steps）——倾向前者（轻、
  与现有明细三表的朴素风格一致）；
- llm_raw 文件 JSON 形状微调（增删 prompt_len 之类的辅助键）。
