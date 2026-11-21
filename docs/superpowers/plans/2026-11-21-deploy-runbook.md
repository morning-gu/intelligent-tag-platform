# 四层重构部署 runbook（2026-11-21-deploy-runbook.md）

适用范围：`refactor/four-layer` 分支全量合入 main 后的一次性停服窗口——
**容器化切换与四层重构同窗**（spec 执行策略 §4-5）。本文只写可复制执行的
命令形态与回退序列；占位路径沿用 deploy/deploy.md 约定 `/opt/tagplatform`
（按服务器实际布局替换）。

配套阅读：

- `deploy/deploy.md`（venv+systemd 部署基线；本文的容器化切换与之并存，
  切换完成后线上以容器形态常驻）
- `Dockerfile`（#36 多阶段镜像：node 构建 dist + python 运行时，EXPOSE 8100）
- spec：`docs/superpowers/specs/2026-11-21-four-layer-refactor-design.md` §执行策略

迁移链状态：0001→0018 全链已在本地 fresh 空库验证（upgrade head 全通 +
0018→0017→0016 两级 downgrade 可回，见 §5 验证记录）。

---

## 1. 切换序列（容器化，一次停服窗口）

顺序总览：**cancel 在途评测 → 采集 smoke 基线（§4.1，停服前）→ 停旧
systemd 服务 → DB/media 备份 → 构建镜像（建议窗口前预构建）→ 起新容器
（迁移随入口）→ smoke**。

### 1.1 窗口前预构建（可选，缩短停机）

镜像构建不依赖线上库，可在窗口前完成：

```bash
cd /opt/tagplatform          # 已同步到重构合入提交（refactor/four-layer 合入 main 后的 main HEAD）
docker build -t tagplatform:four-layer-<git-sha> .
docker images | grep tagplatform   # 确认新 tag 就绪；旧镜像 tag 保留勿删（回退用）
```

> 部署机无法直连 docker.io 时先 retag 基础镜像（Dockerfile 头部注释）。
> 构建上下文 = 仓库根（`COPY frontend/`、`COPY src/`），勿在脏工作区构建。

### 1.2 主动 cancel 全部在途评测 run

cancel 为优雅语义：置事件后不再提交新片，在途片跑完、已完成片结果留盘
（progress.jsonl / raw_results 不丢）。**在途片重跑为已知代价**（spec §4a）。

```bash
# 登录取会话 cookie（HttpOnly，cookie jar 保存）
curl -s -c /tmp/jar -X POST http://127.0.0.1:8100/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"<TAGPLT_ADMIN_PASSWORD>"}'

# 列出历史 run，找 status=running 的 run_id
curl -s -b /tmp/jar http://127.0.0.1:8100/api/v1/eval/closed/runs

# 逐个 cancel（对每个 running 的 run_id）
curl -s -b /tmp/jar -X POST http://127.0.0.1:8100/api/v1/eval/closed/runs/<run_id>/cancel

# 轮询至终态（canceled/failed/完成）后再停服；
# 若窗口不等：直接停服后 meta.status=running 的 run 会被新进程历史扫描标
# interrupted——已知语义，重跑即可
```

### 1.3 采集 smoke 基线（停服前，登录 cookie 同 §1.2）

基线必须在**旧服务仍在跑时**采集——停服后再采，比对对象就变成新服务
自身，白名单外差异静默漏检（逐字比对失效）。登录复用 §1.2 的 cookie jar：

```bash
mkdir -p /backup/smoke-before
curl -s http://127.0.0.1:8100/openapi.json > /backup/smoke-before/openapi.json
# 登录态逐端点采样与带参端点取样：命令与端点清单见 §4.1
```

### 1.4 停旧 systemd 服务

```bash
sudo systemctl stop tagplatform
sudo systemctl status tagplatform   # 确认 inactive；8100 端口已释放
```

停服后旧进程不再接受写入；**暂不 disable**（回退路径 §2.1 仍用它）。

### 1.5 取 DB 与 media 备份（停写入后）

```bash
mkdir -p /backup/switch-$(date +%F)
sqlite3 /opt/tagplatform/data/tagplatform.db ".backup /backup/switch-$(date +%F)/tagplatform.db"
cp -r /opt/tagplatform/data/media /backup/switch-$(date +%F)/media
# 校验：备份库可打开且迁移版本与线上当前一致
sqlite3 /backup/switch-$(date +%F)/tagplatform.db "SELECT version_num FROM alembic_version; PRAGMA integrity_check;"
```

回退的"标准路径"还原这份备份（§2.1）；"零丢失路径"不还原备份、改走
downgrade（§2.2）。

### 1.6 起新容器（迁移随入口执行）

生产库路径、密钥、LLM 网关等经挂载的 `/app/.env` 提供（pydantic-settings
env_file 相对进程 CWD=/app 解析，Dockerfile 已内置）。**必须确保
`TAGPLT_DATABASE_PATH` 指向挂载卷内的路径**（容器重建后库不丢）：

```bash
docker run -d --name tagplatform \
  --restart unless-stopped \
  -p 8100:8100 \
  -v /opt/tagplatform/data:/app/data \
  -v /opt/tagplatform/.env:/app/.env:ro \
  -v /opt/tagplatform/hf-cache:/hf-cache \
  tagplatform:four-layer-<git-sha> \
  sh -c "python -m alembic upgrade head && python -m uvicorn run_server:app --host 0.0.0.0 --port 8100"

docker logs -f tagplatform   # 看到 "Running upgrade ... -> 0018" 与 uvicorn 启动行
curl http://127.0.0.1:8100/health   # 期望 {"status":"ok"}
```

- 入口命令显式覆盖 CMD 先跑 `alembic upgrade head` 再起 uvicorn（spec §4c
  "迁移随容器入口"；镜像默认 CMD 不含迁移，**部署时按 #36 实际启动方式核对
  此命令形态**）。
- 迁移 0016（多模态 evidence 播种）→ 0017（tag_change_log 批键列）→
  0018（drop prompt_template）按链序自动执行。
- 首次启动若 HF 模型缓存为空且 `HF_HUB_OFFLINE=1`，需先把宿主现有 HF 缓存
  拷入挂载目录（`HF_HOME=/hf-cache`）。

### 1.7 smoke（切换后验证）

见 §4：白名单外端点基线比对 + prompt 渲染逐字比对。全过 → 窗口结束；
不过 → 回退 §2。

---

## 2. 回退序列

### 2.1 标准回退（旧镜像 + DB 备份还原；接受丢窗口内写入）

```bash
# 1. 停新容器
docker stop tagplatform && docker rm tagplatform

# 2. 还原 DB 备份（回到切换点；库文件名与 TAGPLT_DATABASE_PATH 一致）
sudo systemctl stop tagplatform 2>/dev/null   # 若曾回 systemd 形态
cp /backup/switch-<日期>/tagplatform.db /opt/tagplatform/data/tagplatform.db
cp -r /backup/switch-<日期>/media/. /opt/tagplatform/data/media/

# 3. 起旧形态（二选一，与切换前线上形态一致）：
#    a. 旧容器（若此前已是容器形态）：docker run 同 §1.5、镜像换旧 tag、
#       去掉入口 alembic 前缀
#    b. 旧 systemd 服务（venv 形态，本文基线）：
sudo systemctl start tagplatform   # ExecStartPre 自动重跑迁移（还原库为旧版本，迁移为 no-op）
curl http://127.0.0.1:8100/health
```

### 2.2 备用零丢失路径（先 downgrade 再回滚镜像；保留窗口内写入）

新迁移均附 downgrade（0018→0017→0016 已验证可回）。**downgrade 必须用
新代码执行**（0018 的 downgrade 从 capabilities/prompts/templates.py 常量
回填 prompt_template 种子——任务定义的显式例外；旧代码无该模块）：

```bash
# 1. 新容器内执行两级 downgrade（新容器此时只跑迁移、不提供服务）
docker exec tagplatform python -m alembic downgrade -1   # 0018 -> 0017
docker exec tagplatform python -m alembic downgrade -1   # 0017 -> 0016

# 2. 停新容器（迁移版本已回 0016，旧代码兼容）
docker stop tagplatform && docker rm tagplatform

# 3. 起旧形态（同 §2.1 第 3 步）——窗口内写入保留在库中
```

> 取舍：标准路径简单可靠（还原到切换点快照）；零丢失路径多一步 downgrade、
> 依赖新代码在容器内可用。仅当窗口内确有不可重放的写入时选 2.2。

---

## 3. 行为变化清单（部署知悉项）

来源 = 重构 ledger（progress.md）各任务记录的部署知悉项。白名单内的契约
变化（tags list 加 total、prompts 只读化、POST/activate 删除）不在此列，
见 §4.2。

1. **组合值挂载行为变化**（Task 8）：`tag_catalog.applicable_scope` 组合值
   （如 `content,scene`）的目录挂载，从"静默降级只挂 content"变为"按证据
   挂 character/scene"——修复本意。生成/评测产物可能多出非 content 侧
   挂载行；smoke 时抽样核对组合值目录的挂载结果。
2. **词表并集匹配变化**（Task 16）：噪音词表合并为单一常量，两侧过滤面
   变宽——dataset 侧也剥"蓝光/中字/eng"，asset_build 侧也剥"hardsub/
   uncut"；标题恰含噪音词的极端情形存在过滤差异（已备案）。
   judge 主/重试提示词 quote 矛盾已可见化为常量但文案未修——内容拍板
   非重构活，**部署后处理**。
3. **ingest 坏 JSON 回落**（Task 9）：ingest 遇坏 JSON 从"丢弃（[]）"变为
   "按逗号回落解析"——修复本意；线上仅历史脏数据可见差异。
4. **build_many stdout→logging**（Task 9）：批量构建进度输出从 stdout 改走
   logging——若运维侧按 stdout 抓取该输出，需改从日志管道收取。
5. **logger 名迁移**（Task 6）：persist 留痕与过滤链日志的 logger 从
   `tagplatform.tasks.runner` / `tagplatform.filtering` 变为
   `tagplatform.services.generation.execute` /
   `tagplatform.services.generation.filtering[.base]`——按旧 logger 名过滤
   的日志管道需同步（本仓 _configure_logging 走根 logger，无内置过滤）。
6. **0017 NULL 窗口**（Task 12）：迁移 0017 之后若仍以旧代码写入，
   `tag_change_log` 的 import 审计行批键列 `task_id` 为 NULL（反查退回
   after_json 文本匹配）。**一次性全量部署无窗口期**——停旧服务后才切新
   容器，切换后全走新列。
7. **smoke 基线豁免 #32**（spec §4d）：#32 单片 badcase 视图的报告页字段
   差异列入豁免清单（基线 = 切换前线上实际版本，不含 #32）。
8. **前端边缘差异**（Task 15，reviewer 确认）：评测弹窗关闭后晚到的
   build_task_id 不再隐形轮询 + toast——旧代码意外行为消除，非缺陷。

---

## 4. smoke 明细

### 4.1 基线采集（§1.3 步骤的完整明细；仅限停服前执行）

```bash
mkdir -p /backup/smoke-before
curl -s http://127.0.0.1:8100/openapi.json > /backup/smoke-before/openapi.json

# 登录（同 §1.2），逐端点采样到 /backup/smoke-before/
# 无路径参数的 GET 端点可直接循环：
python3 - <<'PY'
import json
paths = json.load(open("/backup/smoke-before/openapi.json"))["paths"]
for p, ops in paths.items():
    if "get" in ops and "{" not in p:
        print(p)
PY
#   对输出的每个路径：curl -s -b /tmp/jar http://127.0.0.1:8100<path> >
#   /backup/smoke-before/<path 转义>.json
# 带路径参数的端点每 router 手工取样至少一个（assets/generation/eval runs/
# imports 批次/media build-task 等），取样 id 从列表端点取真实值
```

### 4.2 白名单契约差异（比对时预期出现，非回归）

| 端点 | 预期差异 |
|---|---|
| `GET /api/v1/tags` | 响应**加 `total` 字段**（其余结构不变） |
| `GET /api/v1/prompts` | 只读视图：返回各 mode 模板文本 + allowed/required 占位符；**版本字段移除** |
| `POST /api/v1/prompts`、`POST /api/v1/prompts/{id}/activate` | **端点消失**（405/404） |
| `GET /api/v1/media/build-tasks/{task_id}/view` | URL 与内容等价（HTML 移静态文件、task_id 转义替换）；内容非逐字节相同属预期 |
| 其余白名单外端点 | URL、请求、响应结构**冻结**——比对应零差异（豁免除外） |

**豁免清单**：#32 单片 badcase 视图的报告页字段（eval runs 报告/单片明细
相关响应）——基线版本不含 #32。

### 4.3 prompt 渲染逐字比对（spec §4d 新增）

prompt 本地化后模板常量在 `capabilities/prompts/templates.py`（git 即版本）；
0018 已 drop prompt_template 表。切换后验证容器内渲染链路输出与常量逐字
一致：

```bash
docker exec tagplatform python - <<'PY'
from tagplatform.capabilities.prompts import (
    get_prompt, render_prompt, CLOSED_TEMPLATE_TEXT, OPEN_TEMPLATE_TEXT,
)
sample = "场景1：示例内容"
for mode, const in (("closed", CLOSED_TEMPLATE_TEXT), ("open", OPEN_TEMPLATE_TEXT)):
    rendered = render_prompt(get_prompt(mode), content=sample)
    assert rendered == const.format(content=sample), f"{mode} 渲染与常量不一致"
print("prompt 渲染逐字一致 OK")
PY
```

完整生成链路的逐字等价（produce 拼接候选 JSON 等）已由仓库内
`tests/test_api_prompts_admin.py` 的 prompts_local 断言与 Task 16 逐字
harness 钉死（全量回归 927 绿）；线上 smoke 取上述轻量形态即可。

---

## 5. 迁移链 fresh 验证记录（2026-11-22，Task 17）

临时空库（`TAGPLT_DATABASE_PATH` 覆盖）：

- `alembic upgrade head`：0001→0018 **18 个迁移全链通过**（输出逐条
  "Running upgrade 000N -> 000N+1"），`alembic current` = 0018 (head)
- `alembic downgrade -1` ×2：0018→0017、0017→0016 均通过，
  `alembic current` = 0016
- downgrade 效果抽查：`tag_change_log` 列表不含 `task_id`（0017 回滚生效）；
  `prompt_template` 表重建且含 2 行种子（0018 从 templates.py 常量回填
  生效）
