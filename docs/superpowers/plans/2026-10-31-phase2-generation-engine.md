# 影视智能标签平台 阶段 2（智能标签生产引擎）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在阶段 1 数据底座上建成标签生产引擎：LLM 调用层、Prompt 模板外置（DB + 版本）、模式 A 封闭提取（按 tag_type 分组召回）/ 模式 B 开放生成（归一匹配）、generation_task 状态机与线程池执行、asset_tag upsert 入库（approved 保留）、批量打标 REST API。

**Architecture:** 核心纯 Python 包延续分层红线（`llm/`、`prompts.py`、`generation/`、`results.py`、`tasks/` 不 import fastapi）。模式 A/B 为同一 `TagProducer` 协议的两个注册实现（反失败承诺一）。任务执行核心是纯函数 `run_generation_task`，线程池仅 API 层使用。向量仍 BLOB + numpy（阶段 1 接口不变；sqlite-vec 推迟至有性能证据）。

**Tech Stack:** 阶段 1 既有栈 + openai（SDK，懒导入）+ python-dotenv。LLM = qwen3.7-plus（OpenAI 兼容 API，env `OPENAI_BASE_URL`/`OPENAI_API_KEY`），参照 MovieTagGen `movietaggen/generate/client.py` 照抄精简。

**Spec:** `intelligent-tag-platform/docs/superpowers/specs/2026-10-31-intelligent-tag-platform-design.md`（§5 标签生产引擎、§9 可扩展性、§11 阶段 2）

本计划只覆盖阶段 2；阶段 3（审核后台）在其落地后另立计划。基点：main @ `b1e0ebf`（阶段 1 已合并，54 tests 绿）。Task 12（外部适配器）与阶段 2 并行不冲突，样例到位随时补。

## Global Constraints

- 沿用阶段 1 全部约定：src 布局 `src/tagplatform/`；`TAGPLT_` 环境前缀；标识符英文、注释中文；核心包不 import fastapi；git 每任务一次 `feat:`/`fix:` 提交；测试 `.venv/Scripts/python -m pytest`（从 main 检出运行，`.venv` 已装好）。
- 依赖追加：`openai`、`python-dotenv`（`make_llm_client` 工厂内 `load_dotenv()`，保持 import 无副作用）。
- **测试零网络零下载**：LLM 一律 `MockLLMClient`、向量一律 `MockEmbedder`；真实 OpenAIClient 只测 transport 注入路径（注入函数，不发请求）。
- LLM 客户端照抄 MovieTagGen `movietaggen/generate/client.py` 结构：`LLMResponse(content/usage/raw/ok)`、`OpenAIClient(model, base_url_env, api_key_env, temperature, request_interval, transport=None, max_retries=3, extra_body=None)`、`complete(messages, run_id) -> LLMResponse`、response_format json_object 不支持时降级重试、指数退避、openai 懒导入。**不做**响应磁盘缓存（YAGNI，接口允许后加）。
- Settings 新字段（钉死默认值）：`llm_model="qwen3.7-plus"`、`llm_base_url_env="OPENAI_BASE_URL"`、`llm_api_key_env="OPENAI_API_KEY"`、`llm_temperature=0.7`、`llm_request_interval=0.5`、`use_mock_llm=False`、`recall_top_k_per_type=50`、`open_match_threshold=0.85`、`generation_workers=2`。
- 新表 `prompt_template`（第 9 张）：`mode TEXT NOT NULL(closed|open), version INTEGER NOT NULL, template_text TEXT NOT NULL, is_active INTEGER NOT NULL DEFAULT 0, created_at DATETIME`，UNIQUE(mode, version)；迁移 `0002_prompt_templates.py` 含模式 A/B v1 种子文案（见任务 2 附录，逐字使用）。一个 mode 至多一个 active（应用层保证 + 迁移种子只激活 v1）。
- LLM 输出 JSON 契约（解析层校验，坏结构 → 任务 failed）：
  - 模式 A：`[{"tag_id": str, "tag_name": str, "relevance": 0..1, "confidence": 0..1, "evidence": [{"scene_id": str, "start_ms": int, "end_ms": int, "description": str}]}]`（evidence 可为空数组）
  - 模式 B：`[{"tag_name": str, "tag_type": str, "description": str, "confidence": 0..1, "evidence": [同上]}]`（**不含 tag_id**——模式 B 的 tag_id 由匹配层赋，LLM 输出里的 tag_id 字段一律忽略）
- 召回/匹配均排除 `status == "deprecate"` 的标签。
- 治理与生产新代码显式传配置（终审建议）：`GovernanceConfig(similar_threshold, allowed_tag_types)`；`run_governance` 增可选参数 `config: GovernanceConfig | None = None`（None 时回退 `get_settings()`，向后兼容）。
- 沿用 Task 3 遗留约束：时间戳显式赋值；父子插入 flush 顺序。
- 阶段 2 不做：LLM 响应缓存、Prompt 编辑 API/UI（阶段 4）、审核 API（阶段 3）、sqlite-vec（推迟）。

## Review Focus

1. **LLM 返回坏 JSON / 非预期结构** → 解析抛 `LLMParseError`，任务 failed 且 error 含原因，**不写半截 asset_tag**（事务在任务级，异常回滚）。→ 任务 5/6 解析测试 + 任务 8 `test_run_task_llm_failure_atomic`。
2. **重跑/重试同一任务** → approved 记录原样保留（分数证据不动）；pending 记录被覆盖（分数/证据/task_id/updated_at 更新）；新标签新插；无重复行（部分唯一索引兜底）。→ 任务 7 三个 upsert 测试。
3. **模式 A 零候选**（标签库空或召回零命中）→ 任务 failed，error 明确"候选标签为空，请先导入标签库"。→ 任务 5 `test_closed_recall_empty_fails`。
4. **模式 B 幻觉 tag_id** → 解析层对模式 B 输出中的 tag_id 字段静默忽略（tag_id 只能来自匹配层）。→ 任务 6 `test_open_ignores_llm_tag_id`。
5. **进程重启留下 running 孤儿任务** → `create_app` 启动时把 running 状态任务重置为 failed（error="进程重启中断"）。→ 任务 8 `test_orphan_running_reset`。

## 文件结构总览

```
src/tagplatform/
├── llm/__init__.py        # make_llm_client（任务 1）
├── llm/client.py          # OpenAIClient + LLMResponse（任务 1）
├── llm/mock.py            # MockLLMClient（任务 1）
├── llm/parsing.py         # parse_closed_output / parse_open_output + LLMParseError（任务 5/6 前置，随任务 5 落地）
├── prompts.py             # get_active_prompt / seed 检查（任务 2）
├── generation/
│   ├── __init__.py        # register_producer / get_producer / list_producers（任务 3）
│   ├── base.py            # TagProducer Protocol + ProducedTag + GenerationSpec（任务 3）
│   ├── recall.py          # embed_content + recall_by_type（任务 5）
│   ├── closed.py          # ClosedTagProducer（任务 5）
│   └── open_gen.py        # OpenTagProducer（任务 6）
├── results.py             # persist_tags upsert（任务 7）
├── tasks/
│   ├── __init__.py
│   └── runner.py          # run_generation_task + TaskExecutor + 孤儿清理（任务 8）
├── governance/pipeline.py # 修改：GovernanceConfig 显式传参 + 批量编码（任务 4）
├── db/models.py           # 修改：+PromptTemplate（任务 2）
├── settings.py            # 修改：+9 个新字段（任务 1）
└── api/routers/generation.py、prompts.py  # 任务 9（含资产标签查询并入 generation.py）
alembic/versions/0002_prompt_templates.py # 任务 2
tests/test_llm*.py、test_prompts.py、test_generation_*.py、test_results.py、
test_task_runner.py、test_api_generation.py、test_governance.py（修改）
```

---

### Task 1: LLM 客户端层

**Files:**
- Create: `src/tagplatform/llm/__init__.py`、`src/tagplatform/llm/client.py`、`src/tagplatform/llm/mock.py`、`tests/test_llm_client.py`
- Modify: `src/tagplatform/settings.py`（+9 新字段）、`pyproject.toml`（+openai、python-dotenv）、`tests/conftest.py`（+`MOCK_LLM = MockLLMClient()` fixture 级常量）

**Interfaces:**
- Consumes: `get_settings()`
- Produces:
  - `LLMResponse(content: str, usage: dict, raw: dict, ok: bool)`（dataclass，与 MovieTagGen 同形）
  - `OpenAIClient(model, base_url_env="OPENAI_BASE_URL", api_key_env="OPENAI_API_KEY", temperature=0.7, request_interval=0.5, transport=None, max_retries=3, extra_body=None)`；`complete(messages: list[dict], run_id: str) -> LLMResponse`。行为照抄 MovieTagGen client.py：transport 注入（`Callable[[str, list[dict], float, dict | None, dict | None], dict]`）、懒导入 openai、json_object 降级（`_detect_json_format_error`）、指数退避 `0.5 * 2**attempt`、失败 `ok=False`。**去掉**磁盘缓存与 `sampling_params`。
  - `MockLLMClient`：`complete` 与 OpenAIClient 同签名。确定性策略：拼接 prompt 文本；若含 `## 候选标签` 节（模式 A 标志）→ 用 regex 解析候选行 `- {tag_id} | {tag_name} | {tag_type} | {description}`，按 `sha256(run_id + tag_id)` 哈希以 ~40% 概率选中，relevance/confidence = 0.5 + hash*0.5，evidence 为 `[]`；否则（模式 B）→ 固定返回 3 个候选 `多线叙事/情感张力/视听风格`（tag_type 依次 `narrative/mood/style`、description 一句话中文、confidence 由哈希映射 [0.5,1.0)、evidence `[]`）。raw 用 OpenAI 信封形状 `{"choices":[{"message":{"content": content},"finish_reason":"stop"}],"usage":{"prompt_tokens":100,"completion_tokens":50},"mock":True}`，content 为 `json.dumps(输出列表, ensure_ascii=False)`。
  - `make_llm_client(settings: Settings) -> OpenAIClient | MockLLMClient`：工厂内 `load_dotenv()`；`settings.use_mock_llm` → MockLLMClient。

- [ ] **Step 1: 写失败测试** — `tests/test_llm_client.py`：

```python
def test_mock_closed_selects_from_candidates():
    llm = MockLLMClient()
    msgs = [{"role": "user", "content": "## 候选标签\n- tag_001 | 悬疑 | genre | 以悬念为核心\n- tag_002 | 剧情 | genre | 叙事驱动"}]
    resp = llm.complete(msgs, "run_1")
    assert resp.ok and json.loads(resp.content)  # 列表，元素含 tag_id/relevance/confidence/evidence 键
    assert all(item["tag_id"] in {"tag_001", "tag_002"} for item in json.loads(resp.content))

def test_mock_open_returns_fixed_three():
    resp = MockLLMClient().complete([{"role": "user", "content": "为以下内容生成标签"}], "r1")
    out = json.loads(resp.content)
    assert [t["tag_name"] for t in out] == ["多线叙事", "情感张力", "视听风格"]

def test_openai_client_json_fallback_and_retry():
    calls = []
    def flaky_transport(model, messages, temperature, response_format, extra_body):
        calls.append(response_format)
        if len(calls) == 1: raise RuntimeError("json_object not supported")
        return {"choices": [{"message": {"content": "[]"}}], "usage": {}}
    client = OpenAIClient("m", transport=flaky_transport, request_interval=0, max_retries=3)
    resp = client.complete([{"role": "user", "content": "x"}], "r")
    assert resp.ok and resp.content == "[]" and calls[0] == {"type": "json_object"} and calls[1] is None

def test_openai_client_all_fail_returns_not_ok():
    def broken(model, messages, temperature, response_format, extra_body):
        raise RuntimeError("network down")
    client = OpenAIClient("m", transport=broken, request_interval=0, max_retries=2)
    resp = client.complete([{"role": "user", "content": "x"}], "r")
    assert not resp.ok and "network down" in resp.raw["error"]
```

另加 settings 测试：`use_mock_llm` 默认 False、`make_llm_client(Settings(use_mock_llm=True))` 返回 MockLLMClient、`openai` 不出现在 `sys.modules`（懒导入红线——测试全程不装不导）。

- [ ] **Step 2: 运行确认失败** — Run: `python -m pytest tests/test_llm_client.py -v`，Expected: FAIL（ImportError）

- [ ] **Step 3: 实现** — client.py 从 `../movietaggen/movietaggen/generate/client.py` 移植精简（注释改中文、去缓存/sampling_params/extra_body 保留）；mock.py 按上述策略（哈希函数用 sha256 前 8 hex / 0x100000000 同 MockEmbedder 风格）。pyproject 加依赖并 `pip install -e ".[dev]"`。

- [ ] **Step 4: 运行确认通过** — Run: `python -m pytest tests/test_llm_client.py -v && python -m pytest -q`，Expected: PASS（54 既有零回归）

- [ ] **Step 5: 提交** — `git add -A && git commit -m "feat: LLM 客户端层（OpenAI 兼容 + 确定性 Mock）"`

---

### Task 2: Prompt 模板表、迁移与种子

**Files:**
- Create: `src/tagplatform/prompts.py`、`alembic/versions/0002_prompt_templates.py`、`tests/test_prompts.py`
- Modify: `src/tagplatform/db/models.py`（+PromptTemplate）、`tests/conftest.py`（session fixture 建 prompt 表自动覆盖——`Base.metadata.create_all` 已覆盖，无需改）

**Interfaces:**
- Consumes: `Base`、session fixture
- Produces:
  - `class PromptTemplate(Base)`：`__tablename__ = "prompt_template"`；列 `mode TEXT NOT NULL`、`version INTEGER NOT NULL`、`template_text TEXT NOT NULL`、`is_active INTEGER NOT NULL DEFAULT 0`（SQLAlchemy Boolean）、`created_at DATETIME`；`UniqueConstraint("mode", "version")`。
  - `get_active_prompt(session, mode: str) -> PromptTemplate`（无 active → `ValueError(f"mode={mode} 无激活的 Prompt 模板")`）
  - `render_prompt(template: PromptTemplate, **fields) -> str`（`template.template_text.format(**fields)`，缺占位符 KeyError 自然抛）
  - **种子模板（迁移内逐字插入，v1、is_active=1）**——见本任务附录 A/B。
- 迁移：`alembic revision --autogenerate -m "prompt templates"` 后手工补 INSERT 种子（autogenerate 不产数据行）。

- [ ] **Step 1: 写失败测试**

```python
def test_get_active_prompt_seeded(session):
    for mode in ("closed", "open"):
        t = get_active_prompt(session, mode)
        assert t.version == 1 and t.is_active and "{content}" in t.template_text
    assert "{candidates}" in get_active_prompt(session, "closed").template_text

def test_get_active_prompt_missing_mode(session):
    with pytest.raises(ValueError): get_active_prompt(session, "nope")

def test_render_prompt_fills_placeholders(session):
    text = render_prompt(get_active_prompt(session, "closed"), content="内容X", candidates="候选Y")
    assert "内容X" in text and "候选Y" in text and "{" not in text.replace("{{", "")

def test_migration_upgrades_with_seed(tmp_path):
    # 子进程 alembic upgrade head → sqlite_master 含 prompt_template → 行数 = 2 且各 mode v1 active
```

- [ ] **Step 2: 运行确认失败** — Run: `python -m pytest tests/test_prompts.py -v`，Expected: FAIL

- [ ] **Step 3: 实现** — models.py 加类；`TAGPLT_DATABASE_PATH=<tmp>/m.db alembic revision --autogenerate` 生成 0002 后手工加两行 INSERT（附录文案）；prompts.py 两函数。

- [ ] **Step 4: 运行确认通过** — Run: `python -m pytest tests/test_prompts.py -v && python -m pytest -q`，Expected: PASS

- [ ] **Step 5: 提交** — `git commit -m "feat: Prompt 模板表与 v1 种子（模式 A/B）"`

**附录 A：模式 A v1 模板**（`mode='closed'`，占位符 `{content}` `{candidates}`）

```text
你是影视内容标签专家。以下是一部影视内容的结构化描述：

{content}

候选标签清单（格式：tag_id | 名称 | 分类 | 定义）：
{candidates}

任务：从候选清单中选出与内容真正相关的标签。对每个选中标签输出：
- tag_id（必须与候选清单一致，不得编造）
- tag_name（与候选一致）
- relevance：标签与内容的相关程度，0 到 1
- confidence：你对该判断的置信度，0 到 1
- evidence：支持该标签的内容证据列表，每条含 scene_id、start_ms、end_ms、description

只输出 JSON 数组，不要输出其他文字。未选中的标签不要输出。
```

**附录 B：模式 B v1 模板**（`mode='open'`，占位符 `{content}`）

```text
你是影视内容标签专家。以下是一部影视内容的结构化描述：

{content}

任务：为该内容自由生成最贴切的语义标签（不受任何既有标签体系限制）。对每个标签输出：
- tag_name：简洁的标签名（2-8 字）
- tag_type：标签分类（如 genre/mood/theme/narrative/style/character 或自拟）
- description：一句话语义定义
- confidence：你对该标签的置信度，0 到 1
- evidence：支持该标签的内容证据列表，每条含 scene_id、start_ms、end_ms、description

只输出 JSON 数组，不要输出其他文字。输出 5 到 15 个标签。
```

---

### Task 3: TagProducer 协议与注册表

**Files:**
- Create: `src/tagplatform/generation/__init__.py`、`src/tagplatform/generation/base.py`、`tests/test_generation_base.py`

**Interfaces:**
- Consumes: `ContentJSON`、`LLMResponse` 层契约（仅类型引用）
- Produces:
  - `@dataclass GenerationSpec`: `task_id: str, asset_id: str, content: ContentJSON, policy: GenerationPolicy, prompt_text: str, run_id: str`
  - `@dataclass GenerationPolicy`: `max_tags: int = 30, min_confidence: float = 0.7`
  - `@dataclass Evidence`: `scene_id: str, start_ms: int, end_ms: int, description: str`
  - `@dataclass ProducedTag`: `origin: str("catalog"|"generated"), tag_id: str | None, tag_name: str, candidate_type: str | None, candidate_description: str | None, relevance_score: float | None, confidence: float, evidence: list[Evidence]`
  - `class TagProducer(Protocol): mode: str; def produce(self, spec: GenerationSpec, session: Session, llm, embedder) -> list[ProducedTag]`
  - `register_producer(producer) -> None`（重复 mode 抛 ValueError）、`get_producer(mode: str) -> TagProducer`（KeyError 列出可用 mode）、`list_producers() -> list[str]`

- [ ] **Step 1: 写失败测试** — 注册表三函数行为（同 adapters 测试模式：重复注册 ValueError、未知 mode KeyError 列可用、空注册表 `list_producers() == []`——**注意：本任务不注册任何实现，注册发生在任务 5/6 的模块导入时**）+ GenerationSpec/Policy/ProducedTag 构造与默认值断言。
- [ ] **Step 2: 确认失败** — FAIL（ImportError）
- [ ] **Step 3: 实现** — 模块级 dict 注册表（同 adapters 模式）。
- [ ] **Step 4: 确认通过 + 全量** — PASS
- [ ] **Step 5: 提交** — `git commit -m "feat: TagProducer 协议与注册表"`

---

### Task 4: 治理层显式配置与批量编码

**Files:**
- Modify: `src/tagplatform/governance/pipeline.py`、`tests/test_governance.py`

**Interfaces:**
- Consumes: 既有 `run_governance(rows, session, embedder)`
- Produces: `@dataclass GovernanceConfig: similar_threshold: float = 0.90, allowed_tag_types: set[str] = field(default_factory=set)`；`run_governance(rows, session, embedder, config: GovernanceConfig | None = None)`——None 回退 `get_settings()`（行为不变）；**批量编码**：把逐行 `embedder.embed([text])` 改为对全部待检行一次 `embedder.embed(texts)`（语义检测与插入用同一批向量，embed 调用次数从 2N 降到 2——判定阶段一次、插入复用缓存）。

- [ ] **Step 1: 写失败测试**

```python
def test_governance_explicit_config_no_global(monkeypatch):
    # 不动 get_settings（monkeypatch 不设任何东西），显式传 allowed={"genre"}
    cfg = GovernanceConfig(similar_threshold=0.99, allowed_tag_types={"genre"})
    res = run_governance([TagRow(None, "悬疑", "bad_type", "以悬念为核心", None, [], None, None)],
                         session, MOCK_EMBEDDER, config=cfg)
    assert res.issues[0].code == "tag_type_invalid"

def test_governance_batch_embedding_calls(session):
    calls = []
    class CountingEmbedder:
        name, dim = "counting", 8
        def embed(self, texts):
            calls.append(len(texts)); return np.ones((len(texts), 8), np.float32)
    run_governance([TagRow(None, f"标签{i}", "genre", f"描述{i}", None, [], None, None)
                    for i in range(5)], session, CountingEmbedder())
    assert calls == [5] or calls == [5, 0]  # 一次批量，无逐行调用；空批次容差
```

既有 6+2 个治理测试**不改动断言**（None 回退路径必须保持全绿——这是向后兼容的验收）。

- [ ] **Step 2: 确认失败** — FAIL（GovernanceConfig 不存在 / calls 断言失败）
- [ ] **Step 3: 实现** — pipeline 内 `_load_config()` 与批量 embed 重构；保持判定顺序与副作用不变。
- [ ] **Step 4: 确认通过 + 全量**（54+ 既有零回归）
- [ ] **Step 5: 提交** — `git commit -m "feat: 治理显式配置传参与批量编码"`

---

### Task 5: 模式 A——封闭式标签提取

**Files:**
- Create: `src/tagplatform/generation/recall.py`、`src/tagplatform/generation/closed.py`、`src/tagplatform/llm/parsing.py`、`tests/test_generation_closed.py`
- Modify: `src/tagplatform/generation/__init__.py`（closed 模块导入时自动注册）

**Interfaces:**
- Consumes: `GenerationSpec/ProducedTag/TagProducer`、`get_active_prompt`/`render_prompt`（组装由任务 8 的 runner 做，producer 收 `spec.prompt_text`）、`make_embedder` 契约、`search_similar`、`TagCatalog` 模型、`LLMResponse`
- Produces:
  - `recall.py`：`embed_content_texts(content: ContentJSON) -> list[str]`（`["summary", scene1_desc, scene2_desc...]` 文本列表）；`recall_by_type(session, queries: list[str], embedder, top_k: int) -> list[TagCatalog]`——embed(queries) 一次批量；tag_catalog 全量（排除 deprecate、embedding 非空）；按 tag_type 分组，每 query × 每组合并命中、组内按最高分取 top_k；跨组并集按 tag_id 去重；**空结果返回 []**。
  - `parsing.py`：`LLMParseError(Exception)`；`parse_closed_output(content: str) -> list[dict]`（json.loads → 校验每元素含 tag_id/tag_name/relevance/confidence/evidence 四键且 relevance/confidence ∈ [0,1] → 坏结构抛 LLMParseError，消息含首个坏元素位置）；`parse_open_output(content: str) -> list[dict]`（同构校验 tag_name/tag_type/description/confidence/evidence；**多余键（含 tag_id）静默丢弃**）。
  - `closed.py`：`class ClosedTagProducer` `mode = "closed"`；`produce(spec, session, llm, embedder) -> list[ProducedTag]`：
    1. `candidates = recall_by_type(session, embed_content_texts(spec.content), embedder, settings 回退 get_settings().recall_top_k_per_type)`——producer 直接读 `get_settings()`（阶段 2 保持与治理相同的回退模式；显式传参留给后续统一重构）
    2. 空候选 → `raise ValueError("候选标签为空，请先导入标签库")`
    3. prompt 已由 runner 组装（`spec.prompt_text` 含 candidates）——**producer 内不再重复组装**，直接 `llm.complete([{"role": "user", "content": spec.prompt_text}], spec.run_id)`
    4. `not resp.ok` → `raise RuntimeError(f"LLM 调用失败: {resp.raw.get('error')}")`；`parse_closed_output(resp.content)`
    5. 过滤 `confidence >= spec.policy.min_confidence`，按 relevance 降序截断 `spec.policy.max_tags`
    6. 映射 `ProducedTag(origin="catalog", tag_id=item["tag_id"], tag_name=item["tag_name"], relevance_score=item["relevance"], confidence=item["confidence"], evidence=[Evidence(**e) for e in item["evidence"]])`（candidate_* 为 None）
  - 媒体上下文向量回填：runner 职责（任务 8），producer 不管库写。

- [ ] **Step 1: 写失败测试**（全部 MockEmbedder + MockLLM）

```python
def _seed_catalog(session, n=6): ...  # 2 个 tag_type × 各 3 条，embedding=encode_vector(MOCK_EMBEDDER.embed([f"{name}。{desc}"])[0])

def test_closed_recall_groups_and_dedup(session):
    cats = recall_by_type(session, ["悬疑 推理"], MOCK_EMBEDDER, top_k=2)
    assert len({c.tag_id for c in cats}) == len(cats)  # 去重
    assert all(c.tag_type in {"genre", "mood"} for c in cats)  # 分组内 top-2

def test_closed_recall_empty_catalog(session):
    assert recall_by_type(session, ["任何"], MOCK_EMBEDDER, top_k=5) == []

def test_closed_produce_end_to_end(session, content_fixture):
    _seed_catalog(session)
    spec = GenerationSpec(task_id="t1", asset_id=content_fixture.asset.asset_id,
                          content=content_fixture, policy=GenerationPolicy(),
                          prompt_text=render_prompt(get_active_prompt(session, "closed"),
                          content=..., candidates=...), run_id="r1")
    out = ClosedTagProducer().produce(spec, session, MOCK_LLM, MOCK_EMBEDDER)
    assert out and all(t.origin == "catalog" and t.tag_id for t in out)
    assert all(t.confidence >= 0.7 for t in out) and len(out) <= 30
    assert any(t.tag_id == "tag_001" for t in out)  # mock 哈希选中的候选

def test_closed_no_candidates_raises(session, content_fixture):
    spec = ...  # 空库
    with pytest.raises(ValueError, match="候选标签为空"): ClosedTagProducer().produce(...)

def test_parse_closed_bad_structure():
    with pytest.raises(LLMParseError): parse_closed_output('{"not": "a list"}')
    with pytest.raises(LLMParseError): parse_closed_output('[{"tag_id": "x"}]')  # 缺键
    with pytest.raises(LLMParseError): parse_closed_output('[{"tag_id":"x","tag_name":"n","relevance":2.0,"confidence":0.5,"evidence":[]}]')
```

- [ ] **Step 2: 确认失败** — FAIL
- [ ] **Step 3: 实现** — 注意 recall 的余弦直接用 numpy 点积（向量已 L2 归一化，不用调 search_similar 也行——但复用 `search_similar` 每组调用一次最省代码：每组 `search_similar(q, 组内候选, top_k)` 对每 query）。
- [ ] **Step 4: 确认通过 + 全量**
- [ ] **Step 5: 提交** — `git commit -m "feat: 模式 A 封闭式标签提取（分组召回 + LLM 判定）"`

---

### Task 6: 模式 B——开放式标签生成

**Files:**
- Create: `src/tagplatform/generation/open_gen.py`、`tests/test_generation_open.py`
- Modify: `src/tagplatform/generation/__init__.py`（自动注册）

**Interfaces:**
- Consumes: `parse_open_output`（任务 5 的 parsing.py）、`TagProducer`、`TagCatalog`
- Produces: `class OpenTagProducer` `mode = "open"`；`produce(spec, session, llm, embedder) -> list[ProducedTag]`：
  1. `resp = llm.complete([{"role": "user", "content": spec.prompt_text}], spec.run_id)`；not ok → RuntimeError（同 A）
  2. `items = parse_open_output(resp.content)`
  3. 对每个 item：`vec = embedder.embed([f"{name}。{description}"])[0]`（一次批量 embed 全部 items）；与 tag_catalog 非废弃向量 `search_similar` 取最高分；最高分 ≥ `get_settings().open_match_threshold` → `ProducedTag(origin="catalog", tag_id=命中 tag_id, ...)`；否则 `ProducedTag(origin="generated", tag_id=None, candidate_name=name, candidate_type=type, candidate_description=desc)`；relevance_score = 匹配相似度（generated 为 None 或匹配分数？**generated 的 relevance_score=None**）
  4. 过滤 min_confidence + max_tags 截断（按 confidence 降序）
- `test_open_ignores_llm_tag_id`：MockLLM 模式 B 输出不含 tag_id——用手工构造带 `"tag_id": "hacked"` 的 content 喂 `parse_open_output`，断言输出 dict 无 tag_id 键（Review Focus 4）。

- [ ] **Step 1: 写失败测试** — 端到端（固定 3 候选中 ≥1 匹配已导入同名标签 → origin=catalog；库空 → 全 generated 且 relevance_score=None、candidate_* 齐）、阈值边界（恰好 0.85 含等于 → catalog）、ignore tag_id、min_confidence 过滤。
- [ ] **Step 2: 确认失败** — FAIL
- [ ] **Step 3: 实现**
- [ ] **Step 4: 确认通过 + 全量**
- [ ] **Step 5: 提交** — `git commit -m "feat: 模式 B 开放式标签生成（自由生成 + 归一匹配）"`

---

### Task 7: 结果入库服务（upsert 语义）

**Files:**
- Create: `src/tagplatform/results.py`、`tests/test_results.py`

**Interfaces:**
- Consumes: `ProducedTag`、`AssetTag` 模型、`Evidence`
- Produces: `persist_tags(session, asset_id: str, task_id: str, tags: list[ProducedTag], run_id: str) -> PersistSummary`，`@dataclass PersistSummary: inserted: int, updated: int, skipped_approved: int`。逐条：
  - `origin == "catalog"`：查 `(asset_id, tag_id)`——无 → 插入（review_status="pending"、evidence_json=json.dumps([asdict(e)...])、时间戳显式）；有且 `review_status == "approved"` → **跳过（什么都不动）**、skipped_approved+1；有且其他状态 → 更新 relevance/confidence/evidence_json/task_id/origin/candidate_*（candidate_* 置 None）、updated_at、updated+1
  - `origin == "generated"`：按 `(asset_id, candidate_name)` 同规则（approved/merged 视为已定，跳过）
  - 全部写完后调用方 commit（服务内不 commit——由 runner 统一事务控制，满足 Review Focus 1 原子性）

- [ ] **Step 1: 写失败测试**

```python
def _ptag(tag_id=None, name="悬疑", origin="catalog", conf=0.9):
    return ProducedTag(origin=origin, tag_id=tag_id, tag_name=name, candidate_type=None,
                       candidate_description=None, relevance_score=0.9, confidence=conf,
                       evidence=[Evidence("scene_001", 1000, 2000, "证据")])

def test_persist_insert_pending(session):
    s = persist_tags(session, "a1", "t1", [_ptag("tag_1")], "r1")
    assert s == PersistSummary(1, 0, 0) and session.query(AssetTag).one().review_status == "pending"

def test_persist_rerun_overwrites_pending_keeps_approved(session):
    persist_tags(session, "a1", "t1", [_ptag("tag_1", conf=0.95)], "r1")
    session.query(AssetTag).one().review_status = "approved"; session.commit()
    s = persist_tags(session, "a1", "t2", [_ptag("tag_1", conf=0.5)], "r2")
    row = session.query(AssetTag).one()
    assert s.skipped_approved == 1 and row.confidence == 0.95 and row.task_id == "t1"  # 原值未动

def test_persist_pending_overwritten(session):
    persist_tags(session, "a1", "t1", [_ptag("tag_1", conf=0.95)], "r1")
    s = persist_tags(session, "a1", "t2", [_ptag("tag_1", conf=0.5)], "r2")
    row = session.query(AssetTag).one()
    assert s.updated == 1 and row.confidence == 0.5 and row.task_id == "t2"

def test_persist_generated_candidate_upsert(session):
    g = _ptag(None, "多线叙事", origin="generated")
    persist_tags(session, "a1", "t1", [g], "r1")
    assert session.query(AssetTag).one().candidate_name == "多线叙事"
    # 重跑覆盖 pending 候选；approved 候选跳过
```

- [ ] **Step 2: 确认失败** — FAIL
- [ ] **Step 3: 实现**
- [ ] **Step 4: 确认通过 + 全量**
- [ ] **Step 5: 提交** — `git commit -m "feat: 标签结果入库（approved 保留 / pending 覆盖 upsert）"`

---

### Task 8: 任务执行器

**Files:**
- Create: `src/tagplatform/tasks/__init__.py`、`src/tagplatform/tasks/runner.py`、`tests/test_task_runner.py`

**Interfaces:**
- Consumes: `get_producer`、`get_active_prompt`/`render_prompt`、`persist_tags`、`GenerationTask`/`MediaContext`/`AssetTag` 模型、`parse_content`、`make_llm_client`/`make_embedder`、`encode_vector`
- Produces:
  - `run_generation_task(task_id: str, session, llm, embedder) -> GenerationTask`（纯函数，**内含完整事务**）：
    1. 取 task；状态非 queued → `ValueError("任务状态非 queued")`；置 running、commit
    2. 取 context（`session.get(MediaContext, task.context_id)`，缺失 → failed+error）
    3. `content = parse_content(json.loads(context.content_json))`；资产 = content.asset.asset_id or context.asset_id
    4. `producer = get_producer(task.mode)`；`prompt_text = render_prompt(get_active_prompt(session, task.mode), content=组装文本, candidates=组装文本)`
       - `content` 文本组装（两模式共用）：`f"《{title}》\n摘要：{summary}\n" + 每场景 f"- {scene_id} [{start_ms}-{end_ms}] {description}"`
       - `candidates` 文本组装（仅 closed）：每候选一行 `f"- {tag_id} | {tag_name} | {tag_type} | {description}"`
    5. `policy = GenerationPolicy(**json.loads(task.policy_json))`（空 JSON → 默认）
    6. `tags = producer.produce(spec, session, llm, embedder)`——**任一异常（ValueError/RuntimeError/LLMParseError）**：置 failed、error=str(e)、finished_at、commit、re-raise? **不 re-raise**（返回 task；调用方看状态）
    7. `persist_tags(session, asset_id, task_id, tags, run_id)`；**模式 A 回填 context.embedding**（摘要向量 `encode_vector(embedder.embed([摘要文本])[0])`——注意 producer 已算过，此处重算一次可接受（Mock 零成本；真模型 embed 本地便宜），或 produce 返回携带——**定：runner 独立重算摘要向量**，接口简单）
    8. task.model_version = llm.model（MockLLM 为 "mock-1"）、task.prompt_version = active prompt.version、状态 succeeded、finished_at、commit；返回 task
  - `class TaskExecutor`：`__init__(session_factory, llm, embedder, workers=2)`；`submit(task_id) -> None`（`ThreadPoolExecutor` 提交 `run_generation_task`，线程内独立 session，异常吞并记日志 `logger.exception`）；`reset_orphan_running(session) -> int`（running → failed, error="进程重启中断"，返回数量；`create_app` 启动时调用）
  - `run_id = f"{task_id}:{uuid4().hex[:6]}"`（同 task 重跑换 run_id，Mock 哈希采样随之变化）

- [ ] **Step 1: 写失败测试**

```python
def _make_task(session, mode="closed"): ...  # asset + media_context + generation_task(status="queued")

def test_run_task_success_writes_tags(session):
    _seed_catalog(session); t = _make_task(session)
    run_generation_task(t.task_id, session, MOCK_LLM, MOCK_EMBEDDER)
    assert t.status == "succeeded" and t.prompt_version == 1 and t.model_version == "mock-1"
    assert session.query(AssetTag).count() >= 1 and t.finished_at is not None

def test_run_task_llm_failure_atomic(session):
    class BrokenLLM:
        model = "broken"
        def complete(self, messages, run_id):
            return LLMResponse(content="", usage={}, raw={"error": "boom"}, ok=False)
    t = _make_task(session)
    run_generation_task(t.task_id, session, BrokenLLM(), MOCK_EMBEDDER)
    assert t.status == "failed" and "LLM 调用失败" in t.error
    assert session.query(AssetTag).count() == 0   # Review Focus 1：零半截写入

def test_run_task_empty_catalog_fails(session):
    t = _make_task(session)
    run_generation_task(t.task_id, session, MOCK_LLM, MOCK_EMBEDDER)
    assert t.status == "failed" and "候选标签为空" in t.error

def test_run_task_wrong_status_rejected(session):
    t = _make_task(session); t.status = "running"; session.commit()
    with pytest.raises(ValueError, match="非 queued"): run_generation_task(...)

def test_orphan_running_reset(session):
    t = _make_task(session); t.status = "running"; session.commit()
    assert reset_orphan_running(session) == 1
    assert t.status == "failed" and t.error == "进程重启中断"

def test_executor_thread_pool(tmp_path):  # 用文件库 + 真线程池
    ...  # submit 后轮询 task.status 变 succeeded（上限 10s），验证独立 session 路径
```

- [ ] **Step 2: 确认失败** — FAIL
- [ ] **Step 3: 实现** — 注意 runner 内多段 commit（running 置位先 commit 防止长任务占用未见状态）。
- [ ] **Step 4: 确认通过 + 全量**
- [ ] **Step 5: 提交** — `git commit -m "feat: 生成任务执行器（状态机/线程池/孤儿清理）"`

---

### Task 9: API——任务创建、执行、查询与重试

**Files:**
- Create: `src/tagplatform/api/routers/generation.py`、`src/tagplatform/api/routers/prompts.py`、`tests/test_api_generation.py`
- Modify: `src/tagplatform/api/__init__.py`（注册路由 + `app.state.executor` 构建 + 启动时 `reset_orphan_running` + shutdown 关线程池）

**Interfaces:**
- Consumes: `run_generation_task`、`TaskExecutor`、`get_producer`/`list_producers`、`get_active_prompt`、`GenerationTask`/`AssetTag`/`MediaContext`/`TagCatalog`、conftest `client` fixture（沿用真 app + 临时文件 DB + mock embedder——**需加 `use_mock_llm=True`**）
- Produces（路由）：
  - `POST /api/v1/generation/tasks`：body `{"asset_ids": ["movie_001", ...]（非空列表）, "mode": "closed"|"open", "policy": {"max_tags": 30, "min_confidence": 0.7}（可选，默认）}` → 逐 asset：取其**最新** media_context（created_at 降序第一，无 → 整个请求 422 `{"detail": "资产 {id} 无识别内容"}`）；建 generation_task（status=queued、policy_json、mode）并 `executor.submit`；响应 `{"tasks": [{"task_id", "asset_id", "status"}]}`。mode 未知 → 422（列出可用 mode）；asset 不存在 → 404。**测试同步执行问题**：TaskExecutor 在测试里换成直接同步执行（`create_app` 支持 settings.generation_workers=0 → 同步模式：submit 即 run_generation_task）——钉死：`generation_workers=0` 表示同步执行。
  - `GET /api/v1/generation/tasks?status=&mode=&asset_id=`：列表（task_id/asset_id/mode/status/model_version/prompt_version/error/created_at/finished_at，created_at 降序 + task_id 决胜，limit/offset 默认 100/0）
  - `GET /api/v1/generation/tasks/{task_id}`：详情 + `results`：join tag_catalog 的摘要列表（`tag_name`（catalog 行取标签名，generated 取 candidate_name）、`origin`、`relevance_score`、`confidence`、`review_status`、`evidence`（解析 evidence_json））
  - `POST /api/v1/generation/tasks/{task_id}/retry`：仅 `failed` 可重试（否则 422 `{"detail": "仅失败任务可重试"}`）；状态回 queued、error/finished_at 清空、executor.submit → `{"task_id", "status": "queued"}`
  - `GET /api/v1/prompts?mode=`：`[{mode, version, is_active, created_at}]`
  - `GET /api/v1/assets/{asset_id}/tags`：该资产全部 asset_tag（同 results 摘要形状 + tag_id/candidate_name 原样）
  - `Settings` 加字段：`generation_workers: int = 2`（0 = 同步，测试与单步调试用）

- [ ] **Step 1: 写失败测试** — 端到端（`generation_workers=0` 同步模式）：导标签库 → 导内容 → POST tasks（closed）→ 响应 tasks 状态 succeeded（同步执行完）→ GET 详情 results 非空 → GET assets/{id}/tags 非空；重试路径（先造 failed 任务再 retry → queued → succeeded）；422 三类（坏 mode 列可用、无 context、非 failed 重试）；404（未知 asset/task）；prompts 列表。
- [ ] **Step 2: 确认失败** — FAIL（404 路由不存在）
- [ ] **Step 3: 实现** — 路由薄层；executor 挂 app.state（ lifespan 构建/关闭）；conftest client fixture 升级。
- [ ] **Step 4: 确认通过 + 全量**
- [ ] **Step 5: 提交** — `git commit -m "feat: 生成任务 API（批量创建/查询/重试）与 Prompt 列表"`

---

## 收尾核对（执行完成后）

- `python -m pytest` 全绿；`alembic upgrade head` 空库可执行（0002 含种子）。
- MockLLM 全链路冒烟（uvicorn + curl）：`use_mock_llm+use_mock_embedder` 起服务 → 导 Excel → 导内容 → `POST /api/v1/generation/tasks`（两种 mode 各一次）→ GET 任务详情与 `assets/{id}/tags` → 重试一个 failed 任务。
- 真实 LLM 冒烟（可选，需 `OPENAI_BASE_URL/OPENAI_API_KEY`）：单影片单任务 closed 模式跑一次，人工抽查输出质量——作为阶段 3 前的质量信号，不作为合并门槛。
- 更新项目状态记忆：阶段 2 完成；阶段 3 计划（审核后台 + 账号 + 视频播放）输入 = 本阶段 API 面与 `review_status` 数据。
