# Prompt 文案集中管理 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把全部指令性 prompt 文案集中到 `capabilities/prompts/`（常量 + 只读 registry 端点 + 配置页"流水线文案"区块），行为零变化。

**Architecture:** 纯位移搬运——服务层组装函数改为 import 常量，拼接行为不变；`capabilities/prompts/` 新增 `mtg.py`/`assets.py`/`quality.py`/`registry.py` 四个纯常量模块；API 只增 `GET /api/v1/prompts/registry`；前端 `ModelConfig` 增只读区块。

**Tech Stack:** FastAPI + pytest（后端，927 基线）；React 18 + antd + vitest（前端，257 基线）。

**Spec:** `docs/superpowers/specs/2026-11-22-prompt-copy-centralization-design.md`（已批准；搬运边界线"无参数槽指令文案块搬、协议串/渲染格式/双向词表/含参行留"是本计划的任务分解依据）。

## Global Constraints

- `capabilities/prompts/` 纯常量 + 纯函数，不得 import fastapi / sqlalchemy / `tagplatform.services`
- 现有 `GET /api/v1/prompts` 行为零变化（mode 过滤、排序、POST→405、activate→404 契约锁定不动）；新端点纯增量
- 无 DB 变化、无迁移
- 协议不动：`GENERATION_MARKER`/`JUDGE_MARKER`/`VERIFY_MARKER`、场景行/候选行/图注格式、`MOODS`/`FEELINGS` 词表（双向协议，留 `emotions.py`）、parse 契约、mock 咬合原位原样
- **逐字搬运**：搬运提交的文案 diff 为纯位移（删一处 + 原样加一处）；"改文案"不混入搬运提交
- 新搬常量不经 `render_prompt`/`str.format` 渲染；`{content}` 占位符与花括号转义约定只属于 closed/open mode 模板
- 含参数槽的行（输出格式 JSON 行嵌 scope/dir_field/max_tags、builder 头部嵌 title/genres、场景要求块嵌情绪词表）留原地
- 后端测试命令：worktree 根 `python -m pytest -q`；前端：`bash -c "cd frontend && npm test"`（vitest 需在 frontend/ 下跑）

## Review Focus

（五条搬运最可能咬人的失效模式，按序；每条已落到 owning 任务的测试）

1. **搬运手滑改字面量**（复制粘贴漂移，测试仍绿但语义变）→ 运行时网 = 既有 mock 咬合/parse 契约测试（Task 1/2 全量回归）；纯位移验收是终末 reviewer 的 diff 职责
2. **新常量模块违反分层红线**（import services → 循环导入）→ Task 1 建静态源断言（`tests/test_prompts_central.py`），Task 2 扩到 assets/quality/registry
3. **registry 端点破坏既有 /prompts 契约锁**（路径互踩使 405/404 失效）→ Task 2：`test_api_prompts_admin` 原样全绿 + 新测试显式断言 `POST /api/v1/prompts` 仍 405
4. **常量搬走后旧命名空间消费者断**（`tasks/tag_quality` 从 service 模块 import `PROMPT_VERSION`）→ Task 2：服务模块 import 即再导出 + 既有 eval 测试绿
5. **前端 registry 加载失败拖死整页**（配置页其他区块不可用）→ Task 3：registry 独立错误态（局部 Alert），测试断言 Prompt 模板区仍渲染

---

### Task 1: mtg + closed 指令文案搬运

**Files:**
- Create: `src/tagplatform/capabilities/prompts/mtg.py`
- Modify: `src/tagplatform/capabilities/prompts/templates.py`（追加 `CLOSED_RETRY_HINT`）
- Modify: `src/tagplatform/services/generation/open/mtg/prompts.py`
- Modify: `src/tagplatform/services/generation/open/mtg/descriptions.py`
- Modify: `src/tagplatform/services/generation/open/mtg/verify.py`
- Modify: `src/tagplatform/services/generation/open/mtg/judge.py`
- Modify: `src/tagplatform/services/generation/closed/producer.py`
- Test: `tests/test_prompts_central.py`（新建）

**Interfaces:**
- Produces（Task 2 registry 消费；全部 `str`，纯文本无占位符协议）:

```python
from tagplatform.capabilities.prompts.mtg import (
    GENERATION_ROLE_TEXT, SCENE_GOAL_TEXT, FILM_GOAL_TEXT, HOW_TO_FILL_TEXT,
    DIR_FIELD_RULE_TEXT, DIR_HINT_HEAD_TEXT, DIR_HINT_TAIL_TEXT,
    LIBRARY_HINT_HEAD_TEXT, JUDGE_ROLE_BRIEF, JUDGE_CRITERIA,
    DESCRIPTION_INSTRUCTIONS, DESCRIPTION_RETRY_HINT,
    VERIFY_INSTRUCTIONS, VERIFY_OUTPUT_FORMAT, VERIFY_RETRY_HINT,
    JUDGE_RETRY_HINT,
)
from tagplatform.capabilities.prompts.templates import CLOSED_RETRY_HINT
```

- 搬运映射（源常量/字面量 → 新常量，全部逐字）：
  - `mtg/prompts.py` 函数体字面量 → `GENERATION_ROLE_TEXT`（"你是影视内容分析专家…## 内容\n"）、`SCENE_GOAL_TEXT`/`FILM_GOAL_TEXT`（`_generation_instructions` 两分支 goal 整段）、`HOW_TO_FILL_TEXT`（"## 如何填写\n"整块，无参部分）、`DIR_FIELD_RULE_TEXT`（dir 字段规则行）、`DIR_HINT_HEAD_TEXT`/`DIR_HINT_TAIL_TEXT`（`_dir_hint_block` 头/尾说明）、`LIBRARY_HINT_HEAD_TEXT`（`_library_hint_block` 头）
  - `mtg/prompts.py` `JUDGE_ROLE_BRIEF`/`JUDGE_CRITERIA` → 同名搬运
  - `mtg/descriptions.py` `DESCRIPTION_INSTRUCTIONS`、`_RETRY_HINT` → `DESCRIPTION_INSTRUCTIONS`、`DESCRIPTION_RETRY_HINT`
  - `mtg/verify.py` `VERIFY_INSTRUCTIONS`、`VERIFY_OUTPUT_FORMAT`、`_RETRY_HINT` → 同名 + `VERIFY_RETRY_HINT`
  - `mtg/judge.py` `_RETRY_HINT` → `JUDGE_RETRY_HINT`
  - `closed/producer.py` `_RETRY_HINT` → `templates.py` 的 `CLOSED_RETRY_HINT`

- [ ] **Step 1: Write the failing test**

`tests/test_prompts_central.py`——常量存在性（RED=ImportError）+ 组装函数 wiring（containment，钉"服务真的在用中央常量"）+ 分层红线静态断言：

```python
"""Prompt 文案集中（capabilities/prompts）的 wiring 与分层红线测试。"""
from pathlib import Path

import pytest

from tagplatform.capabilities.prompts.mtg import (
    DIR_HINT_HEAD_TEXT, DIR_FIELD_RULE_TEXT, DESCRIPTION_INSTRUCTIONS,
    DESCRIPTION_RETRY_HINT, FILM_GOAL_TEXT, GENERATION_ROLE_TEXT,
    HOW_TO_FILL_TEXT, JUDGE_CRITERIA, JUDGE_RETRY_HINT, JUDGE_ROLE_BRIEF,
    LIBRARY_HINT_HEAD_TEXT, SCENE_GOAL_TEXT, VERIFY_INSTRUCTIONS,
    VERIFY_OUTPUT_FORMAT, VERIFY_RETRY_HINT,
)
from tagplatform.capabilities.prompts.templates import CLOSED_RETRY_HINT
from tagplatform.services.generation.open.mtg.descriptions import (
    build_description_template,
)
from tagplatform.services.generation.open.mtg.judge import build_judge_template
from tagplatform.services.generation.open.mtg.prompts import (
    build_generation_template, render_judge_messages,
)
from tagplatform.services.generation.open.mtg.verify import build_verify_template

_SRC = Path("src/tagplatform/capabilities/prompts")


def _layering_clean(module: str) -> None:
    text = (_SRC / module).read_text(encoding="utf-8")
    assert "fastapi" not in text
    assert "sqlalchemy" not in text
    assert "tagplatform.services" not in text


def test_mtg_module_layering():
    _layering_clean("mtg.py")


@pytest.mark.parametrize("text", [GENERATION_ROLE_TEXT, SCENE_GOAL_TEXT,
    HOW_TO_FILL_TEXT, DIR_FIELD_RULE_TEXT, DIR_HINT_HEAD_TEXT,
    LIBRARY_HINT_HEAD_TEXT, JUDGE_CRITERIA, JUDGE_ROLE_BRIEF,
    DESCRIPTION_INSTRUCTIONS, DESCRIPTION_RETRY_HINT, VERIFY_INSTRUCTIONS,
    VERIFY_OUTPUT_FORMAT, VERIFY_RETRY_HINT, JUDGE_RETRY_HINT,
    CLOSED_RETRY_HINT])
def test_constants_non_empty(text):
    assert text.strip()


def test_generation_template_uses_central_copy():
    template = build_generation_template("scene")
    first_text = template["messages"][0]["content"][0]["text"]
    assert GENERATION_ROLE_TEXT in first_text
    assert SCENE_GOAL_TEXT in template["instructions_without_directory_hint"]
    assert HOW_TO_FILL_TEXT in template["instructions_without_directory_hint"]
    film = build_generation_template("film")
    assert FILM_GOAL_TEXT in film["instructions_without_directory_hint"]
    all_text = "".join(p["text"] for p in film["messages"][0]["content"]
                       if p["type"] == "text")
    assert LIBRARY_HINT_HEAD_TEXT in all_text
    assert DIR_HINT_HEAD_TEXT in all_text


def test_judge_messages_use_central_copy():
    text = render_judge_messages("正文", "- 候选｜维度｜理由")[0]["content"]
    assert JUDGE_ROLE_BRIEF in text
    assert JUDGE_CRITERIA in text


def test_judge_retry_suffix_is_central():
    assert build_judge_template()["retry_suffix"] == JUDGE_RETRY_HINT


def test_verify_uses_central_copy():
    template = build_verify_template()
    content = template["messages"][0]["content"]
    assert VERIFY_INSTRUCTIONS in content
    assert VERIFY_OUTPUT_FORMAT in content
    assert template["retry_suffix"] == VERIFY_RETRY_HINT


def test_description_uses_central_copy():
    template = build_description_template()
    assert DESCRIPTION_INSTRUCTIONS in template["messages"][0]["content"]
    assert template["retry_suffix"] == DESCRIPTION_RETRY_HINT
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_prompts_central.py -q`
Expected: FAIL —— collection error `ImportError: cannot import name 'GENERATION_ROLE_TEXT'`（或 `No module named ... .mtg`）

- [ ] **Step 3: Create `capabilities/prompts/mtg.py` + 搬运五个服务模块**

`mtg.py` 模块 docstring 一段（说明：本模块是开放流水线指令文案的唯一权威源，逐字搬自 services 层五模块，git 即版本；与各解析端互为协议，改输出形状须两侧同改）。常量按 Interfaces 映射表逐字落位（含 `DESCRIPTION_INSTRUCTIONS`/`JUDGE_ROLE_BRIEF`/`JUDGE_CRITERIA`/`VERIFY_INSTRUCTIONS`/`VERIFY_OUTPUT_FORMAT` 五个已是常量的直接位移）。`templates.py` 追加 `CLOSED_RETRY_HINT`（`closed/producer.py` `_RETRY_HINT` 逐字）。五个服务模块：删本地定义、顶部 import 新常量、用点改名（`_RETRY_HINT` → `DESCRIPTION_RETRY_HINT`/`VERIFY_RETRY_HINT`/`JUDGE_RETRY_HINT`/`CLOSED_RETRY_HINT`）；`_generation_instructions`/`_generation_messages`/`_dir_hint_block`/`_library_hint_block` 函数体里的字面量替换为常量引用，f-string 结构与参数槽（`max_tags`/`scope`/`dir_field`/listing 行）不动。

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_prompts_central.py -q`
Expected: PASS（8 用例全绿）

- [ ] **Step 5: Full backend regression**

Run: `python -m pytest -q`
Expected: 927 + 新增用例全绿，0 fail（mock 咬合/parse 契约即搬运保真网）

- [ ] **Step 6: Commit**

```bash
git add src/tagplatform/capabilities/prompts/ src/tagplatform/services/generation/ tests/test_prompts_central.py
git commit -m "refactor: mtg/closed 指令文案集中到 capabilities/prompts（纯位移）"
```

---

### Task 2: assets + quality 搬运 + registry 端点

**Files:**
- Create: `src/tagplatform/capabilities/prompts/assets.py`、`quality.py`、`registry.py`
- Modify: `src/tagplatform/services/content/assets/builder.py`
- Modify: `src/tagplatform/services/eval/tag_quality.py`
- Modify: `src/tagplatform/api/routers/prompts.py`
- Test: `tests/test_prompt_registry.py`（新建）、`tests/test_prompts_central.py`（扩展）

**Interfaces:**
- Consumes: Task 1 的 `capabilities.prompts.mtg` / `templates` 常量
- Produces（Task 3 前端消费）:

```python
# capabilities/prompts/assets.py
SCENE_ANALYSIS_ROLE_TEXT: str    # builder.scene_messages 头（"你是影视内容分析助手…输出中文结构化分析。\n\n"）
SCENE_ANALYSIS_OUTPUT_TEXT: str   # scene_messages 尾（'只输出 JSON：{"description":...}' 整行）
SUMMARY_TASK_TEXT: str            # summary_messages 头（"把下面这部影片的英文剧情简介改写成中文整片剧情描述。\n"）
SUMMARY_REQUIREMENTS_TEXT: str    # summary_messages 要求段（"要求：200–300 字…不要音译。\n"）
SUMMARY_OUTPUT_TEXT: str          # summary_messages 尾（'只输出 JSON：{"summary": "中文剧情描述"}'）

# capabilities/prompts/quality.py
RUBRIC_TEXT: str                  # tag_quality._PROMPT 逐字
PROMPT_VERSION: str               # "tag-quality-v4"

# capabilities/prompts/registry.py
def list_prompt_registry() -> list[dict]:
    """四组只读清单，JSON 形状：[{"module","title","items":[{"name","text"}]}]。
    组：closed（"闭环生成"：CLOSED_TEMPLATE_TEXT/OPEN_TEMPLATE_TEXT/CLOSED_RETRY_HINT）
        mtg（"开放流水线"：Task 1 全部 16 常量）
        assets（"内容资产构建"：上述 5 常量）
        quality（"标签质量评估"：RUBRIC_TEXT/PROMPT_VERSION）"""

# api/routers/prompts.py 新端点
GET /api/v1/prompts/registry -> list[RegistryGroupOut]（RegistryItemOut{name,text} / RegistryGroupOut{module,title,items}）
```

- Rewire 细节：`tag_quality.py` 顶部 `from tagplatform.capabilities.prompts.quality import PROMPT_VERSION, RUBRIC_TEXT`，`_PROMPT` 删除、用点改 `RUBRIC_TEXT`（`tasks/tag_quality` 从本模块 import `PROMPT_VERSION` 经 import 再导出继续成立）；`builder.py` 头/尾字面量换常量引用，中间数据段（电影头/场景时间/已知角色/剧情/字幕/含情绪词表的要求块）不动。

- [ ] **Step 1: Write the failing test**

`tests/test_prompt_registry.py`：

```python
"""GET /api/v1/prompts/registry 契约 + assets/quality 搬运 wiring。"""
from tagplatform.capabilities.prompts import mtg as mtg_prompts
from tagplatform.capabilities.prompts.assets import (
    SCENE_ANALYSIS_OUTPUT_TEXT, SCENE_ANALYSIS_ROLE_TEXT,
    SUMMARY_OUTPUT_TEXT, SUMMARY_REQUIREMENTS_TEXT, SUMMARY_TASK_TEXT,
)
from tagplatform.capabilities.prompts.quality import PROMPT_VERSION, RUBRIC_TEXT
from tagplatform.services.content.assets.builder import (
    ScenePlan, scene_messages, summary_messages,
)
from tagplatform.services.eval.tag_quality import build_quality_template

# client 为 tests/conftest.py 的 pytest fixture（真 app + 临时库 + mock
# LLM/嵌入器），测试以函数参数注入：def test_x(client): ...

def test_registry_endpoint_contract(client):
    resp = client.get("/api/v1/prompts/registry")
    assert resp.status_code == 200
    groups = resp.json()
    assert {g["module"] for g in groups} == {"closed", "mtg", "assets", "quality"}
    for group in groups:
        assert group["title"]
        assert group["items"]
        for item in group["items"]:
            assert item["name"] and item["text"].strip()

def test_registry_closed_and_quality_content(client):
    groups = {g["module"]: g for g in client.get("/api/v1/prompts/registry").json()}
    closed_texts = [i["text"] for i in groups["closed"]["items"]]
    assert any("候选标签按目录分组" in t for t in closed_texts)      # CLOSED_TEMPLATE_TEXT 特征串
    quality_items = {i["name"]: i["text"] for i in groups["quality"]["items"]}
    assert quality_items["PROMPT_VERSION"] == "tag-quality-v4"
    assert "## 标签质量评估" in quality_items["RUBRIC_TEXT"]

def test_registry_mtg_items_match_constants(client):
    groups = {g["module"]: g for g in client.get("/api/v1/prompts/registry").json()}
    by_name = {i["name"]: i["text"] for i in groups["mtg"]["items"]}
    assert by_name["JUDGE_CRITERIA"] == mtg_prompts.JUDGE_CRITERIA  # 单一来源钉死
    assert by_name["SCENE_GOAL_TEXT"] == mtg_prompts.SCENE_GOAL_TEXT

def test_registry_does_not_break_prompt_contract_locks(client):
    assert client.post("/api/v1/prompts").status_code == 405

def test_summary_messages_use_central_copy():
    text = summary_messages("大都会", "1927", ["剧情"], "A romance...")[0]["content"]
    assert SUMMARY_TASK_TEXT in text and SUMMARY_REQUIREMENTS_TEXT in text
    assert SUMMARY_OUTPUT_TEXT in text

def test_scene_messages_use_central_copy():
    plan = ScenePlan(scene_id="scene_1", start_ms=0, end_ms=1000,
                     source_description="A scene", frames=[], dialogues=[])
    text = scene_messages("大都会", "1927", ["剧情"], ["Cast"], plan, [])[0]["content"]
    assert SCENE_ANALYSIS_ROLE_TEXT in text
    assert SCENE_ANALYSIS_OUTPUT_TEXT in text

def test_quality_template_uses_central_copy():
    content = build_quality_template()["messages"][0]["content"]
    assert RUBRIC_TEXT in content
```

（`RUBRIC_TEXT` import 已在文件头；全部 registry 用例以 `client` fixture 参数注入。）`tests/test_prompts_central.py` 追加：

```python
@pytest.mark.parametrize("module", ["assets.py", "quality.py", "registry.py"])
def test_central_modules_layering(module):
    _layering_clean(module)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_prompt_registry.py tests/test_prompts_central.py -q`
Expected: registry 用例 FAIL（404 或 ImportError）；`test_central_modules_layering` FAIL（文件不存在）

- [ ] **Step 3: Implement 三个常量模块 + rewire + 端点**

`assets.py`：从 `builder.py` `scene_messages`/`summary_messages` 提取五个无参文案段（逐字；场景要求块因嵌 `prompt_catalog(MOODS/FEELINGS)` 留原地）。`quality.py`：`RUBRIC_TEXT` = `_PROMPT` 逐字 + `PROMPT_VERSION = "tag-quality-v4"`。`registry.py`：`list_prompt_registry()` 按 Interfaces 组清单静态返回（import 本包 `.templates`/`.mtg`/`.assets`/`.quality`）。`api/routers/prompts.py`：`RegistryItemOut`/`RegistryGroupOut` 两 pydantic 模型 + `@router.get("/prompts/registry", response_model=list[RegistryGroupOut])` 返回 `list_prompt_registry()`；现有 `list_prompts` 函数与路由零改动。

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_prompt_registry.py tests/test_prompts_central.py -q`
Expected: PASS

- [ ] **Step 5: Full backend regression**

Run: `python -m pytest -q`
Expected: 全绿（含 `test_api_prompts_admin` 原样——契约锁未破；eval/tag_quality 相关用例绿——旧命名空间再导出生效）

- [ ] **Step 6: Commit**

```bash
git add src/tagplatform/capabilities/prompts/ src/tagplatform/services/ src/tagplatform/api/routers/prompts.py tests/
git commit -m "feat: assets/quality 文案集中 + GET /api/v1/prompts/registry 全量只读清单"
```

---

### Task 3: 前端配置页"流水线文案"区块

**Files:**
- Modify: `frontend/src/api/prompts.ts`（追加类型 + `fetchPromptRegistry`）
- Modify: `frontend/src/api/prompts.test.ts`（追加用例）
- Modify: `frontend/src/pages/config/ModelConfig.tsx`
- Test: `frontend/src/pages/config/ModelConfig.test.tsx`（扩展既有 mock）

**Interfaces:**
- Consumes: `GET /api/v1/prompts/registry`（Task 2 契约：`[{module,title,items:[{name,text}]}]`）
- Produces:

```ts
// api/prompts.ts
export interface PromptRegistryItem { name: string; text: string }
export interface PromptRegistryGroup { module: string; title: string; items: PromptRegistryItem[] }
export function fetchPromptRegistry(): Promise<PromptRegistryGroup[]>  // apiFetch('/api/v1/prompts/registry')
```

ModelConfig 在"Prompt 模板"Card 之下新增 `Card title="流水线文案"`：`Collapse`（或 antd Accordion 等价物）按 group 分组，group 头 `title`，每个 item 渲染常量名（Typography.Text strong）+ `pre-wrap` 文案（复用 PromptContractCard 的正文展示样式）。状态三态与既有 Prompt 区一致：`registry` null+无 error → loading；error → 区内局部 `Alert`（**不影响**其他区块）；空数组 → `暂无流水线文案`。加载入口与 `loadPrompts` 并列（`useEffect` 同批次触发）。

- [ ] **Step 1: Write the failing tests**

`prompts.test.ts` 追加（`stubFetch`/`jsonResponse` 是该文件既有 helper，直接复用；import 行补 `fetchPromptRegistry`）：

```ts
describe('fetchPromptRegistry 流水线文案清单', () => {
  it('GET /api/v1/prompts/registry 返回分组清单', async () => {
    const groups: PromptRegistryGroup[] = [
      { module: 'quality', title: '标签质量评估',
        items: [{ name: 'PROMPT_VERSION', text: 'tag-quality-v4' }] },
    ]
    const fetchMock = stubFetch(async () => jsonResponse(200, groups))

    const rows = await fetchPromptRegistry()

    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/v1/prompts/registry')
    expect(rows).toEqual(groups)
    expect(rows[0]?.items[0]?.text).toBe('tag-quality-v4')
  })
})
```

`ModelConfig.test.tsx` 追加两个用例。mock 装配扩展（该文件 `mockApis` 按路径分发、未预期路径抛 `未预期的请求`——页面新增 registry 请求后**既有用例全部会踩到**，必须同步加默认桩）：
- `MockOptions` 增 `registry?: PromptRegistryGroup[]` / `registryError?: Error`；夹具 `REGISTRY` 默认值含一组（`{ module: 'quality', title: '标签质量评估', items: [{ name: 'PROMPT_VERSION', text: 'tag-quality-v4' }] }`）
- `mockApis` 增分支 `path === '/api/v1/prompts/registry'`（error 优先，默认 `options.registry ?? REGISTRY`）

用例：
1. 成功：`mockApis()` 渲染后 `await screen.findByText('流水线文案')`，出现组标题"标签质量评估"与条目 `PROMPT_VERSION`、文案 `tag-quality-v4`
2. `mockApis({ registryError: new Error('boom') })`：区内出现加载失败提示（如 `findByText(/流水线文案加载失败/)`），且既有断言"Prompt 模板"Card 仍渲染（`screen.getByText('Prompt 模板 · 封闭生成（closed）')`）不回退

- [ ] **Step 2: Run tests to verify they fail**

Run: `bash -c "cd frontend && npx vitest run src/api/prompts.test.ts src/pages/config/ModelConfig.test.tsx"`
Expected: FAIL —— `fetchPromptRegistry` 不存在（TS 编译错/undefined）；"流水线文案"查询无命中

- [ ] **Step 3: Implement api 函数 + ModelConfig 区块**

按 Interfaces 实现（`apiFetch` 模式同 `fetchPrompts`；ModelConfig 区块结构与状态机同既有 Prompt 只读区，独立 state：`registry`/`registryError`）。

- [ ] **Step 4: Run tests to verify they pass**

Run: `bash -c "cd frontend && npx vitest run src/api/prompts.test.ts src/pages/config/ModelConfig.test.tsx"`
Expected: PASS

- [ ] **Step 5: Full frontend regression + build**

Run: `bash -c "cd frontend && npm test && npm run build"`
Expected: 257 + 新增全绿；tsc/vite build 通过

- [ ] **Step 6: Commit**

```bash
git add frontend/src/api/prompts.ts frontend/src/api/prompts.test.ts frontend/src/pages/config/ModelConfig.tsx frontend/src/pages/config/ModelConfig.test.tsx
git commit -m "feat: 配置页新增流水线文案只读区块（GET /prompts/registry）"
```

---

## 终末验收（reviewer 职责，非任务）

- 纯位移验收：Task 1/2 的每个搬运用 git diff 逐段核对——删侧与增侧字面量逐字节一致（含全角标点、`\n` 结构）
- Review Focus 五条逐条核对测试覆盖
- Global Constraints 分层红线（`registry.py` import 链全在 capabilities 内）
