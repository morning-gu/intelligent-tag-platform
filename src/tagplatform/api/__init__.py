"""FastAPI 应用装配层（本包是唯一允许 import fastapi 的应用组装入口）。

后续任务经 `create_app()` 构建 app、经 `get_settings()` 取配置。
"""

from fastapi import FastAPI

from tagplatform.api.routers import health
from tagplatform.settings import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    """构建 FastAPI 应用并装配全部路由。

    未传入 settings 时回落到全局 `get_settings()`；
    传入的自定义 settings（如测试用内存库）挂到 `app.state.settings` 供后续依赖取用。
    """
    if settings is None:
        settings = get_settings()
    app = FastAPI(title="影视智能标签平台")
    app.state.settings = settings
    app.include_router(health.router)
    return app
