"""pytest 公共 fixture。

`client` fixture 基于内存库配置构建 FastAPI 应用，经 TestClient 在进程内发请求
（不起真实服务端口）。
注：计划原文为 `httpx.Client(transport=ASGITransport(app=...))`，但 httpx 的
ASGITransport 仅支持异步（无同步 handle_request），故改用 FastAPI 官方推荐的
TestClient——同为 httpx 之上的同步封装，契约（同步、进程内、内存库）不变。
`session` fixture 为 ORM 测试提供函数级隔离的内存库会话：每测试新建
`:memory:` 引擎并按 `Base.metadata` 建表，测试间零残留。
"""

import pytest
from fastapi.testclient import TestClient

from tagplatform.api import create_app
from tagplatform.settings import Settings


@pytest.fixture
def client():
    """进程内同步测试客户端（内存库配置，避免触盘）。"""
    app = create_app(Settings(database_path=":memory:"))
    with TestClient(app) as http_client:
        yield http_client


@pytest.fixture
def session():
    """函数级隔离的内存库 ORM 会话（每测试重建库与表）。

    models 延迟 import：models.py 属 Task 3 产物，若放在模块顶部，
    其未就位时整个测试套件的收集都会失败；放进 fixture 体内只影响
    使用 session 的测试。
    """
    from tagplatform.db import create_engine_for, make_session_factory
    from tagplatform.db.models import Base

    engine = create_engine_for(":memory:")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    with factory() as orm_session:
        yield orm_session
    engine.dispose()
