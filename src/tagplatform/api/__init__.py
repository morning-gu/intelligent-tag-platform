"""FastAPI 应用装配层（本包是唯一允许 import fastapi 的应用组装入口）。

依赖注入（供各 router `Depends` 引用）：
- `get_settings_dep`：每请求取 app 级 Settings；
- `get_session`：每请求 ORM 会话（请求结束自动关闭，不做隐式提交——
  写路由自行 commit）；
- `get_embedder`：app 级嵌入器单例（`create_app` 按
  `settings.use_mock_embedder` 构建一次，mock 路径零模型下载）。

注意：本模块的 router 导入语句必须放在依赖函数定义**之后**——router
模块会 `from tagplatform.api import get_session` 反向引用本模块，若
router 先导入则依赖函数尚不存在（循环导入）。
"""

from collections.abc import Iterator

from fastapi import FastAPI, Request
from sqlalchemy.orm import Session

from tagplatform.db import create_engine_for, make_session_factory
from tagplatform.embed import Embedder, make_embedder
from tagplatform.settings import Settings, get_settings


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


from tagplatform.api.routers import batches, content, health, tags  # noqa: E402  (须在依赖定义之后)


def create_app(settings: Settings | None = None) -> FastAPI:
    """构建 FastAPI 应用并装配全部路由。

    未传入 settings 时回落到全局 `get_settings()`；
    传入的自定义 settings（如测试用临时文件库）挂到 `app.state.settings`
    供依赖取用。app 级资源（引擎/会话工厂/嵌入器）各构建一次挂 state：
    - 引擎与会话工厂绑定 settings.database_path（文件库父目录由
      create_engine_for 确保；表结构由 alembic 迁移管理，应用不建表）；
    - 嵌入器按 use_mock_embedder 构建单例（mock 用于测试与冒烟）。
    """
    if settings is None:
        settings = get_settings()
    app = FastAPI(title="影视智能标签平台")
    app.state.settings = settings
    app.state.engine = create_engine_for(settings.database_path)
    app.state.session_factory = make_session_factory(app.state.engine)
    app.state.embedder = make_embedder(
        settings.embedding_model, mock=settings.use_mock_embedder)
    app.include_router(health.router)
    app.include_router(tags.router)
    app.include_router(batches.router)
    app.include_router(content.router)
    return app
