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
- **嵌入器**：不设 `TAGPLT_USE_MOCK_EMBEDDER`，首次使用自动下载
  `intfloat/multilingual-e5-base`（768 维句向量）。

## 环境变量

前缀 `TAGPLT_`（读 `.env` 与进程环境）：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `OPENAI_BASE_URL` / `OPENAI_API_KEY` | 无 | OpenAI 兼容端点地址与密钥；LLM 生成必需，未设置时 complete 报错指引复制 `.env.example` |
| `TAGPLT_DATABASE_PATH` | `data/tagplatform.db` | SQLite 数据库文件路径（迁移与应用共用） |
| `TAGPLT_ADMIN_PASSWORD` | `admin123` | 首管理员启动引导密码（user_account 空表时创建 admin 账号；生产务必经本变量改强密码） |
| `TAGPLT_SESSION_SECRET` | `dev-secret` | 会话令牌 HMAC 密钥；**生产必改**（默认值仅供开发/测试，泄漏即可伪造任意用户登录态） |
| `TAGPLT_USE_MOCK_LLM` | `false` | true = 确定性 MockLLMClient（零网络，测试/冒烟） |
| `TAGPLT_USE_MOCK_EMBEDDER` | `false` | true = 确定性 MockEmbedder（不下载模型） |
| `TAGPLT_GENERATION_WORKERS` | `2` | 生成任务线程池大小；0 = 同步执行（submit 返回即任务完成） |
| `TAGPLT_LLM_MODEL` | `qwen3.7-plus` | LLM 模型名 |
| `TAGPLT_SEMANTIC_SIMILAR_THRESHOLD` | `0.90` | 语义相似阈值，余弦相似度 >= 该值判为疑似同义 |
| `TAGPLT_RECALL_TOP_K_PER_TYPE` | `50` | 模式 A 每 tag_type 召回候选上限 |
| `TAGPLT_OPEN_MATCH_THRESHOLD` | `0.85` | 模式 B 归一匹配阈值：与库内标签相似度 >= 该值归入 catalog |

其余 `TAGPLT_*` 配置项（温度、限速间隔、白名单等）见 `src/tagplatform/settings.py`。

## 测试

```bash
.venv/Scripts/python -m pytest
```

全量约 130 条测试，默认全走 mock，不联网、不下载模型。

## 架构概览

`src/tagplatform/` 是核心纯 Python 包，分层红线：除 `api/` 子包外，任何模块
不得 import fastapi——业务逻辑与 Web 框架解耦，`api/` 只是 FastAPI 薄层
（依赖注入 + 协议转换，路由不写业务规则）。

```
src/tagplatform/
├── db/                     SQLAlchemy 模型（9 张表，结构由 alembic 迁移管理）
├── schemas/                影视内容 JSON 契约与解析
├── adapters/               内容源适配（内容 JSON 导入）
├── governance/             阶段 1：Excel 标签库导入、检测与治理流水线
├── content_import.py       内容识别 JSON 导入
├── embed/ + vectors.py     句向量嵌入与相似度计算
├── llm/                    OpenAI 兼容客户端 / Mock / 输出解析
├── generation/             阶段 2：模式 A（封闭式提取）/ 模式 B（开放式生成）+ 召回
├── prompts.py              prompt 模板渲染（版本化模板存库）
├── tasks/                  生成任务执行器：状态机、线程池、孤儿清理
├── results.py              标签结果入库（approved 保留 / pending 覆盖）
└── api/                    FastAPI 薄层（唯一 import fastapi 的入口）
```

**数据表（9 张）**：`media_asset`、`media_context`、`tag_catalog`、
`generation_task`、`asset_tag`、`review_record`、`import_batch`、
`tag_change_log`、`prompt_template`。

**两阶段功能一览**：

- **阶段 1（导入与治理）**：Excel 标签库上传 → 解析/检测 → 语义归一 →
  入库；批次与变更全程留痕（`import_batch` / `tag_change_log`）。
- **阶段 2（生成引擎）**：内容识别 JSON 导入 → 建生成任务（模式 A 从
  库内按类型召回候选做封闭式提取 / 模式 B 开放式生成后归一匹配入库）→
  后台线程池执行（0 = 同步）→ 生成结果与任务终态同事务落库，任务可
  查询、失败可重试。
