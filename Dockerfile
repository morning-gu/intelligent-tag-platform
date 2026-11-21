# 影视智能标签平台——多阶段容器镜像
#
# 阶段 frontend：node 构建 dist（部署机无 node，node 由镜像提供）
# 阶段 runtime ：python 依赖 + 代码 + dist；src 经 PYTHONPATH 直挂，不做包安装
#
# 墙内部署机（github.com / docker.io 直连不可达）先经镜像源拉基础镜像并
# retag，详见 docs/containerized-deploy.md 与 deploy-remote/deploy-docker.py：
#   docker pull docker.m.daocloud.io/library/node:24-slim \
#     && docker tag docker.m.daocloud.io/library/node:24-slim node:24-slim
#   docker pull docker.m.daocloud.io/library/python:3.12-slim \
#     && docker tag docker.m.daocloud.io/library/python:3.12-slim python:3.12-slim

FROM node:24-slim AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --registry=https://registry.npmmirror.com
COPY frontend/ ./
RUN npm run build          # tsc -b && vite build → /build/dist

FROM python:3.12-slim AS runtime
WORKDIR /app
# 依赖层只依赖 pyproject.toml：日常代码变更命中层缓存，不重装依赖。
# 依赖清单由 tomllib 现解析，避免 pyproject 与 Dockerfile 双份清单漂移
# （仅 project.dependencies，dev 依赖不进运行时镜像）。
COPY pyproject.toml ./
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']))" > /tmp/deps.txt \
 && pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple -r /tmp/deps.txt

# 运行时环境：HF 缓存由 docker run 挂到 /hf-cache；.env 挂到 /app/.env
# （pydantic-settings env_file=".env" 相对进程 CWD=/app 解析）。
ENV PYTHONPATH=/app/src \
    HF_HOME=/hf-cache \
    HF_HUB_OFFLINE=1 \
    PYTHONUNBUFFERED=1

COPY src/ ./src/
COPY alembic/ alembic.ini run_server.py ./
COPY --from=frontend /build/dist ./frontend/dist

# 权限归一化：COPY 保留构建上下文的文件模式（部署机 umask 0077 → 640）并把
# 属主变为 root——容器进程以非 root uid（--user 1321）运行时会无权读取。
# 统一放开读 + 目录执行位，镜像权限自包含、不依赖宿主 umask。
RUN chmod -R a+rX /app

EXPOSE 8100
# 与裸机 start.sh 同构：单进程 uvicorn 同时托管 API 与 SPA
# （run_server.py 的 SPA 兜底路由按 /app/frontend/dist 解析，见该文件头部注释）。
CMD ["python", "-m", "uvicorn", "run_server:app", "--host", "0.0.0.0", "--port", "8100"]
