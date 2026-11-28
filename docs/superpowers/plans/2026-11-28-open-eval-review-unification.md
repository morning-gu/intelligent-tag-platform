# 开放评测评审归一 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 生产 Judge 与开放评测评审合并为一把尺子（融合输出 schema）、一个引擎（共享解析/重试/拆批/证据核验），通过判定统一为程序阈值复算。

**Architecture:** 新共享模块 `services/generation/open/mtg/verdicts.py` 承载判决域（楼层/复算/目标）、融合解析器、证据核验终判与批编排引擎；`mtg/judge.py` 与 `eval/open_review.py` 重写为引擎的两个薄调用方（策略参数不同）；报告层在装配时派生通过判定并新增 judge↔评审一致性对照。

**Tech Stack:** Python 3.11 / pytest（纯标准库+pydantic）；React + Vitest（前端）。

**Spec:** `docs/superpowers/specs/2026-11-28-open-eval-review-unification-design.md`（已批准；本计划的设计依据，执行者须同读）

## Global Constraints

- 分层红线：`capabilities/` 不得 import `services/`；`services/eval/` 不得 import fastapi；`verdicts.py` 落 `services/generation/open/mtg/`（eval → generation import 已有先例 `open_review.py` import `mtg.prompts`）。
- 楼层常量值逐字：`{"relevance": 3, "semantics": 3, "value": 2}`。
- 调用预算：`2 * len(targets) + 2`（两测同款）。
- 版本号逐字：`REVIEW_PROMPT_VERSION = "open-review-v14"`；`MTG_VERSION = "mtg-v15-unified-verdict"`；`OPEN_REPORT_SCHEMA_VERSION = 4`；`meta.review.pass_mode = "threshold"`。
- null 纪律（None≠0）：relevance/semantics 显式 null = 无法判断；value 三方向只收 0–4 整数（null/漏字段/非法 = 协议错误重试，绝不与「无法判断」混同）。
- 证据核验（已拍板）：只认判决自引 quote + scenes，不回落生成侧场景；quote ≥8 字连续重合（`_QUOTE_MIN_CHARS=8`，归一化后）；film 级 scenes ≥2、scene 级 ≥1 且 ∈ 来源场景；失败 → relevance 压 1。
- 淘汰原因（已拍板）：压分不过线优先专属原因——verify_fail=="scenes" 且无有效引用场景 → `_DROP_NO_EVIDENCE`；=="scenes" 且 film<2 → `_DROP_SINGLE_SCENE`；=="quote" → 新常量 `_DROP_QUOTE_FAIL`（文案 `"高相关分无逐字摘录支撑"`）；belt 两闸（`_evidence_for` 后的空证据/单场检查）保留；`is_filter_stage_drop` 精确匹配元组加 `_DROP_QUOTE_FAIL`。
- mock 分流标志串不得改：`## 审核任务`、`## 标签质量评估`、`## 开放评测补充规则`、`## 待评估标签`（`\n\n## 待评估标签\n` 分隔）。
- 评审行 report.json 键兼容：`pass`（语义改派生）、`relevance/semantics/value`（嵌套 {score,reason}）、`reason`、`evidence_scene_ids`、`evaluated` 保留，新增 `quote`、`confidence`。
- 生产 audit `verdicts` 行保持扁平键（relevance/semantics/value/value_scores/confidence/scenes/quote/reason/tag/candidate_id），新增 `*_reason` 与 `verify_fail` 键——`open_report._judge_score_summary` 与前端 `JudgeVerdict` 不破。

## Review Focus

1. **模型回 id 变体**（`"t3"` / `3` / `"3"`，或把整行候选抄进 `tag`）→ 必须回配上目标不丢判决（`VerdictTarget.id_aliases` + name 兜底）→ Task 4 引擎回配测试。
2. **重复 id 的判决条目** → 整条按不可靠剔除（镜像现行评审规则，不是首个生效）→ Task 2 解析器测试。
3. **输出截断（finish_reason=length）** → 已判条目保留、漏判拆批，预算耗尽显式记错误 → Task 4 引擎截断/预算测试。
4. **旧 run 材料**（旧 review 行带模型 pass、旧 judge_trace/raw 形状、schema 3 报告）→ 报告复算与读侧容错不崩、legacy 口径警示出现 → Task 9 报告测试 + Task 10 前端 legacy 夹具测试。
5. **relevance ≥3 但 quote 为空串或 <8 字** → 压 1 而不是放行或置 null → Task 3 核验测试。

---

### Task 1: 判决域常量与复算函数（`verdicts.py` 起步）

**Files:**
- Create: `src/tagplatform/services/generation/open/mtg/verdicts.py`
- Test: `tests/test_mtg_verdicts.py`

**Interfaces:**
- Produces: `VERDICT_FLOORS: dict[str, int]`；`verdict_pass(relevance, semantics, value) -> bool | None`；`VerdictTarget`（后续任务的公共类型）。

- [ ] **Step 1: 写失败测试**

```python
from tagplatform.services.generation.open.mtg.verdicts import (
    VERDICT_FLOORS, VerdictTarget, verdict_pass)

def test_floors_values():
    assert VERDICT_FLOORS == {"relevance": 3, "semantics": 3, "value": 2}

@pytest.mark.parametrize("rel,sem,val,expected", [
    (4, 3, 3.0, True), (3, 3, 2.0, True),
    (2, 4, 3.0, False), (4, 2, 3.0, False), (4, 3, 1.99, False),
    (None, 3, 3.0, None), (4, None, 3.0, None), (4, 3, None, None),
])
def test_verdict_pass(rel, sem, val, expected):
    assert verdict_pass(rel, sem, val) == expected

def test_verdict_target_fields():
    t = VerdictTarget(key="t0", tag="逃离安排", level="film",
                      allowed_scenes=frozenset({"scene_001"}),
                      tag_kind="content", has_desc=True)
    assert t.key == "t0" and t.level == "film"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_mtg_verdicts.py -v`
Expected: FAIL（ModuleNotFoundError）

- [ ] **Step 3: 实现 `verdicts.py` 域段**

`verdict_pass`：三轴任一 None → None；否则 `relevance>=3 and semantics>=3 and value>=2`。`VerdictTarget` 用 `@dataclass(frozen=True)`，字段：`key: str`、`tag: str`、`level: str`（"film"|"scene"）、`allowed_scenes: frozenset[str]`、`tag_kind: str`、`has_desc: bool`、`id_aliases: tuple[str, ...] = ()`。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_mtg_verdicts.py -v`  Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/tagplatform/services/generation/open/mtg/verdicts.py tests/test_mtg_verdicts.py
git commit -m "feat: 判决域起步——VERDICT_FLOORS/verdict_pass/VerdictTarget（评审归一 Task 1）"
```

### Task 2: 融合解析器 `parse_verdicts`

**Files:**
- Modify: `src/tagplatform/services/generation/open/mtg/verdicts.py`
- Test: `tests/test_mtg_verdicts.py`（追加解析器节）
- Reference: 容错家族并集的来源——`mtg/parse.py:parse_judge_items`（围栏/salvage/协议错误）与 `eval/open_review.py:_parse_reviews`（reviews 键/按 id 索引对象/裸列表/重复 id 剔除/显式 null）。

**Interfaces:**
- Produces: `parse_verdicts(text: str) -> list[dict]`；条目键：`id: str | None`（字符串化）、`tag: str`（`_clean_tag` 同款截断，可空）、`relevance/semantics: {"score": int | None, "reason": str}`、`value: {"presentation": {"score": int, "reason": str}, "search": ..., "recommendation": ...}`、`quote: str`（≤60 字）、`scenes: list[str]`、`confidence: float | None`。违协议 → `MTGParseError(message, items=合格条目)`（从 `mtg.parse` import）。
- Consumes: `MTGParseError`、`VALUE_DIRECTION_ALIASES/VALUE_DIRECTIONS`（`services/tags/value_scoring`）。

- [ ] **Step 1: 写失败测试（每条一个用例）**

覆盖并断言（用 `_text(obj)` = `json.dumps` 包 `{"verdicts": [...]}`）：
- 融合形状（id + 每维 {score,reason} + value 三方向 {score,reason} + quote/scenes/confidence）→ 全字段解析正确；
- judge 旧形状（tag + candidate_id + 裸分 + `value_scores` 裸整数）→ 等价解析（id 取 candidate_id，reason 为 ""）；
- 包装键族：`{"reviews": [...]}`、`{"tags": [...]}`、裸列表、`{"reviews": {"t0": {...}}}`（按 id 索引对象展平）都可用；前后 prose + ```json 围栏剥除；
- relevance 显式 null（`{"score": null}` 与裸 `null`）→ score None；
- value 方向 null / 漏方向 / 分值 5 / 布尔 → 该条目进 `MTGParseError.items` 之外（整条协议错误， salvage 其余）；
- **重复 id 两条 → 两条都被剔除**（不是首条生效）；
- 中文方向键（`{"内容呈现": 3, ...}`）→ 归一到三方向；
- `scenes` 非列表 → 协议错误；缺 scenes 键 → `[]`；缺 quote → `""`；缺 confidence → None。

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_mtg_verdicts.py -v`  Expected: 新用例 FAIL

- [ ] **Step 3: 实现 `parse_verdicts`**

复用 `mtg.parse` 的 `extract_json_array`（import，不改 parse.py——`_find_items` 已找嵌套列表，先直接 `json.loads` 再走 `extract_json_array` 兜底围栏/prose；包装 dict 展平逻辑在 verdicts 内做）。分值清洗复刻 `_clean_score` 语义（哨兵 `_MISSING` 区分协议错误与 null），每维接受裸值或 `{score,reason}`；directions 归一用 `VALUE_DIRECTION_ALIASES`。

- [ ] **Step 4: 跑测试确认通过**（含 Task 1 用例）

Run: `python -m pytest tests/test_mtg_verdicts.py -v`  Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/tagplatform/services/generation/open/mtg/verdicts.py tests/test_mtg_verdicts.py
git commit -m "feat: 融合解析器 parse_verdicts——两套容错家族取并集（评审归一 Task 2）"
```

### Task 3: 证据核验与终判 `finalize_verdict`

**Files:**
- Modify: `src/tagplatform/services/generation/open/mtg/verdicts.py`
- Test: `tests/test_mtg_verdicts.py`（追加核验节）
- Reference: 迁移来源——`pipeline.py:_require_quotes/_quote_ok/_normalize_quote/_QUOTE_MIN_CHARS`、`eval/open_review.py` 200–241 行后处理、`_summarize_value_reasons`。

**Interfaces:**
- Produces: `finalize_verdict(item: dict, target: VerdictTarget, *, haystack: str, scene_texts: Mapping[str, str], value_weights: Mapping | None = None, semantics_null_without_desc: bool = False) -> dict`。返回键：`relevance/semantics: {"score", "reason"}`、`value: {"score": float, "reason": str, "tag_kind", "weights", "dimensions"}`、`quote: str`、`scenes: list[str]`（核验后有效集，排序）、`confidence: float | None`、`reason: str`（派生顶层理由）、`verify_fail: None | "quote" | "scenes"`。
- Consumes: `aggregate_value_score / weights_dict_for_kind / normalize_tag_kind / VALUE_*`（value_scoring）。
- 同时迁入模块级：`_normalize_quote / _quote_ok`（从 pipeline 原样搬移，pipeline 后续 import 回来）。

- [ ] **Step 1: 写失败测试**

用例断言（haystack/scene_texts 用固定中文句子构造）：
- relevance=4、quote 在 haystack 归一化后可寻、film 级 scenes 两个有效 → `verify_fail` None、分数原样、value 聚合保留两位（`aggregate_value_score` 同参对拍）；
- **quote 空串 / 8 字以下 / 全文找不到**（relevance≥3）→ relevance 压 1、`verify_fail=="quote"`、`relevance.reason` 含「未引用可核对原文」、顶层 `reason` 为核验失败说明；
- film 级只引 1 个有效场景（relevance≥3）→ 压 1、`verify_fail=="scenes"`；
- scene 级引用不在 `allowed_scenes` 内的场景 → 不计数 → 压 1；scene 级 `allowed_scenes` 为空集 → 核验退回全文 haystack（quote 可寻则不因 scenes 压分）；
- scene 级 quote 必须落在 allowed 场景文本内（全文有、场景内没有 → 压 1）；
- `semantics_null_without_desc=True` 且 `has_desc=False` 且 semantics 分 ≥2 → semantics 置 `{"score": None, "reason": "缺少释义"}`；同参 False → 原样保留；
- 派生 reason：无压分时 = 各维 reason 拼接（relevance/semantics + `_summarize_value_reasons` 同款 value 三方向汇总）。

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_mtg_verdicts.py -v`  Expected: 新用例 FAIL

- [ ] **Step 3: 实现 `finalize_verdict`**

规则序：① scenes 求有效集（`item.scenes ∩ (全部场景键)`，scene 级再 ∩ allowed）；② relevance≥3 时核验 quote（源 = allowed 的 `scene_texts` 值列表，空则 `[haystack]`）与计数（film≥2 / scene≥1），任一失败压 1 并写 `relevance.reason` + `verify_fail`；③ semantics 缺释义规则（仅 flag 开）；④ value 三方向聚合（`aggregate_value_score`，weights 参数）；⑤ 派生顶层 reason。压分只在 relevance 为 int 且 ≥3 时发生（None 不压）。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_mtg_verdicts.py -v`  Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/tagplatform/services/generation/open/mtg/verdicts.py tests/test_mtg_verdicts.py
git commit -m "feat: finalize_verdict——统一证据核验(quote+场景计数/压1)与价值聚合（评审归一 Task 3）"
```

### Task 4: 批编排引擎 `run_verdicts`

**Files:**
- Modify: `src/tagplatform/services/generation/open/mtg/verdicts.py`
- Test: `tests/test_mtg_verdicts.py`（追加引擎节；本地 `Recorder` 替身，契约 `complete(messages, run_id) -> str | LLMResponse`）

**Interfaces:**
- Produces:
  - `VerdictPolicy(max_batch_size: int | None = None, split_on_missing: bool = False, defer_single_retries: bool = False)`（frozen dataclass；预算恒 `2*len(targets)+2`）
  - `run_verdicts(client, targets: list[VerdictTarget], *, build_messages: Callable[[list[VerdictTarget], int], list[dict]], run_id: str, policy: VerdictPolicy, haystack: str, scene_texts: Mapping[str, str] | None = None, value_weights=None, semantics_null_without_desc: bool = False, match_by_name: bool = False, trace: dict | None = None, usage: dict | None = None, request_slot=None) -> tuple[dict[str, dict], dict[str, str]]`（(按 target.key 的终判 dict, 错误 dict)）
  - trace 键族（两测统一）：`{"batches": [{"batch": str, "attempt": int, "targets": [key...], "raw": str, "error": str | None, "missing": [key...]}], "errors": {key: msg}, "salvaged: int}`
- Consumes: Task 1–3 全部；`normalize_key`（`mtg.normalization`）做 name 兜底；`accumulate_usage`（`capabilities/llm/usage`）。
- 引擎统一响应处理：`str` 替身按成功；`LLMResponse.ok` False → 调用失败错误；`finish_reason=="length"` → 截断（走拆批）。

- [ ] **Step 1: 写失败测试（Recorder 替身按调用次数返回不同响应）**

用例（policy 变体 + 断言）：
- 全批一次成功（整批 policy）→ 每目标一条终判、errors 空、trace.batches 长度 1、`batch`/`targets` 键正确；
- **id 变体回配**：响应 id 回 `"3"`/`3`/`"t3"`（target key `"t3"`、`id_aliases=("3",)`）三种都回配上；回 `tag` 不回 id 且 `match_by_name=True` 时按 `normalize_key` 精确→唯一前缀兜底配对；
- 整批格式失败（非 JSON）→ 同批 attempt 1 重试一次（retry hint 已由 build_messages 附加）后成功；
- 部分成功 + `split_on_missing=True` → 漏判子集另起一批（已判不重发）、子批再失败且 >1 条 → 对半拆两批；单条 → 进 `defer_single_retries` 单条复试队列（在所有子批之后才发出）；单条复试失败 → errors 有该 key；
- 部分成功 + `split_on_missing=False`（judge 策略，`max_batch_size=2` 分批）→ 漏判重试一轮后记 errors、已判保留、`trace.salvaged` 计数正确；
- **截断**：`LLMResponse(raw={"choices":[{"finish_reason":"length"}]})` → 已解析条目保留 + 漏判走拆批；
- **预算耗尽**：Recorder 每次都失败、目标 3 条（预算 8）→ 消耗完后剩余目标直接进 errors（错误串含「预算耗尽」），不再发请求（Recorder 调用次数断言 ≤ 预算）；
- 调用抛异常 → 该批目标进 errors（错误串含「评审调用失败」或异常文本），其余批继续；
- `usage` 传入 dict → `accumulate_usage` 被累计（mock LLMResponse 带 usage）。

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_mtg_verdicts.py -v`  Expected: 新用例 FAIL

- [ ] **Step 3: 实现 `run_verdicts`**

控制流（队列驱动）：初始按 `max_batch_size` 分批（None = 整批一队）；每批 attempt 0 调用→`parse_verdicts`→回配（id 精确/别名 → `match_by_name` 时名称两级）→ 全配齐即完成；有漏判且 attempt 0 且**零命中** → attempt 1 整批重试；仍有漏判：有命中 → 漏判子集重新入队（front）；零命中且 `split_on_missing` 且 >1 → 对半两批入队（front）；单条 → `defer_single_retries` 时追加队尾单条复试（一次机会），否则记 errors；`split_on_missing=False` → 漏判直接记 errors。每次调用前查预算（含单条复试）。回配重复 key 以首条为准（setdefault）。终判对每个命中目标调 `finalize_verdict`。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_mtg_verdicts.py -v`  Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/tagplatform/services/generation/open/mtg/verdicts.py tests/test_mtg_verdicts.py
git commit -m "feat: run_verdicts 批编排引擎——策略参数化/预算/统一 trace 键族（评审归一 Task 4）"
```

### Task 5: 提示词融合与版本

**Files:**
- Modify: `src/tagplatform/capabilities/prompts/_evaluation.py`（新增共享输出块）
- Modify: `src/tagplatform/capabilities/prompts/mtg.py`（JUDGE_TEMPLATE 输出段换共享块）
- Modify: `src/tagplatform/capabilities/prompts/open_review.py`（REVIEW_PROMPT v14 重写协议附注）
- Test: `tests/test_prompts_registry.py`（或现有 registry 完备性测试所在文件——先 Glob 确认）与 `tests/test_mtg_judge.py` 的模板断言

**Interfaces:**
- Produces: `_evaluation.VERDICT_OUTPUT_SPEC: str`（共享输出字段要求正文）、`_evaluation.VERDICT_RETRY_HINT: str`；`mtg.JUDGE_RETRY_HINT = VERDICT_RETRY_HINT`（保留常量名，registry 名单不动）；`open_review.REVIEW_PROMPT_VERSION = "open-review-v14"`、`REVIEW_RETRY_HINT = VERDICT_RETRY_HINT`。
- 输出示例 JSON（设计 §1 逐字）：`{"verdicts":[{"id":"t0","relevance":{"score":4,"reason":"…"},"semantics":{"score":3,"reason":"…"},"value":{"presentation":{"score":3,"reason":"…"},"search":{"score":4,"reason":"…"},"recommendation":{"score":2,"reason":"…"}},"quote":"10–25 字逐字原文摘录","scenes":["scene_001","scene_007"],"confidence":0.85}]}`。

- [ ] **Step 1: 写失败测试**

- `REVIEW_PROMPT_VERSION == "open-review-v14"`；`REVIEW_PROMPT` 不含「pass」「该片其他标签」「独立保留」「总评」字样；含 `VERDICT_OUTPUT_SPEC` 全文与「## 开放评测补充规则」节标题、quote 要求（「relevance 达到 3 或 4 分时」给出逐字摘录）；
- `JUDGE_TEMPLATE` 含 `VERDICT_OUTPUT_SPEC`；不再含 `value_scores` 字段名（输出段）；judge 输出示例为融合形状（id 键）；
- 两模板都含 `_evaluation.UNIFIED_EVALUATION_CRITERIA` 全文（现状保持）；
- registry 完备性测试（现有）仍 PASS——`JUDGE_RETRY_HINT/REVIEW_RETRY_HINT` 常量名保留。

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_mtg_judge.py -v`（先 Glob `tests/` 确认 registry 完备性测试文件名，一并跑）
Expected: FAIL（版本/内容断言）

- [ ] **Step 3: 实现三处提示词**

`VERDICT_OUTPUT_SPEC`：字段要求列表（id 原样返回；每维 {score 0–4 整数或 null, reason 简短理由}，仅 relevance/semantics 允许 null 并写明缺失信息；value 三方向 {score 0–4 整数, reason}；quote 10–25 字连续逐字摘录、找不到可引用原文时空串，relevance≥3 必须有；scenes 取真实场景 id、film≥2 独立场景、scene 取来源场景；confidence 0–1）+ 上面的输出示例。`open_review` 的 `_REVIEW_PROTOCOL_NOTE` 重写：删两步/组内比较/pass 段，保留「只对待评估标签输出、原样返回 id」与字段要求（字段要求由共享块承载）；`mtg.py` JUDGE_TEMPLATE 的「输出字段要求」段替换为 `VERDICT_OUTPUT_SPEC`，候选行/影片内容/目录区不动。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_mtg_judge.py tests/test_prompts_registry.py -v`（按实际文件名）
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/tagplatform/capabilities/prompts/_evaluation.py src/tagplatform/capabilities/prompts/mtg.py src/tagplatform/capabilities/prompts/open_review.py tests/
git commit -m "feat: 提示词融合——共享 VERDICT_OUTPUT_SPEC/RETRY_HINT，open-review-v14 协议瘦身（评审归一 Task 5）"
```

### Task 6: 生产侧接引擎（judge.py + pipeline.py）

**Files:**
- Modify: `src/tagplatform/services/generation/open/mtg/judge.py`（重写 `judge_candidates`/删 `_judge_batch/_map_items`）
- Modify: `src/tagplatform/services/generation/open/mtg/pipeline.py:82-96,135-150,346-389`（楼层 import、过滤段、`_DROP_QUOTE_FAIL`）
- Modify: `src/tagplatform/capabilities/llm/mock.py:_judge_output`（融合形状）
- Modify: `src/tagplatform/services/generation/open/mtg/parse.py`（删 `parse_judge_items` 及其测试引用迁移）
- Test: `tests/test_mtg_judge.py`（重写）、`tests/test_generation_open.py`（judge 断言更新）、`tests/test_mtg_verdicts.py`（不变）

**Interfaces:**
- Consumes: Task 1–5（`run_verdicts/VerdictTarget/VerdictPolicy/VERDICT_FLOORS/verdict_pass/finalize_verdict`）。
- Produces: `judge_candidates` 签名不变（返回 `dict[str, dict]`，值为终判嵌套 dict）；`pipeline._filter_candidates` 返回结构不变；audit `verdicts` 行 = 扁平投影（新增 `relevance_reason/semantics_reason/value_reasons/confidence/quote/reason/verify_fail`）。
- pipeline `verdicts` 消费点改嵌套读法：`_rank_by_value_and_similarity` 取 `verdict["value"]["score"]`；`_evidence_for` 取 `verdict["scenes"]`（键名不变）。

- [ ] **Step 1: 写失败测试**

- `judge_candidates`（Recorder 替身回融合形状）→ 返回值按 candidate_id 键控、值为终判嵌套形状（含 value.score 聚合、quote、verify_fail）；批大小 20（21 候选 → 2 次调用）；漏判候选不在返回值、不抛错（上抛由 `_filter_candidates` 落 `_DROP_NO_VERDICT`）；
- 全失败 → `JudgeError`（零判决时）；
- `_filter_candidates`（构造 verdicts dict 直测）：压分判决（verify_fail=="quote" + relevance=1）→ `dropped.why == "高相关分无逐字摘录支撑"`；verify_fail=="scenes" 无场景 → `"无有效场景证据"`；film 单场 → `"全片级但只有单场支撑"`；未过线（无 verify_fail）→ 前缀 `"judge 评分未过线"`；`is_filter_stage_drop("高相关分无逐字摘录支撑")` is True；
- mock 路径（`test_generation_open` 现有 mock 流水线用例）：`mtg_report` audit verdicts 行含 `relevance_reason` 与 `verify_fail` 键、扁平数值键仍在；`MTG_VERSION == "mtg-v15-unified-verdict"`；
- 现有 `test_generation_open` 全量过（断言更新处：verdict 行形状、淘汰原因文案集）。

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_mtg_judge.py tests/test_generation_open.py -v`
Expected: FAIL

- [ ] **Step 3: 实现**

`judge_candidates`：构造 `VerdictTarget`（key=candidate_id 或 normalize_key(tag)；level 由 scope 映射，excerpt→scene；allowed_scenes=film→全部、scene/excerpt→candidate.scenes∩场景索引；tag_kind=现 `_candidate_tag_kind`；has_desc=bool(desc)）+ `build_messages=lambda batch, attempt: build_judge_messages(content, batch_candidates, ...)（attempt>0 时附 JUDGE_RETRY_HINT）`；policy=`VerdictPolicy(max_batch_size=20)`；`match_by_name=True`；`semantics_null_without_desc=False`；haystack/scene_texts 按 `_filter_candidates` 现口径（`_normalize_quote(render_content/render_scene)`）。`pipeline._filter_candidates`：楼层检查改 `verdict_pass(verdict["relevance"]["score"], verdict["semantics"]["score"], verdict["value"]["score"])`，None 与 False 都淘汰（None 用 `_DROP_SCORE_BELOW` 前缀带「无法判断」标注）；verify_fail 专属原因映射（Global Constraints 的三条）；belt 两闸不动；`_require_quotes` 调用删除（引擎已做）；`_evidence_for` 不动。audit 行投影 helper `_audit_verdict_rows(verdicts, merged)`。`parse.py` 删 `parse_judge_items`（`test_mtg_judge.py` 解析用例已在 Task 2 迁至 `test_mtg_verdicts.py`）。mock `_judge_output`：条目改 `{"id": candidate_id 或 tag, "relevance": {"score": 4, "reason": "模拟内容评分"}, ..., "value": {三方向 {"score": 3, "reason": ...}}, "quote": 现逻辑, "scenes": 现逻辑, "confidence": 0.9}`。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_mtg_judge.py tests/test_generation_open.py tests/test_mtg_verdicts.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/tagplatform/services/generation/open/mtg/judge.py src/tagplatform/services/generation/open/mtg/pipeline.py src/tagplatform/services/generation/open/mtg/parse.py src/tagplatform/capabilities/llm/mock.py tests/
git commit -m "feat: 生产 judge 接统一引擎——过滤段 verdict_pass 复算+专属淘汰原因，mtg-v15（评审归一 Task 6）"
```

### Task 7: 评审侧接引擎（open_review.py 重写）

**Files:**
- Modify: `src/tagplatform/services/eval/open_review.py`（重写 `review_movie_tags`；删 `_parse_reviews/_clean_dimension/_clean_value_block/_row_index/_scene_mentions/_summarize_value_reasons`（后者已迁 verdicts））
- Modify: `src/tagplatform/capabilities/llm/mock.py:_quality_output`（融合形状、删 pass 分支）
- Test: `tests/test_eval_open_review.py`（大改）

**Interfaces:**
- Consumes: Task 1–6。
- Produces: `review_movie_tags` 签名不变（client, content, tag_rows, run_id, modalities, request_slot, trace, value_weights → list[dict]）；行回填键：`relevance/semantics/value`（终判嵌套，value 含 score/tag_kind/weights/dimensions）、`quote`、`scenes`、`confidence`、`reason`（派生）、`evidence_scene_ids`（=scenes）、`pass`（=verdict_pass 复算）、`evaluated`。失败行：三维 {score:None, reason:错误串}、pass None、evaluated False（现状契约）。`review_calibre.pass_mode = "threshold"`。

- [ ] **Step 1: 写失败测试（在现有 test_eval_open_review.py 上改）**

- 成功路径：StubClient 回融合形状（无 pass 字段）→ 行带派生 pass（由分数复算，(4,3,3.0)→True；分数 (2,4,3)→False）、`quote`/`confidence`/`scenes` 键在、`evidence_scene_ids` 与 scenes 一致；
- 请求形状：messages 无「该片其他标签（上下文）」块（StubClient 记录 messages 断言不含该串）；系统消息 == REVIEW_PROMPT（v14）；拆批请求也不带上下文块；
- 漏判行：三维 {score:None, reason:含错误}、pass None、evaluated False；
- 全失败 → `ReviewRequestError`；
- quote 核验：relevance=4 但 quote 与材料不符 → 行内 relevance.score==1、reason 含「未引用」；
- film 引用场景不足 → 压 1；
- 拆批/预算/截断用例沿用现有（StubClient.handler 信封/截断形状）断言行为等价（预算 2n+2、单条复试顺序、trace batches 键族）；
- mock：`MockLLMClient` 走「## 标签质量评估」→ `_quality_output` 回融合 verdicts（无 pass）、可被 `review_movie_tags` 消化（无 scenes 时 relevance None 路径不崩）；
- `review_calibre` 的 `pass_mode == "threshold"`。

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_eval_open_review.py -v`  Expected: FAIL

- [ ] **Step 3: 实现**

`review_movie_tags`：haystack=`_normalize_quote(render_content(content, modalities))`、scene_texts=逐场归一化；targets 由 tag_rows 构造（key=f"t{index}"、id_aliases=(str(index),)、level=row level、allowed_scenes=film→全部/scene→row scene_ids∩全部、tag_kind=现归一、has_desc=bool(desc)）；`build_messages=lambda batch, attempt: [{"role":"system","content":REVIEW_PROMPT},{"role":"user","content": haystack_text + "\n\n## 待评估标签\n" + json.dumps(label_view(batch), ensure_ascii=False) + (REVIEW_RETRY_HINT if attempt else "")}]`（label_view 含 id/name/desc/level/target_type/dir_path/dir_description/scene_ids——与现 labels 构造一致，**无上下文块**）；policy=`VerdictPolicy(split_on_missing=True, defer_single_retries=True)`；终判回填行 + 失败行契约。mock `_quality_output`：两分支统一融合形状（`{"verdicts": rows}`，行含 id + 每维 {score,reason} + value 三方向 + evidence→scenes + quote 空串 + confidence 0.9），删 pass/reason 总评分支。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_eval_open_review.py tests/test_mtg_verdicts.py -v`  Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/tagplatform/services/eval/open_review.py src/tagplatform/capabilities/llm/mock.py tests/test_eval_open_review.py
git commit -m "feat: 开放评审接统一引擎——整片盲评瘦身/阈值复算 pass/trace 统一键族（评审归一 Task 7）"
```

### Task 8: open_service 行契约与 meta

**Files:**
- Modify: `src/tagplatform/services/eval/open_service.py:855-876`（`_review_produced` fields 元组、行构造）
- Test: `tests/test_open_eval.py`（行字段断言更新）

**Interfaces:**
- Consumes: Task 7 行契约。
- Produces: review 行落 raw_results 的字段元组 `("pass", "relevance", "semantics", "value", "reason", "evidence_scene_ids", "evaluated", "quote", "confidence")`。

- [ ] **Step 1: 写失败测试**

`_review_produced`（StubClient 注入 review client）→ hits/new_words 行的 review dict 含 `quote` 与 `confidence` 键；`meta["review"]["pass_mode"] == "threshold"`；`review_error` 契约不变（评审失败产出保留）。**rereview 路径**：对已完成 run 调 `rereview` → 新 run 的评审走融合协议（行含 quote/confidence、pass 为复算值）——现有 `test_open_eval`/`test_api_eval` 的 rereview 用例随新形状更新。现有 `test_open_eval` 用例随新形状更新。

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_open_eval.py -v`  Expected: FAIL

- [ ] **Step 3: 实现**（fields 元组扩两键；其余逻辑不动）

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_open_eval.py tests/test_eval_open_review.py -v`  Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/tagplatform/services/eval/open_service.py tests/test_open_eval.py
git commit -m "feat: 评审行契约落 quote/confidence，meta.pass_mode=threshold（评审归一 Task 8）"
```

### Task 9: 报告复算与一致性对照（open_report.py）

**Files:**
- Modify: `src/tagplatform/services/eval/open_report.py`（`_group_stats` 派生 pass、consistency 段、schema 4、legacy 警示、CSV、MD）
- Test: `tests/test_eval_open_report.py`（新增/更新）

**Interfaces:**
- Consumes: Task 7/8 行契约；`verdict_pass`（verdicts.py）。
- Produces: `OPEN_REPORT_SCHEMA_VERSION = 4`；`summary.consistency = {"denominator": int, "review_rejected": int, "disagreement_rate": float | None, "review_undetermined": int, "no_judge_reference": int, "axis_delta": {axis: {"mean_abs": float | None, "exact_rate": float | None, "n": int}}}`；`_banner` 新增 legacy 警示（`meta.review.prompt_version != "open-review-v14"` 时加「旧协议 run：通过率按阈值复算口径重算，与原模型保留率不可横比」）。

- [ ] **Step 1: 写失败测试**

- `_group_stats`：行 review 分数 (4,3,3.0) 但行内 `pass` 字段是旧模型值 False → 统计按**复算**计 True（不信任存储 pass）；任一轴 None → judged 不计、`three_judged` 语义不变；
- 一致性段：构造行集（部分有 judge_reference 三维分、部分复算 None、部分缺 reference）→ denominator/rejected/rate/undetermined/no_reference/axis_delta 数值精确断言（None≠0：None 轴不进 mean_abs 分母）；
- schema_version==4；旧 schema 3 report.json → `upgrade_open_report` 用 raw 行复算出 pass_rate（分数复算而非存储 pass）、banner 含 legacy 警示；
- metrics.csv 新行：`disagreement_rate`（n=denominator）、`{axis}_mean_abs`/`{axis}_exact_rate`（n=axis_delta.n）；report.md 汇总节含「一致性对照」行。

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_eval_open_report.py -v`  Expected: FAIL

- [ ] **Step 3: 实现**

`_group_stats` 内 `passed/judged` 改为 `verdict_pass(row 三维 score)`（行存 pass 仅展示用）；`assemble_open_report` 汇总循环累计 consistency（行级 judge_reference 数值 vs review score）；`_banner` 加 legacy 分支；CSV/MD 各加行。`upgrade_open_report` 无需改逻辑（同 `assemble_open_report` 复算路径），仅需测试钉住。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_eval_open_report.py tests/test_api_eval.py -v`  Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/tagplatform/services/eval/open_report.py tests/test_eval_open_report.py
git commit -m "feat: 报告阈值复算+judge↔评审一致性对照，schema v4 与 legacy 警示（评审归一 Task 9）"
```

### Task 10: 前端（报告/审计组件）

**Files:**
- Modify: `frontend/src/components/OpenEvalReport.tsx`（labelDetail 阈值分支、quote 展示）
- Modify: `frontend/src/components/OpenEvalMovieAudit.tsx`（judge trace 统一 batches 键族 + legacy 容错）
- Modify: `frontend/src/api/evalOpen.ts`（JudgeVerdict 注释与新增键类型）
- Test: `frontend/src/components/OpenEvalReport.test.tsx`、`frontend/src/components/OpenEvalMovieAudit.test.tsx`

**Interfaces:**
- Consumes: Task 6 audit 行新增键、Task 7 review 行 quote/confidence、Task 9 consistency 段与 pass_mode。

- [ ] **Step 1: 写失败测试**

- OpenEvalReport：calibre `pass_mode: "threshold"` 夹具 → labelDetail 标签为评分口径文案、含 quote 时明细显示摘录文本；summary.consistency 夹具 → 一致性对照行渲染（分歧率/逐维均差/三桶计数）；
- OpenEvalMovieAudit：judge_trace 新键族夹具（batches）→ 逐批响应/错误渲染；**legacy 夹具**（judge_trace 只有 raw 列表）→ 旧形状照常渲染不崩；
- 通过列：pass False/None/True 渲染语义不变（键未动）。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend && npx vitest run src/components/OpenEvalReport.test.tsx src/components/OpenEvalMovieAudit.test.tsx`
Expected: FAIL

- [ ] **Step 3: 实现**（读组件现状后小改：trace 读取改 `batches ?? raw 兜底`；labelDetail threshold 分支拼各维 reason + quote 行；consistency 渲染段新增）

- [ ] **Step 4: 跑测试确认通过**

Run: `cd frontend && npx vitest run`  Expected: PASS（全组件）

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/OpenEvalReport.tsx frontend/src/components/OpenEvalMovieAudit.tsx frontend/src/api/evalOpen.ts frontend/src/components/OpenEvalReport.test.tsx frontend/src/components/OpenEvalMovieAudit.test.tsx
git commit -m "feat: 前端适配——阈值口径文案/quote 明细/统一 trace 键族+legacy 容错（评审归一 Task 10）"
```

### Task 11: 文档与全量回归

**Files:**
- Modify: `docs/eval.md`（§5 重写：整片评审口径、复算判定、一致性对照、口径变更四条）
- Modify: `docs/open-tag-quality-evaluation.md`（协议字段表更新）
- Test: 全量

**Interfaces:** 无代码接口；文档须含设计的四条口径变更声明（逐字）。

- [ ] **Step 1: 更新两份文档**（评审协议 v14/融合 schema/证据核验规则/淘汰原因映射/consistency 口径/「新旧 run 不可横比」警示）

- [ ] **Step 2: 全量回归**

Run: `python -m pytest -q`  Expected: 全绿（对照 main 的 331+ 基线）
Run: `cd frontend && npx vitest run`  Expected: 全绿

- [ ] **Step 3: Commit**

```bash
git add docs/eval.md docs/open-tag-quality-evaluation.md
git commit -m "docs: eval.md §5 重写为统一评审口径+四条口径变更声明（评审归一 Task 11）"
```
