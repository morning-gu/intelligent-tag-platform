# 部署手册（deploy.md）

Ubuntu 服务器直接部署 runbook：**venv + systemd + nginx**（无 Docker 依赖）。
本文与 `deploy/tagplatform.service`、`deploy/nginx-tagplatform.conf`、
`deploy/requirements.lock` 配套，占位路径统一为 `/opt/tagplatform`（按需替换）。

架构一句话：nginx 监听 80 托管 `frontend/dist` 静态产物（SPA）并把 `/api`
反代到本机 uvicorn（127.0.0.1:8000）；uvicorn 以 systemd 服务常驻，启动前
自动跑 alembic 迁移；数据落 SQLite 文件。

## 0. 前置要求

| 组件 | 版本 | 说明 |
|---|---|---|
| Python | >= 3.11 | `pyproject.toml` 要求 |
| Node.js | >= 20.19 | Vite 8 构建要求（前端构建用，服务器只需构建一次） |
| nginx | any | `sites-available` 目录布局（Ubuntu 默认） |

```bash
# 系统依赖（Python 版本按发行版调整；Node 走 NodeSource 或 nvm，apt 自带版本常过旧）
sudo apt update
sudo apt install -y git nginx python3 python3-venv
```

## 1. 取得代码与 Python 依赖

```bash
sudo mkdir -p /opt/tagplatform
sudo chown "$USER" /opt/tagplatform
git clone <仓库地址> /opt/tagplatform
cd /opt/tagplatform

# 虚拟环境（路径固定为 .venv——systemd unit 的 ExecStart/ExecStartPre 引用它）
python3 -m venv .venv
.venv/bin/pip install --upgrade pip

# 两步装依赖（顺序可换，两步都要）：
# 1) lock 全量 pin 安装（版本与验证过的环境完全一致）；
# 2) editable 安装本包——tagplatform 自身不在 requirements.lock 里
#    （pip freeze 会列出本包的 -e 行，生成 lock 时已手动剔除），必须单独装。
.venv/bin/pip install -r deploy/requirements.lock
.venv/bin/pip install -e .
```

> torch 体积提示：lock 里 pin 了 torch，Linux 上默认拉 CUDA 版轮子（含
> nvidia-* 依赖，数 GB）。纯 CPU 服务器可自行改配 CPU 轮源后重装。

## 2. 配置环境变量

两种方式任选（同一变量只在其中一处配置，勿双源并存）：

- **方式 A（推荐，不进 unit 文件）**：`cp .env.example .env` 后填入
  `TAGPLT_DATABASE_PATH`、`TAGPLT_SESSION_SECRET`（强随机）、
  `TAGPLT_ADMIN_PASSWORD`（强密码）、`OPENAI_BASE_URL`、`OPENAI_API_KEY`；
  systemd unit 的密钥类注释行保持注释，另加一行
  `EnvironmentFile=/opt/tagplatform/.env`（去掉注释）。
  注意：库路径若改用 .env 提供，需同步**修改/删除** unit 里的
  `TAGPLT_DATABASE_PATH` 生效行——unit 生效 Environment 行与 .env 双源
  并存时总有一处静默失效（勿留两处不同值的"真相"）。
- **方式 B**：直接编辑 `deploy/tagplatform.service`，把 `TAGPLT_SESSION_SECRET`、
  `TAGPLT_ADMIN_PASSWORD`、`OPENAI_*` 四行注释取消并填入真实值。

生产必改项（默认值仅供开发/测试）：

- `TAGPLT_SESSION_SECRET`：默认 `dev-secret` 泄漏即可伪造任意用户登录态；
- `TAGPLT_ADMIN_PASSWORD`：默认 `admin123`，首管理员引导密码。

## 3. 数据库迁移（必须先于启动）

```bash
# TAGPLT_DATABASE_PATH 指定库文件位置（父目录自动创建）；与应用共用同一变量
TAGPLT_DATABASE_PATH=/opt/tagplatform/data/tagplatform.db \
  .venv/bin/python -m alembic upgrade head
```

> 顺序红线：应用启动即查询业务表，空库（未迁移）直接崩溃。systemd unit 的
> `ExecStartPre` 每次启动都会自动重跑迁移，此步只在首次部署手动执行一次
> （或提前验证迁移无报错）。

## 4. 前端构建

```bash
cd /opt/tagplatform/frontend
npm ci        # 按 package-lock.json 精确安装
npm run build # tsc 类型检查 + 产物输出 frontend/dist/
```

产物 `frontend/dist/` 为纯静态文件，由 nginx 直接托管。

## 5. 启用 systemd 服务

```bash
# 5.1 先按第 2 节改好 unit 文件（部署路径 / 运行用户 / 密钥），再复制或软链：
sudo ln -s /opt/tagplatform/deploy/tagplatform.service /etc/systemd/system/tagplatform.service

# 5.2 创建数据目录并赋权给运行用户（SQLite 与媒体文件都落在这里）
sudo mkdir -p /opt/tagplatform/data/media
sudo chown -R CHANGE_ME:CHANGE_ME /opt/tagplatform/data

# 5.3 启用并启动
sudo systemctl daemon-reload
sudo systemctl enable --now tagplatform
sudo systemctl status tagplatform        # 确认 active (running)
journalctl -u tagplatform -f             # 跟踪日志（可选）
```

## 6. 启用 nginx 站点

```bash
sudo ln -s /opt/tagplatform/deploy/nginx-tagplatform.conf /etc/nginx/sites-available/tagplatform
sudo ln -s /etc/nginx/sites-available/tagplatform /etc/nginx/sites-enabled/tagplatform
sudo nginx -t                            # 语法自检（the configuration file ... syntax is ok）
sudo systemctl reload nginx
```

## 7. 验证

```bash
# 7.1 后端存活（绕过 nginx 直连 uvicorn）
curl http://127.0.0.1:8000/health        # 期望 {"status":"ok"}

# 7.2 nginx 静态托管与反代（服务器本机或远程）
curl -I http://<服务器IP>/                # 期望 200，返回 index.html
curl -i http://<服务器IP>/api/v1/tags     # 期望 401（JSON detail"未登录或会话已过期"）
#                                         ——反代连通且鉴权生效；浏览器登录后访问即 200
# 注意：后端健康路由挂在根路径 /health（无 /api 前缀、不经鉴权），反代侧
# 不可达——/api/health 必 404、/health 会被 SPA 静态托管接住，健康检查
# JSON 只走 7.1 的直连
```

- 浏览器打开 `http://<服务器IP>/`，用 admin 登录（密码 = 部署时设置的
  `TAGPLT_ADMIN_PASSWORD`）；会话为 HttpOnly cookie，401 统一跳登录页。
- 首次接真实 LLM 前冒烟可在 `.env` 临时加 `TAGPLT_USE_MOCK_LLM=true`
  （零网络验证全链路，验证完删除，详见 README「快速启动」）。

## 8. 数据备份与恢复

平台全部持久化状态 = **SQLite 数据库文件 + `data/media` 目录**（媒体文件
不入库，video_uri 只存路径），备份即拷贝二者：

```bash
# 建议先停写入（避免拷到半截事务）：sudo systemctl stop tagplatform
# 在线备份 SQLite 可用 sqlite3 <db> ".backup /backup/tagplatform-<日期>.db"
cp /opt/tagplatform/data/tagplatform.db  /backup/tagplatform-$(date +%F).db
cp -r /opt/tagplatform/data/media        /backup/media-$(date +%F)/
```

恢复 = 停服务 → 用备份覆盖这两个位置（库文件名与 `TAGPLT_DATABASE_PATH`
一致）→ `systemctl start tagplatform`。迁移版本随库文件走（alembic_version
表在库内），旧库 + 新代码先跑 `alembic upgrade head` 再启动（unit 的
ExecStartPre 会自动做）。

## 9. 关于容器化

本平台无 Docker 依赖（SQLite 文件 + 静态前端 + 单进程 uvicorn），如需容器化
可后续补充 Dockerfile 与 compose 编排，本文不涉及。
