"""FastAPI 应用装配层（本包是唯一允许 import fastapi 的应用组装入口）。

依赖注入（供各 router `Depends` 引用）：
- `get_settings_dep`：每请求取 app 级 Settings；
- `get_session`：每请求 ORM 会话（请求结束自动关闭，不做隐式提交——
  写路由自行 commit）；
- `get_embedder`：app 级嵌入器单例（`create_app` 按
  `settings.use_mock_embedder` 构建一次，mock 路径零模型下载）；
- `get_executor`：app 级生成任务执行器单例（lifespan 启动时按
  `settings.generation_workers` 构建，0 = 同步执行，Task 9）；
- `get_current_user` / `require_admin`（api/depends.py）：会话认证两级
  门槛（阶段 3 B2）。

生命周期（lifespan）：启动时先做首管理员引导（user_account 空表经
`bootstrap_admin` 创建 admin，密码取 `TAGPLT_ADMIN_PASSWORD`，默认
admin123），随后构建执行器并孤儿清理（进程重启留下的 running 任务复位
failed）；关停时 `executor.shutdown()` 等待在途任务。

全 API 保护（阶段 3 B2）：除 `/health` 外的全部业务 router 在 create_app
include 时统一挂 `dependencies=[Depends(get_current_user)]`（一处 include
统一做法，后续新 router 同样在此挂）；auth router 例外——login 是取会话
的入口必须开放，/me 经端点级 get_current_user 自行保护，logout 仅清 cookie。

注意：本模块的 depends/router 导入语句必须放在依赖函数定义**之后**——
depends 与 router 模块会 `from tagplatform.api import get_session` 反向
引用本模块，若先导入则依赖函数尚不存在（循环导入）。
"""

import os
from collections.abc import Iterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from sqlalchemy.orm import Session

from tagplatform.auth import bootstrap_admin
from tagplatform.db import create_engine_for, make_session_factory
from tagplatform.embed import Embedder, make_embedder
from tagplatform.llm import make_llm_client
from tagplatform.settings import Settings, get_settings
from tagplatform.tasks import TaskExecutor, reset_orphan_running


def get_settings_dep(request: Request) -> Settings:
    """请求级配置依赖：返回挂到 app.state 的 Settings 实例（每 app 一份）。"""
    return request.app.state.settings


def get_session(request: Request) -> Iterator[Session]:
    """每请求 ORM 会话依赖：从 app 级会话工厂取会话，请求结束自动关闭。"""
    session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()


def get_embedder(request: Request) -> Embedder:
    """嵌入器依赖：app 启动时按 settings 构建的单例（见 create_app）。"""
    return request.app.state.embedder


def get_executor(request: Request) -> TaskExecutor:
    """生成任务执行器依赖：lifespan 启动时构建的单例（见 create_app）。"""
    return request.app.state.executor


from tagplatform.api.depends import get_current_user, require_admin  # noqa: E402

from tagplatform.api.routers import (  # noqa: E402  (须在依赖定义之后)
    auth,
    batches,
    content,
    generation,
    health,
    prompts,
    tags,
    users,
)


def create_app(settings: Settings | None = None) -> FastAPI:
    """构建 FastAPI 应用并装配全部路由。

    未传入 settings 时回落到全局 `get_settings()`；
    传入的自定义 settings（如测试用临时文件库）挂到 `app.state.settings`
    供依赖取用。app 级资源（引擎/会话工厂/嵌入器/LLM 客户端）各构建一次
    挂 state：
    - 引擎与会话工厂绑定 settings.database_path（文件库父目录由
      create_engine_for 确保；表结构由 alembic 迁移管理，应用不建表）；
    - 嵌入器按 use_mock_embedder 构建单例（mock 用于测试与冒烟）；
    - LLM 客户端按 use_mock_llm 构建单例（mock 零网络，测试与冒烟）。
    lifespan：启动时先做首管理员引导（空表创建 admin，密码取
    TAGPLT_ADMIN_PASSWORD，默认 admin123），随后按 generation_workers 构建
    TaskExecutor（0 = 同步执行）并孤儿清理（running 任务复位 failed）；关停
    时关线程池等待在途任务结束。
    """
    if settings is None:
        settings = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """启动：首管理员引导（最前）+ executor 构建 + 孤儿清理；关停：线程池等待在途任务。"""
        # 启动引导放最前（先于 executor 构建与孤儿清理）：user_account 空表
        # 时创建首管理员 admin（密码取 TAGPLT_ADMIN_PASSWORD，默认 admin123，
        # 生产环境务必经环境变量改密——README 注明）
        session = app.state.session_factory()
        try:
            bootstrap_admin(
                session,
                os.environ.get("TAGPLT_ADMIN_PASSWORD", "admin123"))
        finally:
            session.close()
        app.state.executor = TaskExecutor(
            app.state.session_factory, app.state.llm, app.state.embedder,
            workers=settings.generation_workers)
        session = app.state.session_factory()
        try:
            reset_orphan_running(session)
        finally:
            session.close()
        yield
        app.state.executor.shutdown()

    app = FastAPI(title="影视智能标签平台", lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = create_engine_for(settings.database_path)
    app.state.session_factory = make_session_factory(app.state.engine)
    app.state.embedder = make_embedder(
        settings.embedding_model, mock=settings.use_mock_embedder)
    app.state.llm = make_llm_client(settings)
    # /health 开放（存活探测无需登录）；其余业务 router 统一挂会话保护
    # （一处 include 统一做法；users router 的 admin 门槛在其端点级
    # require_admin——依赖缓存保证 get_current_user 每请求只解析一次）
    protected_deps = [Depends(get_current_user)]
    app.include_router(health.router)
    app.include_router(tags.router, dependencies=protected_deps)
    app.include_router(batches.router, dependencies=protected_deps)
    app.include_router(content.router, dependencies=protected_deps)
    app.include_router(generation.router, dependencies=protected_deps)
    app.include_router(prompts.router, dependencies=protected_deps)
    app.include_router(users.router, dependencies=protected_deps)
    # auth router：login 必须开放（见模块 docstring），/me 端点级自保护
    app.include_router(auth.router)
    return app
