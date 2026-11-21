# 影视智能标签平台——多阶段容器镜像
#
# 阶段 frontend：node 构建 dist（构建在镜像内完成，部署机无需安装 node）
# 阶段 runtime ：python 依赖 + 代码 + dist；src 经 PYTHONPATH 直挂，不做包安装
#
# 若部署机无法直连 docker.io，可先从可用镜像源拉基础镜像并 retag：
#   docker pull <mirror>/library/node:24-slim     && docker tag <mirror>/library/node:24-slim node:24-slim
#   docker pull <mirror>/library/python:3.12-slim && docker tag <mirror>/library/python:3.12-slim python:3.12-slim

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
# torch 先从官方 CPU 索引单独装：无 NVIDIA GPU 的部署环境里，默认 pypi 轮子捆绑的
# ~8G CUDA 依赖是纯死重（镜像体积主因）。CPU torch 装好后，sentence-transformers
# 等依赖解析到已满足的版本，不再拉 nvidia-* 轮子；需 GPU 推理时改用默认索引。
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch \
 && python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']))" > /tmp/deps.txt \
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

# 权限归一化：COPY 会保留构建上下文的文件模式（受宿主 umask 影响）并把属主
# 变为 root——容器进程以非 root uid 运行时可能无权读取。统一放开读 + 目录
# 执行位，镜像权限自包含、不依赖宿主 umask。
RUN chmod -R a+rX /app

EXPOSE 8100
# 单进程 uvicorn 同时托管 API 与 SPA
# （run_server.py 的 SPA 兜底路由按 /app/frontend/dist 解析，见该文件头部注释）。
CMD ["python", "-m", "uvicorn", "run_server:app", "--host", "0.0.0.0", "--port", "8100"]
