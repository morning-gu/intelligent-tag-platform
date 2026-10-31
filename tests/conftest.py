"""pytest 公共 fixture。

`client` fixture 基于临时文件库 + mock 嵌入器配置构建 FastAPI 应用（真
app、真文件 DB），经 TestClient 在进程内发请求（不起真实服务端口）。
注：计划原文为 `httpx.Client(transport=ASGITransport(app=...))`，但 httpx 的
ASGITransport 仅支持异步（无同步 handle_request），故改用 FastAPI 官方推荐的
TestClient——同为 httpx 之上的同步封装，契约（同步、进程内、临时库）不变。
应用本身不建表（生产由 alembic 迁移管理），fixture 先按 `Base.metadata`
对临时库建表再启动 app。
`session` fixture 为 ORM 测试提供函数级隔离的内存库会话：每测试新建
`:memory:` 引擎并按 `Base.metadata` 建表，测试间零残留。
"""

import pytest
from fastapi.testclient import TestClient

from tagplatform.api import create_app
from tagplatform.embed import make_embedder
from tagplatform.settings import Settings

# 治理流水线（Task 7）等测试共用的确定性 mock 嵌入器（零网络、零模型下载）
MOCK_EMBEDDER = make_embedder("mock", mock=True)


@pytest.fixture
def client(tmp_path):
    """进程内同步测试客户端：真 app + 临时文件 DB + mock 嵌入器。

    API 层（Task 10）的导入/批次/标签路由需要文件库与嵌入器：
    - database_path 指向 tmp_path 下的临时文件（每测试隔离，测试间零残留）；
    - use_mock_embedder=True 使 create_app 构建确定性 MockEmbedder；
    - 表结构先经 Base.metadata.create_all 写入临时库（应用不负责建表）。
    """
    from tagplatform.db import create_engine_for
    from tagplatform.db.models import Base

    db_path = tmp_path / "tagplatform.db"
    settings = Settings(database_path=str(db_path), use_mock_embedder=True)
    engine = create_engine_for(str(db_path))
    Base.metadata.create_all(engine)
    engine.dispose()  # 建表引擎即弃，app 内部自建引擎

    app = create_app(settings)
    with TestClient(app) as http_client:
        yield http_client
    app.state.engine.dispose()  # 释放 app 级连接（Windows 文件句柄）


@pytest.fixture
def content_fixture():
    """解析 fixtures/content_movie.json 为 ContentJSON（内容导入测试输入）。

    parse_content 延迟 import：与 session fixture 同理，模型层未就位时
    只影响使用本 fixture 的测试，不拖垮整套件收集。
    """
    import json
    from pathlib import Path

    from tagplatform.schemas import parse_content

    fixture_path = Path(__file__).parent / "fixtures" / "content_movie.json"
    data = json.loads(fixture_path.read_text(encoding="utf-8"))
    return parse_content(data)


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
