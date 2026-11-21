# 容器化部署（9.15.154.212）

> 2026-11-23 起生效。此前「本地 tar 打包 + SFTP 上传 + stop.sh/start.sh」流程退役为
> **回滚路径**（start.sh 原样保留，随切随回）。

## 架构

- **镜像**：仓库根 `Dockerfile`，两阶段——node:24-slim 构建前端 dist（部署机无
  node，node 由镜像提供）+ python:3.12-slim 运行时。依赖层只依赖
  `pyproject.toml`（tomllib 现解析），代码变更命中层缓存不重装依赖。
- **代码分发**：服务器应用目录是仓库的 git checkout（remote 走 gh-proxy 镜像），
  部署 = `git fetch + reset --hard` + `docker build`。
- **运行**：容器名 `tagplatform`，`--restart unless-stopped`，宿主 8100 → 容器 8100。
- **挂载**（镜像自包含之外的全部运行态）：
  - `<app>/data` → `/app/data`（SQLite、媒体、评测产物）
  - `<app>/.env` → `/app/.env`（pydantic-settings 按进程 CWD 解析）
  - `/cache/g00619218/tools/hf-cache` → `/hf-cache`（e5 模型，`HF_HOME`）
- **容器进程 uid = mtuser(1321)**：`docker run --user 1321:1321`，与挂载目录属主
  一致，data 可写、镜像层只读。
- **权限模型**：mtuser 不在 docker 组（用户拍板最小化）。git 操作走 mtuser，
  docker 操作一律走本地 `deploy-remote/root_exec.py`（pty+su root 通道）。
  日常部署用本地一键驱动 `deploy-remote/deploy-docker.py`，不需要手敲 root。

## 前置（一次性，已完成）

1. docker daemon 已有 `data-root: /cache/docker`（根分区仅 312M，勿删此配置）。
2. 基础镜像经 daocloud 拉 + retag（幂等，部署驱动会自动补）：
   ```bash
   docker pull docker.m.daocloud.io/library/node:24-slim \
     && docker tag docker.m.daocloud.io/library/node:24-slim node:24-slim
   docker pull docker.m.daocloud.io/library/python:3.12-slim \
     && docker tag docker.m.daocloud.io/library/python:3.12-slim python:3.12-slim
   ```
3. 应用目录 git 化：
   ```bash
   cd /cache/g00619218/intelligent-tag-platform
   git init
   git remote add origin https://gh-proxy.com/https://github.com/morning-gu/intelligent-tag-platform.git
   git fetch origin main
   git checkout -B main origin/main   # 首次会拒绝覆盖有差异文件并列清单 → 审计 → -f 对齐
   ```
   `data/ .venv .env server.log* server.pid deploy/` 均在树外，git 覆盖不伤数据。

## 日常部署（本地一条命令）

```bash
python deploy-remote/deploy-docker.py            # 部署 origin/main
python deploy-remote/deploy-docker.py feat/xxx   # 或指定分支/短 SHA
```

驱动内部：mtuser 侧 `git fetch + reset --hard <ref>` → root 侧补基础镜像 →
`docker build -t tagplatform:<短SHA>` → `docker stop/rm tagplatform` →
`docker run -d --name tagplatform --restart unless-stopped --user 1321:1321
-p 8100:8100 <挂载三件套> tagplatform:<短SHA>` → 等 `/health`。

## 数据库迁移（README 铁律：先迁移、后启动）

含 alembic 变更的版本，起容器**前**执行：

```bash
# root_exec.py run "docker run --rm --user 1321:1321 <同款挂载>
#   -v <app>/.env:/app/.env tagplatform:<SHA> python -m alembic upgrade head"
```

（一次性容器跑完即删；`alembic.ini` / `alembic/` 已在镜像内。）

## 冒烟

```bash
curl -s http://9.15.154.212:8100/health                  # {"status":"ok"}（勿用 /api/v1/health——会落 SPA）
curl -s -c /tmp/jar -X POST http://9.15.154.212:8100/api/v1/auth/login \
  -H 'Content-Type: application/json' -d '{"username":"admin","password":"admin123"}'
curl -s -b /tmp/jar http://9.15.154.212:8100/api/v1/...  # 带 cookie 的业务路由
docker logs tagplatform --tail 50                        # root 通道
```

## 回滚

- **回到上一版镜像**：`docker stop tagplatform && docker rm tagplatform`，
  再按同款 run 命令起旧 `<SHA>` tag（镜像按提交 SHA 留存）。
- **回到裸机 uvicorn**：`docker stop tagplatform && bash stop.sh && bash start.sh`
  （旧流程原样在，`.venv` 未动）。

## 已知限制 / 待办

- torch 走清华 pypi 默认轮子（含 CUDA 库，镜像 ~6G；本机无 NVIDIA 也不影响 CPU
  推理）。后续可换 CPU-only 索引瘦身，非必须。
- 服务器 git 走 gh-proxy.com 第三方镜像，镜像挂了拉不动（本地同样依赖它）。
- docker build/stop/run 均经 root 通道（密码在 root_exec.py，注意该目录保密）。
