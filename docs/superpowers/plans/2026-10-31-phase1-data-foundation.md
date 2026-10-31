# 影视智能标签平台 阶段 1（数据底座与导入治理）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建成平台数据底座：SQLite 8 表、统一内容 JSON 标准、Excel 标签库导入与治理流水线、内容导入服务及对应 REST API，交付"可导入标签库和已识别内容"。

**Architecture:** 核心逻辑为纯 Python 包 `tagplatform`（db/schemas/embed/vectors/adapters/governance/content_import，不 import FastAPI），API 层仅做编排。向量存 BLOB、numpy 余弦检索（数据量 ≤数千，无需向量扩展；sqlite-vec 集成留给阶段 2 评估）。SQLite WAL 模式 + Alembic 迁移。

**Tech Stack:** Python ≥3.11、FastAPI、SQLAlchemy ≥2.0、Alembic、Pydantic ≥2、openpyxl、sentence-transformers（intfloat/multilingual-e5-base）、numpy、pytest + httpx。

**Spec:** `intelligent-tag-platform/docs/superpowers/specs/2026-10-31-intelligent-tag-platform-design.md`（下称"设计文档"；接口路径与 JSON 示例另见原始提案 `intelligent-tag-platform/docs/design.md` §3）

本计划只覆盖设计文档 §11 的阶段 1；阶段 2-4 在前一阶段代码落地后另立计划。项目目录 `intelligent-tag-platform/` 目前只有 docs/，任务 1 负责建仓。

## Global Constraints

- 环境约定：Windows 开发、Linux 部署（Docker 属阶段 4）；Python ≥3.11。
- 依赖：fastapi、uvicorn、sqlalchemy≥2.0、alembic、pydantic≥2、openpyxl、sentence-transformers、numpy；dev 依赖 pytest、httpx。
- 包名 `tagplatform`，src 布局（`src/tagplatform/`），`pip install -e .` 安装。
- SQLite：WAL 模式 + `PRAGMA foreign_keys=ON`；DB 路径由 settings 注入，默认 `data/tagplatform.db`。
- 向量存储：BLOB = float32 little-endian 连续打包；相似度 = 余弦。
- Embedding：`intfloat/multilingual-e5-base`（768 维，L2 归一化，对称相似度场景不加 E5 前缀，沿用 MovieTagGen 用法）；测试一律用 MockEmbedder（确定性哈希向量，绝不触发模型下载/sentence_transformers import）。
- 语义相似阈值 `semantic_similar_threshold = 0.90`（settings 可配；**含等于**判为疑似同义）。
- 标签向量文本 = `f"{tag_name}。{description}"`。
- 分层红线：`tagplatform.db / schemas / embed / vectors / adapters / governance / content_import / settings` 不得 import fastapi（设计文档 §9.4）。
- review_status 枚举：pending / approved / rejected / merged；generation_task.mode：closed / open；tag_change_log 动作：create / update / merge / deprecate / import。
- asset_type 枚举：movie / series / scene。
- 语言约定：代码标识符英文，注释与文档中文。
- 配置环境变量前缀 `TAGPLT_`（如 `TAGPLT_DATABASE_PATH`）。
- git 仓库在任务 1 初始化；每个任务结束提交一次（feat:/test: 前缀）。

## Review Focus

1. **畸形 Excel**（空行、数字填入文本列、缺必需列头、别名分隔符中英文混用）→ 解析必须返回带行号的行级错误，绝不抛未捕获异常。→ 任务 6 测试 `test_excel_malformed_rows_*`。
2. **同一 Excel 重复导入** → 第二次导入所有行落入 `duplicate_name` issue，库内不重复插入，API 不返回 5xx。→ 任务 7 `test_rerun_same_excel_all_skipped`。
3. **非法时间戳**（end_ms < start_ms、负数）→ 内容 JSON 校验直接拒绝。→ 任务 4 参数化 `test_invalid_rejected`。
4. **video_root 不存在** → 内容导入返回 422 明确报错；video_root 合法但个别视频文件缺失时导入照常成功（播放 404 属阶段 3 范畴）。→ 任务 9 `test_import_video_root_missing`。
5. **别名跨标签冲突**（A 的别名等于 B 的名称；批内两条互为别名）→ `alias_conflict` issue 进待处理，不插入。→ 任务 7 `test_alias_cross_conflict`。

## 文件结构总览

```
intelligent-tag-platform/
├── pyproject.toml            # 任务 1
├── .gitignore                # 任务 1（data/、__pycache__/、.venv/、*.db、dist/）
├── alembic.ini               # 任务 2
├── alembic/env.py、versions/ # 任务 2 骨架、任务 3 首个迁移
├── src/tagplatform/
│   ├── settings.py           # 任务 1
│   ├── db/__init__.py        # 任务 2：engine/session 工厂
│   ├── db/models.py          # 任务 3：8 张表
│   ├── schemas/content.py    # 任务 4
│   ├── embed/__init__.py     # 任务 5：make_embedder
│   ├── embed/embedding.py    # 任务 5：Mock/Local
│   ├── vectors.py            # 任务 5：BLOB 编解码 + 余弦检索
│   ├── governance/excel.py   # 任务 6
│   ├── governance/pipeline.py# 任务 7
│   ├── adapters/base.py      # 任务 8：协议 + 注册表
│   ├── adapters/platform.py  # 任务 8
│   ├── content_import.py     # 任务 9
│   └── api/
│       ├── __init__.py       # 任务 1：create_app
│       └── routers/health.py # 任务 1
│       └── routers/tags.py   # 任务 10
│       └── routers/batches.py# 任务 10
│       └── routers/content.py# 任务 11
├── tests/
│   ├── conftest.py           # 任务 1：app/db fixture；任务 5 起补 mock embedder
│   ├── fixtures/content_movie.json  # 任务 4（取自设计文档 §3 示例）
│   └── test_*.py             # 各任务对应
└── docs/（已存在）
```

---

### Task 1: 项目脚手架与 /health

**Files:**
- Create: `pyproject.toml`、`.gitignore`、`src/tagplatform/__init__.py`、`src/tagplatform/settings.py`、`src/tagplatform/api/__init__.py`、`src/tagplatform/api/routers/health.py`、`tests/conftest.py`、`tests/test_health.py`

**Interfaces:**
- Produces: `class Settings(BaseSettings)`，字段 `database_path: str = "data/tagplatform.db"`、`embedding_model: str = "intfloat/multilingual-e5-base"`、`use_mock_embedder: bool = False`、`semantic_similar_threshold: float = 0.90`、`allowed_tag_types: str = ""`（逗号分隔白名单，空 = 不校验）；`get_settings() -> Settings`（lru_cache）。`create_app(settings: Settings | None = None) -> FastAPI`。后续所有任务经 `get_settings()` 取配置、经 `create_app()` 构建 app。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_health.py
def test_health_ok(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
```

conftest 提供 `client` fixture：`httpx.Client(transport=ASGITransport(app=create_app(Settings(database_path=":memory:"))))`。

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_health.py -v`
Expected: FAIL（`tagplatform` 不存在 / ImportError）

- [ ] **Step 3: 实现脚手架**

pyproject：`[project] name="tagplatform"`，deps 见 Global Constraints，`[tool.pytest.ini_options] testpaths=["tests"]`，`[build-system] setuptools`，`packages=[{include="tagplatform", from="src"}]` 等价配置。settings.py 用 `pydantic-settings`，前缀 `TAGPLT_`。`create_app` 装配 health 路由。
执行 `pip install -e .[dev]`（dev extras: pytest、httpx）。

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_health.py -v`
Expected: PASS

- [ ] **Step 5: 建仓并提交**

```bash
git init && git add -A && git commit -m "feat: 项目脚手架与 /health"
```

---

### Task 2: 数据库引擎、会话工厂与 Alembic 骨架

**Files:**
- Create: `src/tagplatform/db/__init__.py`、`alembic.ini`、`alembic/env.py`、`alembic/versions/.gitkeep`、`tests/test_db.py`

**Interfaces:**
- Consumes: `get_settings()`
- Produces: `create_engine_for(path: str) -> Engine`（连 `:memory:` 或文件路径；文件库确保 `data/` 目录存在）；`make_session_factory(engine: Engine) -> sessionmaker[Session]`。engine 每连接强制 WAL + `foreign_keys=ON`（用 `event.listens_for(engine, "connect")`）。alembic env.py 从 settings 读 DB 路径、target_metadata 指向 `tagplatform.db.models.Base.metadata`。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_db.py
def test_engine_wal_and_fk(tmp_path):
    engine = create_engine_for(str(tmp_path / "t.db"))
    with engine.connect() as c:
        assert c.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
        assert c.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
```

- [ ] **Step 2: 运行确认失败** — Run: `python -m pytest tests/test_db.py -v`，Expected: FAIL（ImportError）

- [ ] **Step 3: 实现** — SQLAlchemy 2.0 风格；alembic.ini 不硬编码 URL（env.py 动态取）。

- [ ] **Step 4: 运行确认通过** — Run: `python -m pytest tests/test_db.py -v`，Expected: PASS

- [ ] **Step 5: 提交** — `git add -A && git commit -m "feat: SQLite 引擎（WAL/FK）与 alembic 骨架"`

---

### Task 3: 8 张表 ORM 与初始迁移

**Files:**
- Create: `src/tagplatform/db/models.py`、`alembic/versions/0001_initial_tables.py`（autogenerate 产出）、`tests/test_models.py`

**Interfaces:**
- Consumes: `create_engine_for`、`make_session_factory`
- Produces: `Base(DeclarativeBase)` 与 8 个模型类（表列即下述契约，后续任务按属性名读写）：

```
media_asset:   asset_id TEXT PK, asset_type TEXT NOT NULL(movie|series|scene),
               parent_id TEXT FK->media_asset NULL, title TEXT NOT NULL,
               duration_ms INTEGER NULL, video_uri TEXT NULL, created_at DATETIME
media_context: context_id TEXT PK, asset_id TEXT FK NOT NULL, content_json TEXT NOT NULL,
               embedding BLOB NULL, source TEXT, model_version TEXT,
               recognition_status TEXT, created_at DATETIME; Index(asset_id)
tag_catalog:   tag_id TEXT PK, tag_name TEXT NOT NULL UNIQUE, tag_type TEXT NOT NULL,
               description TEXT NOT NULL, parent_id TEXT FK->tag_catalog NULL,
               aliases TEXT(JSON 数组字符串) NULL, applicable_scope TEXT NULL,
               status TEXT DEFAULT 'active', embedding BLOB NULL,
               created_at DATETIME, updated_at DATETIME
generation_task: task_id TEXT PK, context_id TEXT FK NULL, mode TEXT NOT NULL(closed|open),
               model_version TEXT, prompt_version TEXT, policy_json TEXT,
               status TEXT NOT NULL(queued|running|succeeded|failed),
               error TEXT NULL, created_at DATETIME, finished_at DATETIME NULL
asset_tag:     id INTEGER PK, asset_id TEXT FK NOT NULL, tag_id TEXT FK NULL,
               candidate_name TEXT NULL, candidate_type TEXT NULL,
               candidate_description TEXT NULL, origin TEXT NOT NULL(catalog|generated),
               relevance_score REAL NULL, confidence REAL NULL,
               evidence_json TEXT NULL, review_status TEXT NOT NULL DEFAULT 'pending',
               task_id TEXT FK->generation_task NULL, created_at, updated_at DATETIME
               + 两个部分唯一索引（见 Step 3）
review_record: record_id TEXT PK, result_id INTEGER FK->asset_tag.id NOT NULL,
               action TEXT NOT NULL(approve|reject|modify|merge), reviewer_id TEXT NULL,
               before_json TEXT NULL, after_json TEXT NULL, comment TEXT NULL, created_at
import_batch:  batch_id TEXT PK, file_name TEXT NOT NULL, status TEXT NOT NULL
               (processing|completed|failed), result_summary TEXT NULL(JSON), created_at
tag_change_log: log_id INTEGER PK, tag_id TEXT NOT NULL,
               action TEXT NOT NULL(create|update|merge|deprecate|import),
               before_json TEXT NULL, after_json TEXT NULL, operator TEXT NULL,
               created_at DATETIME; Index(tag_id)
```

- [ ] **Step 1: 写失败测试**

```python
# tests/test_models.py — conftest 提供 session（metadata.create_all 建表）
def test_tag_catalog_roundtrip(session):
    t = TagCatalog(tag_id="tag_x", tag_name="悬疑", tag_type="genre",
                   description="以悬念和谜题为核心", aliases='["悬念"]')
    session.add(t); session.commit()
    got = session.get(TagCatalog, "tag_x")
    assert got.aliases == '["悬念"]' and got.status == "active"

def test_asset_tag_partial_unique(session):
    session.add_all([AssetTag(asset_id="a", tag_id="t1", origin="catalog"),
                     AssetTag(asset_id="a", tag_id="t1", origin="catalog")])
    with pytest.raises(IntegrityError): session.commit()

def test_asset_tag_candidate_unique(session):
    session.add_all([AssetTag(asset_id="a", origin="generated", candidate_name="新词"),
                     AssetTag(asset_id="a", origin="generated", candidate_name="新词")])
    with pytest.raises(IntegrityError): session.commit()
```

（conftest 新增 `session` fixture：`create_engine_for(":memory:")` + `Base.metadata.create_all` + 每测试回滚/重建。）

- [ ] **Step 2: 运行确认失败** — Run: `python -m pytest tests/test_models.py -v`，Expected: FAIL（ImportError）

- [ ] **Step 3: 实现模型 + 生成迁移**

部分唯一索引用 `Index("ux_asset_tag_catalog", "asset_id", "tag_id", sqlite_where=text("tag_id IS NOT NULL"))` 与 `Index("ux_asset_tag_candidate", "asset_id", "candidate_name", sqlite_where=text("tag_id IS NULL AND candidate_name IS NOT NULL"))`（重跑语义：approved 保留、pending 覆盖由阶段 2 的服务层实现，索引只保证不重复）。
生成迁移：`TAGPLT_DATABASE_PATH=<tmp>/m.db alembic revision --autogenerate -m "initial tables"`。

- [ ] **Step 4: 运行确认通过（含迁移可升级）**

```python
# tests/test_models.py 追加
def test_alembic_upgrade(tmp_path):
    # 子进程跑 alembic upgrade head，env 传 TAGPLT_DATABASE_PATH 指向 tmp db
    # 升级后用 sqlite3 查 sqlite_master，断言 8 张表全部存在
```

Run: `python -m pytest tests/test_models.py -v`，Expected: PASS

- [ ] **Step 5: 提交** — `git add -A && git commit -m "feat: 8 张核心表 ORM 与初始迁移"`

---

### Task 4: 统一内容 JSON 模型

**Files:**
- Create: `src/tagplatform/schemas/content.py`、`src/tagplatform/schemas/__init__.py`、`tests/fixtures/content_movie.json`、`tests/test_content_schema.py`

**Interfaces:**
- Consumes: 无（纯 Pydantic）
- Produces: `parse_content(data: dict) -> ContentJSON`（校验失败抛 `pydantic.ValidationError`）。模型树：`ContentJSON{schema_version: Literal["1.0"], asset: AssetInfo, content: ContentBody, metadata: MetaInfo}`；`AssetInfo{asset_id: str | None, asset_type: Literal["movie","series","scene"], title: str, duration_ms: int ≥ 0 | None, video_uri: str | None, parent_id: str | None}`；`ContentBody{summary: str, scenes: list[Scene]}`；`Scene{scene_id: str, time_range: TimeRange, description: str, visual_features: list[str] = [], dialogues: list[Dialogue] = [], entities: list[Entity] = [], events: list[Event] = []}`；`TimeRange{start_ms: int ≥ 0, end_ms: int ≥ 0}`（model_validator：`end_ms ≥ start_ms`）；`Dialogue{speaker, text, start_ms, end_ms}`（同约束）；`Entity{entity_id, name, type}`；`Event{event_type, description}`；`MetaInfo{source, model_version | None, recognition_status}`。

- [ ] **Step 1: 写失败测试**

fixture `content_movie.json` = 设计文档 §3.3.1 的完整示例 JSON 原文。

```python
def test_spec_example_valid():
    data = json.loads(Path("tests/fixtures/content_movie.json").read_text(encoding="utf-8"))
    c = parse_content(data)
    assert c.asset.asset_id == "movie_001" and c.content.scenes[0].dialogues[0].speaker == "角色A"

@pytest.mark.parametrize("mutate", [
    {"path": ("content","scenes",0,"time_range","end_ms"), "value": 1000},   # end<start
    {"path": ("asset","duration_ms"), "value": -1},
    {"path": ("asset","asset_type"), "value": "clip"},
    {"path": ("schema_version",), "value": "0.9"},
])
def test_invalid_rejected(mutate):
    # 深拷贝 fixture 按 path 覆写后 parse_content 应抛 ValidationError
```

- [ ] **Step 2: 运行确认失败** — Run: `python -m pytest tests/test_content_schema.py -v`，Expected: FAIL（ImportError）

- [ ] **Step 3: 实现** — Pydantic v2，字段约束按 Interfaces 契约；不加多余校验（场景数可为 0，空字符串交由后续治理判断）。

- [ ] **Step 4: 运行确认通过** — Run: `python -m pytest tests/test_content_schema.py -v`，Expected: PASS

- [ ] **Step 5: 提交** — `git add -A && git commit -m "feat: 统一内容 JSON 模型（schema v1.0）"`

---

### Task 5: Embedder 与向量编解码检索

**Files:**
- Create: `src/tagplatform/embed/__init__.py`、`src/tagplatform/embed/embedding.py`、`src/tagplatform/vectors.py`、`tests/test_embedding.py`、`tests/test_vectors.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `make_embedder(model_name: str, mock: bool) -> Embedder`，契约：`.name: str`、`.dim: int | None`、`.embed(texts: list[str]) -> np.ndarray (n, d) float32 L2 归一化`。`MockEmbedder`（name="mock"，dim=64，sha256 符号向量，规范化同形同向量、`sys.modules` 不出现 `sentence_transformers`）与 `LocalEmbedder`（惰性加载 SentenceTransformer、空输入绝不加载）——实现方式照抄 MovieTagGen `align/embedding.py` 的同名三件套中 Mock/Local 两件（去掉磁盘缓存与 ApiEmbedder）。
  - `encode_vector(vec: np.ndarray) -> bytes` / `decode_vector(blob: bytes) -> np.ndarray`（float32 little-endian `.tobytes()` / `np.frombuffer`）
  - `search_similar(query: np.ndarray, candidates: list[tuple[str, np.ndarray]], top_k: int) -> list[tuple[str, float]]`（余弦降序，返回前 top_k；query 与候选须同维）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_embedding.py
def test_mock_deterministic_and_no_download():
    e = make_embedder("intfloat/multilingual-e5-base", mock=True)
    a, b = e.embed(["悬疑 推理"]), e.embed(["悬疑  推理 "])
    assert a.shape == (1, 64) and np.allclose(a, b)
    assert np.isclose(np.linalg.norm(a[0]), 1.0)
    assert "sentence_transformers" not in sys.modules

def test_mock_empty_never_loads():
    e = make_embedder("x", mock=True)
    assert e.embed([]).shape == (0, 64)

# tests/test_vectors.py
def test_vector_roundtrip_and_search():
    v = np.array([0.1, 0.2, 0.3], dtype=np.float32)
    assert np.allclose(decode_vector(encode_vector(v)), v)
    hits = search_similar(np.array([1.0, 0.0, 0.0], dtype=np.float32),
                          [("a", np.array([1, 0, 0], np.float32)),
                           ("b", np.array([0, 1, 0], np.float32))], top_k=2)
    assert hits[0][0] == "a" and hits[0][1] == pytest.approx(1.0)
```

- [ ] **Step 2: 运行确认失败** — Run: `python -m pytest tests/test_embedding.py tests/test_vectors.py -v`，Expected: FAIL

- [ ] **Step 3: 实现** — 复用 MovieTagGen 实现结构；`search_similar` 用矩阵点积（已归一化即余弦）。

- [ ] **Step 4: 运行确认通过** — Run: `python -m pytest tests/test_embedding.py tests/test_vectors.py -v`，Expected: PASS

- [ ] **Step 5: 提交** — `git add -A && git commit -m "feat: embedder 契约与向量编解码/余弦检索"`

---

### Task 6: Excel 标签模板解析

**Files:**
- Create: `src/tagplatform/governance/__init__.py`、`src/tagplatform/governance/excel.py`、`tests/test_excel_parse.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `@dataclass TagRow`: `tag_id: str | None, tag_name: str, tag_type: str, description: str, parent_tag: str | None, aliases: list[str], applicable_scope: str | None, status: str | None`
  - `@dataclass RowError`: `row_number: int, tag_name: str | None, code: str, detail: str`（code ∈ missing_field / bad_type / bad_alias）
  - `parse_tag_excel(data: bytes) -> ExcelParseResult`（`.rows: list[TagRow]`, `.errors: list[RowError]`, `.header_error: str | None`）
  - 解析规则：首行为表头，必需列 `tag_name/tag_type/description`，其余 5 列可选（缺可选列头不报错）；空行跳过；`aliases` 单元格按中英文逗号/分号（`,，;；`）分隔去空；数字单元格转字符串；status 缺省由入库层定；`header_error` 非空时 rows/errors 为空。

- [ ] **Step 1: 写失败测试**

测试内用 openpyxl 现建 xlsx 字节流（helper `build_xlsx(rows)`）。

```python
def test_valid_template():
    r = parse_tag_excel(build_xlsx([
        ["tag_name", "tag_type", "description", "parent_tag", "aliases", "status"],
        ["悬疑", "genre", "以悬念为核心", "剧情", "悬念,推理", "active"],
    ]))
    assert r.rows[0].aliases == ["悬念", "推理"] and r.rows[0].parent_tag == "剧情"

def test_excel_malformed_rows_reported():
    r = parse_tag_excel(build_xlsx(HEADERS + [
        ["悬疑", "genre", "以悬念为核心"],              # 合法
        [None, "genre", "缺名称"],                      # 行级 missing_field(tag_name)
        ["数量", "genre"],                              # 行级 missing_field(description)
        ["风格", "genre", "d", "aliases列乱填数字", 123],  # bad_type→aliases 数字转字符串"123"
        [None, None, None],                             # 空行跳过
    ]))
    assert len(r.rows) == 3 and {e.code for e in r.errors} == {"missing_field"}
    assert r.errors[0].row_number == 3 and r.errors[0].tag_name is None

def test_header_missing_required():
    r = parse_tag_excel(build_xlsx([["tag_name", "tag_type"]]))
    assert r.header_error and not r.rows
```

- [ ] **Step 2: 运行确认失败** — Run: `python -m pytest tests/test_excel_parse.py -v`，Expected: FAIL

- [ ] **Step 3: 实现** — openpyxl `load_workbook(io.BytesIO(data))`；逐行收集错误不中断。

- [ ] **Step 4: 运行确认通过** — Run: `python -m pytest tests/test_excel_parse.py -v`，Expected: PASS

- [ ] **Step 5: 提交** — `git add -A && git commit -m "feat: Excel 标签模板解析（行级错误）"`

---

### Task 7: 标签治理流水线

**Files:**
- Create: `src/tagplatform/governance/pipeline.py`、`tests/test_governance.py`

**Interfaces:**
- Consumes: `TagRow`、`make_embedder`（mock 测试）、`encode_vector`、`search_similar`、`session` fixture、`TagCatalog / ImportBatch / TagChangeLog` 模型、`get_settings()`
- Produces: `run_governance(rows: list[TagRow], session: Session, embedder) -> GovernanceResult`，其中 `@dataclass Issue: tag_name: str, code: str, detail: str, similar_to: str | None`，code ∈ `duplicate_name / alias_conflict / semantic_similar / parent_not_found / parent_cycle / tag_type_invalid`；`@dataclass GovernanceResult: batch_id: str, inserted: list[str], issues: list[Issue]`。副作用：写 `tag_catalog`（含 embedding、aliases JSON 串）、写 `import_batch`（status=completed、result_summary=JSON：total/inserted/skipped/issues）、每条插入写 `tag_change_log(action="import")`。**一行的多种问题只报首个命中的 code，按上述顺序判定，判定即停（不插入）。**

处理规则（按行，两遍法）：
1. `duplicate_name`：tag_name 等于库内已有（status≠deprecate）或批内更早行的名称 → detail 注明"已存在 tag_id"或"批内重复"。
2. `alias_conflict`：aliases 任一项等于库内/批内其他行的 tag_name，或批内两行共享同一 alias。
3. `tag_type_invalid`：settings.allowed_tag_types 非空且 tag_type 不在白名单。
4. `semantic_similar`：对 `f"{tag_name}。{description}"` embed，与库内已有标签向量（含本批已通过校验的行）余弦 **≥ threshold（含等于）** → similar_to 记对端 tag_name，不插入。
5. `parent_not_found`：parent_tag 名称既不在库内也不在批内有效行。
6. `parent_cycle`：批内 parent 链成环（库内已有 parent 链不会成环，只查批内新增边）。
7. 通过全部检查 → 第二遍统一插入（parent_id 解析库内或批内 tag_id；批内嵌套自动拓扑序插入）。

- [ ] **Step 1: 写失败测试**

```python
def test_clean_rows_inserted_with_embedding_and_log(session):
    res = run_governance([TagRow(None, "悬疑", "genre", "以悬念与谜题为核心", None, ["悬念"], None, None)], session, MOCK_EMBEDDER)
    assert res.inserted and not res.issues
    t = session.get(TagCatalog, res.inserted[0])
    assert t.aliases == '["悬念"]' and t.embedding is not None and t.parent_id is None
    log = session.query(TagChangeLog).filter_by(tag_id=t.tag_id).one()
    assert log.action == "import"
    batch = session.get(ImportBatch, res.batch_id)
    assert batch.status == "completed" and json.loads(batch.result_summary)["inserted"] == 1

def test_rerun_same_excel_all_skipped(session):
    rows = [TagRow(None, "悬疑", "genre", "以悬念为核心", None, [], None, None)]
    r1 = run_governance(rows, session, MOCK_EMBEDDER)
    r2 = run_governance(rows, session, MOCK_EMBEDDER)
    assert r2.inserted == [] and r2.issues[0].code == "duplicate_name"
    assert session.query(TagCatalog).count() == 1

def test_alias_cross_conflict(session):
    # 库内：别名撞已有名称
    run_governance([TagRow(None, "推理", "genre", "以解谜推进", None, [], None, None)], session, MOCK_EMBEDDER)
    res = run_governance([TagRow(None, "悬疑", "genre", "d", None, ["推理"], None, None)], session, MOCK_EMBEDDER)
    assert res.issues[0].code == "alias_conflict" and res.inserted == []
    # 批内：两行共享同一别名 → 后行（或两行均）报 alias_conflict，不插入
    res2 = run_governance([
        TagRow(None, "甲", "genre", "d1", None, ["共享词"], None, None),
        TagRow(None, "乙", "genre", "d2", None, ["共享词"], None, None)], session, MOCK_EMBEDDER)
    assert res2.inserted == [] and any(i.code == "alias_conflict" for i in res2.issues)

def test_semantic_threshold_inclusive(session):
    # MockEmbedder 同形文本同向量 → "推理" vs "推理" 余弦 1.0 ≥ 0.90
    run_governance([TagRow(None, "推理", "genre", "以解谜推进", None, [], None, None)], session, MOCK_EMBEDDER)
    res = run_governance([TagRow(None, "推理", "style", "以解谜推进", None, [], None, None)], session, MOCK_EMBEDDER)
    assert res.issues[0].code == "semantic_similar" and res.issues[0].similar_to == "推理"

def test_parent_missing_and_cycle(session):
    res = run_governance([TagRow(None, "子", "genre", "d", "不存在的父", None, None, None)], session, MOCK_EMBEDDER)
    assert res.issues[0].code == "parent_not_found"
    res2 = run_governance([
        TagRow(None, "甲", "genre", "d1", "乙", None, None, None),
        TagRow(None, "乙", "genre", "d2", "甲", None, None, None)], session, MOCK_EMBEDDER)
    assert {i.code for i in res2.issues} == {"parent_cycle"}

def test_batch_parent_chain_inserts(session):
    res = run_governance([
        TagRow(None, "剧情", "genre", "叙事驱动", None, None, None, None),
        TagRow(None, "悬疑", "genre", "以悬念为核心", "剧情", None, None, None)], session, MOCK_EMBEDDER)
    child = next(t for t in session.query(TagCatalog).all() if t.tag_name == "悬疑")
    parent = next(t for t in session.query(TagCatalog).all() if t.tag_name == "剧情")
    assert child.parent_id == parent.tag_id
```

（conftest 新增 `MOCK_EMBEDDER = make_embedder("mock", mock=True)`。）

- [ ] **Step 2: 运行确认失败** — Run: `python -m pytest tests/test_governance.py -v`，Expected: FAIL

- [ ] **Step 3: 实现** — 纯函数式两遍：第一遍逐行按顺序判 issue 并计算"有效行集"，第二遍按 parent 拓扑序插入。批内 tag_id 规则：Excel 提供则用，否则 `tag_` + uuid4().hex[:8]。插入时 `embedding=encode_vector(embedder.embed([f"{name}。{desc}"])[0])`。

- [ ] **Step 4: 运行确认通过** — Run: `python -m pytest tests/test_governance.py -v`，Expected: PASS

- [ ] **Step 5: 提交** — `git add -A && git commit -m "feat: 标签治理流水线（校验/语义/层级/入库/追溯）"`

---

### Task 8: ContentAdapter 协议、注册表与平台格式适配器

**Files:**
- Create: `src/tagplatform/adapters/__init__.py`、`src/tagplatform/adapters/base.py`、`src/tagplatform/adapters/platform.py`、`tests/test_adapters.py`

**Interfaces:**
- Consumes: `parse_content`
- Produces:
  - `class ContentAdapter(Protocol): kind: str; def adapt(self, raw: dict) -> ContentJSON`（校验失败抛 ValidationError）
  - `register_adapter(adapter: ContentAdapter) -> None`（重复 kind 抛 ValueError）
  - `get_adapter(kind: str) -> ContentAdapter`（未注册抛 KeyError，信息列出可用 kind）
  - `list_adapters() -> list[str]`
  - `PlatformAdapter(kind="platform")`：`adapt(raw) = parse_content(raw)`（平台格式即标准格式，identity）；包导入时自动注册。
  - `src/tagplatform/adapters/__init__.py` 导出上述四个名字。

- [ ] **Step 1: 写失败测试**

```python
def test_platform_adapter_roundtrip():
    a = get_adapter("platform")
    c = a.adapt(json.loads(FIXTURE.read_text(encoding="utf-8")))
    assert c.asset.title == "示例电影"

def test_registry_duplicate_and_unknown():
    register_adapter(PlatformAdapter())  # kind="platform" 已自动注册 → ValueError
    with pytest.raises(KeyError): get_adapter("nope")
```

- [ ] **Step 2: 运行确认失败** — Run: `python -m pytest tests/test_adapters.py -v`，Expected: FAIL

- [ ] **Step 3: 实现** — Protocol + 模块级 dict 注册表（阶段 1 单进程足够，不做插件发现）。

- [ ] **Step 4: 运行确认通过** — Run: `python -m pytest tests/test_adapters.py -v`，Expected: PASS

- [ ] **Step 5: 提交** — `git add -A && git commit -m "feat: ContentAdapter 协议与平台格式适配器"`

---

### Task 9: 内容导入服务

**Files:**
- Create: `src/tagplatform/content_import.py`、`tests/test_content_import.py`

**Interfaces:**
- Consumes: `ContentJSON`、`MediaAsset / MediaContext` 模型、`session` fixture
- Produces: `import_content(content: ContentJSON, session: Session, video_root: str | None) -> ImportResult`，`@dataclass ImportResult: asset_id: str, context_id: str, scene_asset_ids: list[str]`。副作用与规则：
  - **video_root 校验**：非 None 时必须存在（`Path.is_dir()`），否则抛 `ValueError("video_root 不存在: ...")`（API 层转 422）。
  - **video 路径解析** `resolve_video_path(video_uri: str, video_root: str | None) -> str`：video_uri 为 `file://` URI → 取路径部分；为绝对路径 → 原样；其余 → `str(Path(video_root) / video_uri)`；video_root 为 None → 原样返回。
  - **asset_id**：`content.asset.asset_id` 非空则用之并 **upsert**（存在则更新 title/duration_ms/video_uri，不动 parent）；为空则生成 `asset_` + uuid4().hex[:8]。
  - **场景资产**：每 scene 建 `MediaAsset(asset_id=f"{movie_asset_id}::{scene_id}", asset_type="scene", parent_id=movie_asset_id, duration_ms=end-start, video_uri=同影片)`，重复导入时 upsert。
  - **media_context**：每次导入**新建一行**（context_id = `ctx_` + uuid4().hex[:8]，content_json=原始 JSON 序列化、source/model_version/recognition_status 取 metadata），embedding 留空（生产任务写入）。

- [ ] **Step 1: 写失败测试**

```python
def test_import_creates_hierarchy_and_context(session, content_fixture):
    res = import_content(content_fixture, session, video_root=None)
    movie = session.get(MediaAsset, res.asset_id)
    assert movie.asset_type == "movie" and len(res.scene_asset_ids) == 1
    scene = session.get(MediaAsset, res.scene_asset_ids[0])
    assert scene.parent_id == res.asset_id and scene.asset_type == "scene"
    ctx = session.get(MediaContext, res.context_id)
    assert ctx.asset_id == res.asset_id and ctx.embedding is None

def test_reimport_new_context_upserts_assets(session, content_fixture):
    r1 = import_content(content_fixture, session, None)
    content_fixture.asset.video_uri = "file:///data/movie_002.mp4"
    r2 = import_content(content_fixture, session, None)
    assert r2.asset_id == r1.asset_id and r2.context_id != r1.context_id
    assert session.query(MediaAsset).filter_by(parent_id=r1.asset_id).count() == 1  # 场景不重复
    assert session.query(MediaContext).count() == 2
    # file:// URI 无论 video_root 与否都解析为路径部分（任务接口契约）
    assert session.get(MediaAsset, r1.asset_id).video_uri == "/data/movie_002.mp4"

def test_import_video_root_missing(session, content_fixture):
    with pytest.raises(ValueError, match="video_root"):
        import_content(content_fixture, session, video_root="Z:/不存在")

def test_resolve_video_path_rules(tmp_path):
    assert resolve_video_path("file:///data/a.mp4", None) == "/data/a.mp4"
    assert resolve_video_path("/abs/a.mp4", str(tmp_path)) == "/abs/a.mp4"
    assert resolve_video_path("rel/a.mp4", str(tmp_path)) == str(tmp_path / "rel" / "a.mp4")
```

（conftest 新增 `content_fixture`：解析 fixtures/content_movie.json。）video_uri 示例已是 `file:///data/movie_001.mp4`，video_root=None 路径直接保留。

- [ ] **Step 2: 运行确认失败** — Run: `python -m pytest tests/test_content_import.py -v`，Expected: FAIL

- [ ] **Step 3: 实现** — 注意 `::` 拼接的 scene asset_id 与 asset_id 变更时的级联（asset_id 不允许变更——upsert 只更新非键字段）。

- [ ] **Step 4: 运行确认通过** — Run: `python -m pytest tests/test_content_import.py -v`，Expected: PASS

- [ ] **Step 5: 提交** — `git add -A && git commit -m "feat: 内容导入服务（层级/上下文/视频路径映射）"`

---

### Task 10: API——标签导入、批次与标签查询

**Files:**
- Create: `src/tagplatform/api/routers/tags.py`、`src/tagplatform/api/routers/batches.py`、`tests/test_api_tags.py`
- Modify: `src/tagplatform/api/__init__.py`（注册路由）

**Interfaces:**
- Consumes: `parse_tag_excel`、`run_governance`、`session`/`Settings`（经 FastAPI 依赖 `get_session`、`get_settings_dep` 注入，tests 用 `dependency_overrides`）
- Produces（路由，全部挂 `create_app`）：
  - `POST /api/v1/tags/import`：multipart 字段 `file`（Excel 字节）→ `run_governance(parse_tag_excel(file).rows, ...)` → `{"batch_id", "summary": {total, inserted, skipped, issues: [...]}}`；`header_error` 非空 → 422 `{"detail": header_error}`。embedder 经依赖 `get_embedder()` 提供（app 内按 settings.use_mock_embedder 构建一次，单例）。
  - `GET /api/v1/import/batches`：`[{batch_id, file_name, status, created_at}]`，按 created_at 降序。
  - `GET /api/v1/import/batches/{batch_id}`：含 `result_summary` 解析后的 issues。
  - `GET /api/v1/tags?type=&q=`：`tag_catalog` 列表（q 对 tag_name 与 aliases 子串匹配；返回 tag_id/tag_name/tag_type/description/parent_id/status），分页 `limit/offset`（默认 100/0）。
  - `GET /api/v1/tags/{tag_id}`：单条详情含 aliases 列表。
  - 错误约定：未知 batch/tag → 404。

- [ ] **Step 1: 写失败测试**

```python
def test_import_excel_via_api(client):  # client fixture 改为注入 mock embedder 的 app
    resp = client.post("/api/v1/tags/import", files={"file": ("tags.xlsx", XLSX_BYTES)})
    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"]["inserted"] == 1 and body["summary"]["issues"] == []
    got = client.get(f"/api/v1/tags?q=悬疑")
    assert got.json()["items"][0]["tag_name"] == "悬疑"

def test_import_twice_reports_duplicates(client):
    resp1 = client.post("/api/v1/tags/import", files={"file": ("tags.xlsx", XLSX_BYTES)})
    resp2 = client.post("/api/v1/tags/import", files={"file": ("tags.xlsx", XLSX_BYTES)})
    assert resp2.json()["summary"]["skipped"] == 1
    assert resp2.status_code == 200          # Review Focus 2：重复导入不 5xx

def test_bad_header_422(client):
    resp = client.post("/api/v1/tags/import", files={"file": ("x.xlsx", BAD_HEADER_BYTES)})
    assert resp.status_code == 422

def test_batch_list_and_detail(client): ...
def test_tag_404(client):
    assert client.get("/api/v1/tags/tag_none").status_code == 404
```

- [ ] **Step 2: 运行确认失败** — Run: `python -m pytest tests/test_api_tags.py -v`，Expected: FAIL（404 路由不存在）

- [ ] **Step 3: 实现** — 路由薄层：解析→调 `run_governance`/查询→响应模型（Pydantic response_model 定义在各自 router 文件）。conftest 升级 `client` fixture：真 app + 临时文件 DB + `use_mock_embedder=True`。

- [ ] **Step 4: 运行确认通过** — Run: `python -m pytest tests/test_api_tags.py -v`，Expected: PASS

- [ ] **Step 5: 提交** — `git add -A && git commit -m "feat: 标签导入/批次/标签查询 API"`

---

### Task 11: API——内容导入与资产查询

**Files:**
- Create: `src/tagplatform/api/routers/content.py`、`tests/test_api_content.py`
- Modify: `src/tagplatform/api/__init__.py`

**Interfaces:**
- Consumes: `get_adapter`、`import_content`、`resolve_video_path`
- Produces（路由）：
  - `POST /api/v1/content/import`：body `{"kind": "platform", "video_root": str | None, "data": <原始内容 JSON dict>}` → `get_adapter(kind).adapt(data)`（KeyError → 422 列出可用 kind；ValidationError → 422 带摘要）→ `import_content(...)`（ValueError → 422）→ `{"asset_id", "context_id", "scene_asset_ids"}`。
  - `GET /api/v1/assets?asset_type=&q=`：资产列表（q 匹配 title），含 video_uri、parent_id。
  - `GET /api/v1/assets/{asset_id}`：详情 + 其全部 `media_context`（context_id/model_version/recognition_status/created_at，不含 content_json 大字段；提供 `GET /api/v1/assets/{asset_id}/contexts/{context_id}` 取完整 JSON）。
  - `GET /api/v1/adapters`：`list_adapters()`（前端导入表单的 kind 下拉数据源）。

- [ ] **Step 1: 写失败测试**

```python
def test_content_import_via_api(client):
    resp = client.post("/api/v1/content/import", json={
        "kind": "platform", "video_root": None, "data": CONTENT_FIXTURE_DICT})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["scene_asset_ids"]) == 1
    detail = client.get(f"/api/v1/assets/{body['asset_id']}").json()
    assert detail["contexts"][0]["recognition_status"] == "completed"
    full = client.get(f"/api/v1/assets/{body['asset_id']}/contexts/{body['context_id']}")
    assert full.json()["content"]["summary"]

def test_content_import_bad_video_root_422(client):   # Review Focus 4
    resp = client.post("/api/v1/content/import", json={
        "kind": "platform", "video_root": "Z:/不存在", "data": CONTENT_FIXTURE_DICT})
    assert resp.status_code == 422

def test_content_import_unknown_kind_422(client):
    ... assert resp.status_code == 422 and "platform" in resp.json()["detail"]

def test_adapters_endpoint(client):
    assert client.get("/api/v1/adapters").json() == ["platform"]
```

- [ ] **Step 2: 运行确认失败** — Run: `python -m pytest tests/test_api_content.py -v`，Expected: FAIL

- [ ] **Step 3: 实现** — 同任务 10 的薄层风格。

- [ ] **Step 4: 运行确认通过** — Run: `python -m pytest`（全量），Expected: ALL PASS

- [ ] **Step 5: 提交** — `git add -A && git commit -m "feat: 内容导入与资产查询 API（阶段 1 收口）"`

---

### Task 12: 外部来源适配器（需要输入：来源样例 JSON）

**前置输入：** 用户提供的"外部视频理解项目"输出样例 JSON（≥1 部影片、含场景与起止时间戳）。**拿到样例前本任务不动工**；其余任务不依赖它。

**Files:**
- Create: `src/tagplatform/adapters/external.py`、`tests/fixtures/external_sample.json`（用户提供样例的脱敏副本）、`tests/test_adapter_external.py`

**Interfaces:**
- Consumes: `ContentAdapter` 协议、`register_adapter`、`parse_content` 的模型树
- Produces: `ExternalAdapter(kind="external_vision")`：字段映射按样例实际结构（层级组织、时间戳单位归一到毫秒、实体/台词/事件映射到 §3 模型树）；映射不了的源字段丢弃并在 docstring 记录清单。

- [ ] **Step 1: 写失败测试** — 以样例 fixture 为输入：`get_adapter("external_vision").adapt(SAMPLE) `通过 `parse_content` 级校验（即输出是合法 ContentJSON），关键断言按样例中已知的时间戳与场景数取值。

- [ ] **Step 2: 运行确认失败** — Run: `python -m pytest tests/test_adapter_external.py -v`，Expected: FAIL

- [ ] **Step 3: 实现** — 纯映射函数；时间戳单位（秒/毫秒）按样例实测判定，常量记录在文件顶部。

- [ ] **Step 4: 运行确认通过** — Run: `python -m pytest tests/test_adapter_external.py -v`，Expected: PASS

- [ ] **Step 5: 提交** — `git add -A && git commit -m "feat: 外部视频理解项目内容适配器"`

---

## 收尾核对（执行完成后）

- `python -m pytest` 全绿；`alembic upgrade head` 在空库可执行。
- 用一份真实 Excel 与一份平台格式内容 JSON 手工走 `POST /api/v1/tags/import` 与 `POST /api/v1/content/import` 冒烟（uvicorn 起本地服务 + curl）。
- 更新项目状态记忆：阶段 1 完成，准备写阶段 2 计划（模式 A/B 生产引擎，含 sqlite-vec 与 LLM 调用层评估）。
