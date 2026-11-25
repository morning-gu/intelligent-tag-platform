# 影视智能标签平台

面向影视内容的离线智能标签生产平台：标签库导入治理（阶段 1）+ LLM 标签生成引擎（阶段 2）+ 结果入库与人工审核支撑，统一存储于 SQLite，经 FastAPI 对外提供 API。

## 快速启动

> 顺序必须严格：**先迁移、后启动**。应用启动即查询业务表（孤儿任务清理等），
> 空库（未跑迁移）直接崩溃——这不是可选步骤。

```bash
# 1) 创建虚拟环境并安装（Python >= 3.11）
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"

# 2) 数据库迁移（必须；TAGPLT_DATABASE_PATH 指定库文件位置）
TAGPLT_DATABASE_PATH=data/tagplatform.db .venv/Scripts/python -m alembic upgrade head

# 3) 启动服务（冒烟模式：mock 开关全开，零网络、零模型下载）
TAGPLT_DATABASE_PATH=data/tagplatform.db \
TAGPLT_USE_MOCK_EMBEDDER=true \
TAGPLT_USE_MOCK_LLM=true \
.venv/Scripts/python -m uvicorn tagplatform.api:create_app --factory

# 4) 冒烟验证
curl http://127.0.0.1:8000/health        # {"status":"ok"}
```

冒烟开关说明：

- `TAGPLT_USE_MOCK_LLM=true`：用确定性 MockLLMClient（哈希即输出），不访问任何 LLM 端点；
- `TAGPLT_USE_MOCK_EMBEDDER=true`：用确定性 MockEmbedder，不下载 sentence-transformers 模型。

两者都适合首次冒烟与离线演示；接真实模型时去掉对应开关即可（见下）。

### 接真实模型

- **LLM**：复制 `.env.example` 为 `.env`，填入 `OPENAI_BASE_URL` 与
  `OPENAI_API_KEY`（OpenAI 兼容端点）。不设 `TAGPLT_USE_MOCK_LLM`（默认
  false）即走真实端点；未配置就发起生成任务时，complete 会报错并提示
  "复制 .env.example 为 .env"。
- **嵌入器**：默认采用本地 `BAAI/bge-m3`（1024 维句向量，不设 `TAGPLT_USE_MOCK_EMBEDDER` 时首次使用自动加载）；也可设 `TAGPLT_EMBEDDING_BACKEND=remote` 并配置 `TAGPLT_EMBEDDING_API_BASE` 对接 OpenAI 兼容 `/v1/embeddings` 服务。

### 前端启动（双进程开发流）

前端为 React + AntD SPA（`frontend/`），开发时与后端**双进程并行**：

```bash
# 1) 安装依赖（Node >= 20.19）
cd frontend && npm install

# 2) 启动 Vite 开发服务器（默认 http://localhost:5173）
npm run dev
```

- **代理说明**：`vite.config.ts` 将 `/api` 代理到 `http://127.0.0.1:8000`——
  前端代码只写相对路径（免 CORS），需先按上文步骤把 FastAPI 跑在 8000 端口。
- **登录**：浏览器打开 `http://localhost:5173`，用 admin 账号登录
  （首管理员密码见环境变量 `TAGPLT_ADMIN_PASSWORD`，默认 `admin123`）；
  会话为 HttpOnly cookie，401 统一跳登录页。
- **测试与构建**：`npm test`（Vitest + Testing Library）、`npm run build`
  （tsc 类型检查 + 产物输出 `dist/`）。

### 开放评测（admin）

「开放评测」页（仅 admin）零漂移干跑整条开放生成管线（不写入业务库，产物只落
`TAGPLT_OPEN_EVAL_OUTPUT_ROOT`），并由独立评审模型按 0–4 五档量表逐条盲评
内容相关性、语义准确性、标签价值，再综合给出保留结论。报告提供评审通过率、新词占比、
三维均分与分布，并支持展开单部影片查看生产审计与逐次评审诊断，以及基于产出快照发起重评。

> 注：早期内嵌在「生产任务」中的附带标签质量评估已退役并由独立的「开放评测」替代；
> 历史评估记录保留于数据库兼容表。详细口径见 [评测方法 §5](docs/eval.md)，
> 三维五档评分标准见 [标签质量评估设计](docs/open-tag-quality-evaluation.md)。

### 闭环评测（admin）

「闭环评测」页（仅 admin）以 `/cache/MovieTags` 银标准基准做干跑排序评测：
选数据集与影片（preset30 固定抽样 / all / custom）起跑 → 进度轮询与取消 →
θ×K 网格报告（P/R/nDCG ± 自举 CI，附先验基线、覆盖率与样本核算），产物
（report.md / metrics.csv / raw_results.json 等）可下载。单进程同时只允许
一个 run（重复起跑 409）。数据集根与产物根由 `TAGPLT_EVAL_DATASET_ROOT` /
`TAGPLT_EVAL_OUTPUT_ROOT` 配置；评测全程对业务库只读（DB 零写入），产物只
落 `TAGPLT_EVAL_OUTPUT_ROOT`。

### 生产部署（同源反代）

生产部署采用 **venv + systemd + nginx** 直接部署（无 Docker 依赖）：nginx 托管
`frontend/dist` 静态产物（SPA）并把 `/api` 反代到本机 uvicorn（同源，无 CORS），
uvicorn 以 systemd 服务常驻、启动前自动跑迁移。完整步骤（依赖锁定、迁移、
systemd/nginx 启用、验证、数据备份）见 **`deploy/deploy.md`**（配套 systemd
unit、nginx 站点配置与 `requirements.lock` 均在 `deploy/` 下）。

## 环境变量

前缀 `TAGPLT_`（读 `.env` 与进程环境）：

| 变量                                     | 默认值                  | 说明                                                                                            |
| ---------------------------------------- | ----------------------- | ----------------------------------------------------------------------------------------------- |
| `OPENAI_BASE_URL` / `OPENAI_API_KEY` | 无                      | OpenAI 兼容端点地址与密钥；LLM 生成必需，未设置时 complete 报错指引复制 `.env.example`        |
| `TAGPLT_DATABASE_PATH`                 | `data/tagplatform.db` | SQLite 数据库文件路径（迁移与应用共用）                                                         |
| `TAGPLT_ADMIN_PASSWORD`                | `admin123`            | 首管理员启动引导密码（user_account 空表时创建 admin 账号；生产务必经本变量改强密码）            |
| `TAGPLT_SESSION_SECRET`                | `dev-secret`          | 会话令牌 HMAC 密钥；**生产必改**（默认值仅供开发/测试，泄漏即可伪造任意用户登录态）       |
| `TAGPLT_USE_MOCK_LLM`                  | `false`               | true = 确定性 MockLLMClient（零网络，测试/冒烟）                                                |
| `TAGPLT_USE_MOCK_EMBEDDER`             | `false`               | true = 确定性 MockEmbedder（不下载模型）                                                        |
| `TAGPLT_EMBEDDING_BACKEND`             | `local`               | 嵌入后端：`local`（sentence-transformers 本地）/ `remote`（OpenAI 兼容 /v1/embeddings）         |
| `TAGPLT_EMBEDDING_MODEL`               | `BAAI/bge-m3`         | local 后端句向量模型名或本地模型路径（1024 维）                                                 |
| `TAGPLT_EMBEDDING_API_BASE`            | 空                    | remote 后端服务地址（含 `/v1` 前缀，如 `http://9.15.87.193:8193/v1`）；backend=remote 时必填   |
| `TAGPLT_EMBEDDING_API_MODEL`           | `bge-m3`              | remote 后端请求的 model 名                                                                      |
| `TAGPLT_EMBEDDING_API_BATCH`           | `32`                  | remote 后端分批请求上限（大目录标签集按批编码）                                                 |
| `TAGPLT_EMBEDDING_API_TIMEOUT`         | `60`                  | remote 后端单请求超时秒数                                                                       |
| `TAGPLT_GENERATION_WORKERS`            | `2`                   | 生成任务线程池大小；0 = 同步执行（submit 返回即任务完成）                                       |
| `TAGPLT_LLM_MODEL`                     | `qwen3.7-plus`        | LLM 模型名                                                                                      |
| `TAGPLT_MTG_JUDGE_MODEL`               | `deepseek-v4.1-flash` | 模式 B 开放生成 Judge 审核模型名                                                                |
| `TAGPLT_RECALL_TOP_K_PER_TYPE`         | `50`                  | 模式 A 向量 + BM25 召回经 RRF 融合后的全局候选上限（变量名为兼容旧配置保留）                     |
| `TAGPLT_CLOSED_BATCH_SIZE`             | `1`                   | 模式 A 目录组内分批上限：每批一次 LLM 调用（防输出截断），组内批次全部完成后合并再门控截断      |
| `TAGPLT_OPEN_LIBRARY_SUSPECT_COSINE`   | `0.75`                | 模式 B 库内对齐疑似余弦阈值：与库内最近标签相似度 ≥ 该值时送 LLM 概念复核                       |
| `TAGPLT_OPEN_LIBRARY_NAME_OVERLAP`     | `0.40`                | 模式 B 库内对齐疑似字面重合阈值：字符 Jaccard ≥ 该值时送 LLM 概念复核                           |
| `TAGPLT_MEDIA_ROOT`                    | `data/media/`         | 上传视频存盘根目录（上传时按需创建；数据备份需覆盖该目录）                                      |
| `TAGPLT_EVAL_DATASET_ROOT`             | `/cache/MovieTags`    | 闭环/开放评测基准数据集根（`gt/*.csv` 三列 gt + `data/<ttID>/content.json`，全程只读；换数据集 = 改本变量，不接受前端提交路径） |
| `TAGPLT_EVAL_OUTPUT_ROOT`              | `data/eval/closed`    | 闭环评测产物落盘根（meta/进度/报告三件套；闭环评测代码唯一的写盘位置）                          |
| `TAGPLT_OPEN_EVAL_OUTPUT_ROOT`         | `data/eval/open`      | 开放评测产物落盘根（meta/报告/产出与评审快照；开放评测代码唯一的写盘位置）                      |
| `TAGPLT_OPEN_EVAL_REVIEW_MODEL`        | 空                    | 开放评测独立评审模型名；留空时回落 `TAGPLT_LLM_MODEL`                                           |
| `TAGPLT_LOG_LEVEL`                     | `INFO`                | 根日志级别；生成流水线与过滤责任链的逐步日志走 INFO，设 `DEBUG` 可看到每环节传入/传出的标签名 |

其余 `TAGPLT_*` 配置项（温度、限速间隔、白名单等）见 `src/tagplatform/settings.py`。

## 测试

```bash
.venv/Scripts/python -m pytest
```

全量 pytest 用例默认全走 mock，不联网、不下载模型。

前端测试（Vitest + Testing Library，含组件与 API 契约测试）：

```bash
cd frontend && npm test
```

## 架构概览

`src/tagplatform/` 是核心纯 Python 包，按四层架构拆分，分层红线：除 `api/`
子包外，任何模块不得 import fastapi——业务逻辑与 Web 框架解耦，`api/` 只是
FastAPI 薄层（依赖注入 + 协议转换，路由不写业务规则）。

```
src/tagplatform/
├── capabilities/           基础设施层：
│   ├── storage/            SQLAlchemy ORM 模型与数据库会话（结构由 alembic 管理）
│   ├── embed/ + vectors.py 句向量嵌入（local / remote / mock）与相似度计算
│   ├── llm/                OpenAI 兼容客户端 / Mock / 用量统计
│   ├── prompts/            本地化提示词常量与只读注册表（closed / open / open_review / assets）
│   └── adapters/           内容源与视觉模型适配
├── schemas/                影视内容 JSON 契约与上游数据解析
├── services/               核心业务服务层：
│   ├── tags/               阶段 1：标签库导入治理（Excel/XML）、目录树读取与挂载点解析
│   ├── content/            内容识别导入、视频资产构建、审核后标签入库
│   ├── generation/         阶段 2：模式 A（closed 封闭提取）、模式 B（open/mtg 逐目录两阶段
│   │                       生成 + Judge + 聚类 + 库内对齐）、过滤责任链、结果落库与人工审核
│   ├── eval/               闭环评测、开放评测（独立盲评与报告）、消融/排序/一致性与质量指标
│   └── auth/               阶段 3：账号密码哈希、会话认证与用户管理
├── orchestration/          跨服务编排与接口 DTO 装配（供 api/ 调用）
├── tasks/                  后台任务执行器：生成队列状态机、线程池、孤儿清理与标签导入任务
├── api/                    FastAPI 薄层（唯一 import fastapi 的入口；routers 仅做协议转换）
├── settings.py             全局配置（TAGPLT_* 环境变量）
└── reembed.py              标签/目录向量批量重灌命令行入口
```

**数据表**：10 张现行表——`media_asset`、`tag_catalog`（目录）、`tag_define`（标签）、
`tag_generation_task`、`pre_asset_tag`、`asset_tag`、`tag_import_task`、
`tag_change_log`、`user_account`、`asset_build_task`，另保留 1 张历史兼容表
`tag_quality_evaluation`。`prompt_template` 表已于迁移 `0018` 移除，提示词统一
本地化维护于 `capabilities/prompts/`。

**三阶段功能一览**：

- **阶段 1（导入与治理）**：Excel/XML 标签库上传 → 解析/检测 → 语义归一与向量持久化 →
  入库；批次与变更全程留痕（`tag_import_task` / `tag_change_log`）。
- **阶段 2（生成引擎）**：内容识别 JSON 导入或视频资产构建 → 建生成任务（模式 A
  从库内混合召回候选做封闭式提取 / 模式 B 逐目录两阶段开放生成、Judge 三维过滤、
  场景聚类与库内概念对齐）→ 后台线程池执行（0 = 同步）→ 过过滤责任链 →
  按目录适用范围挂载并落库，任务可查询、失败可重试。
- **阶段 3（审核后台与评测）**：登录认证（admin/reviewer 两级角色，除 `/health`
  外全 API 保护）→ 审核中心执行 `approve` / `reject` / `modify`（仅限库外新词）/ `merge`
  单条与批量审核（并发冲突保护、逐条独立不中断；新词通过自动入库并写入向量）→
  闭环评测、开放评测与质量指标复盘。
