"""pytest 公共 fixture。

`client` fixture 基于内存库配置构建 FastAPI 应用，经 TestClient 在进程内发请求
（不起真实服务端口）。
注：计划原文为 `httpx.Client(transport=ASGITransport(app=...))`，但 httpx 的
ASGITransport 仅支持异步（无同步 handle_request），故改用 FastAPI 官方推荐的
TestClient——同为 httpx 之上的同步封装，契约（同步、进程内、内存库）不变。
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
