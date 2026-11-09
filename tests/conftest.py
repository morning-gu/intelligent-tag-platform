"""pytest 公共 fixture。

`client` fixture 基于临时文件库 + mock 嵌入器 + mock LLM 配置构建 FastAPI
应用（真 app、真文件 DB），经 TestClient 在进程内发请求（不起真实服务端口）。
注：计划原文为 `httpx.Client(transport=ASGITransport(app=...))`，但 httpx 的
ASGITransport 仅支持异步（无同步 handle_request），故改用 FastAPI 官方推荐的
TestClient——同为 httpx 之上的同步封装，契约（同步、进程内、临时库）不变。
应用本身不建表（生产由 alembic 迁移管理），fixture 先按 `Base.metadata`
对临时库建表再启动 app。
`session` fixture 为 ORM 测试提供函数级隔离的内存库会话：每测试新建
`:memory:` 引擎并按 `Base.metadata` 建表，测试间零残留。
"""

import os
import tempfile

import pytest
from fastapi.testclient import TestClient

from tagplatform.embed import make_embedder
from tagplatform.llm import MockLLMClient
from tagplatform.settings import Settings

# 测试隔离：mtg 流水线的审计报告与数据集根都指向临时目录，
# 避免单测往仓库里写文件、或误扫 /cache/MovieNet（零网络、零外部依赖红线）。
os.environ.setdefault("TAGPLT_MTG_REPORT_DIR", tempfile.mkdtemp(prefix="mtg-audit-"))
os.environ.setdefault("TAGPLT_DATASET_ROOT", tempfile.mkdtemp(prefix="mtg-dataset-"))

# 治理流水线（Task 7）等测试共用的确定性 mock 嵌入器（零网络、零模型下载）
MOCK_EMBEDDER = make_embedder("mock", mock=True)

# 生成流水线（Task 5/6/8）等测试共用的确定性 mock LLM 客户端（零网络、零随机源）
MOCK_LLM = MockLLMClient()


@pytest.fixture(autouse=True)
def tag_library(tmp_path):
    """每个测试一份临时标签库（JSON 夹具 + 一个"无 session/client 时"的兜底库）。

    不请求 `session` / `client` 的用例（如纯召回/对齐单测）直接
    `try_load_library()`，读的就是这里绑定的临时库；`session` / `client`
    fixture 之后会再绑它们自己的引擎并灌默认行（那一步受
    `@pytest.mark.empty_library` 控制，避免污染"空库"用例）。
    """
    from tagplatform import library as library_mod
    from tagplatform.db import create_engine_for
    from tagplatform.db.models import Base
    from tests.library_fixture import bind_engine, unbind_engine, write_library

    engine = create_engine_for(str(tmp_path / "taglibrary.db"))
    Base.metadata.create_all(engine)
    bind_engine(engine)

    root = write_library(tmp_path / "taglibrary")

    library_mod._CACHE.clear()
    library_mod._INDEX_CACHE.clear()
    library_mod._DIR_VECTOR_CACHE.clear()
    yield root
    unbind_engine(engine)
    engine.dispose()
    library_mod._CACHE.clear()
    library_mod._INDEX_CACHE.clear()
    library_mod._DIR_VECTOR_CACHE.clear()


@pytest.fixture
def client(tmp_path, request):
    """进程内同步测试客户端：真 app + 临时文件 DB + mock 嵌入器 + mock LLM。

    API 层（Task 10/11）的导入/批次/标签/生成路由需要文件库与嵌入器/LLM：
    - database_path 指向 tmp_path 下的临时文件（每测试隔离，测试间零残留）；
    - use_mock_embedder=True 使 create_app 构建确定性 MockEmbedder；
    - use_mock_llm=True 使 create_app 构建确定性 MockLLMClient（零网络，
      生成路由端到端可用，Task 9）；
    - generation_workers=0 钉死同步执行：executor.submit 返回即任务完成，
      POST /generation/tasks 响应已是终态（T9 的端到端测试模式）；
    - 表结构先经 Base.metadata.create_all 写入临时库（应用不负责建表）；
    - 启动（lifespan 引导 admin）后即以 admin 登录：cookie 随 TestClient
      会话自动携带，既有 API 测试断言零改动即处于已认证上下文（阶段 3 B2
      全 API 保护）。密码与 lifespan 同源（TAGPLT_ADMIN_PASSWORD，默认
      admin123）——在 fixture 内读取环境变量而非依赖测试内 monkeypatch
      （fixture 与测试体的执行顺序不保证）。
    """
    # api 延迟 import：与 session/content_fixture 同理——顶部 import 会经
    # tagplatform.api → tasks/routers 拉起全链路服务模块，其 models 导入在
    # 表结构切换期（Task 1-10）未就位时拖垮整个测试套件的收集；放进
    # fixture 体内只影响使用 client 的测试。
    from tagplatform.api import create_app
    from tagplatform.db import create_engine_for
    from tagplatform.db.models import Base

    db_path = tmp_path / "tagplatform.db"
    settings = Settings(database_path=str(db_path), use_mock_embedder=True,
                        use_mock_llm=True, generation_workers=0,
                        # 数据集根指向空目录：上传不会误命中真实 MovieNet，
                        # 也避免测试去扫 /cache/MovieNet
                        dataset_root=str(tmp_path / "movienet"))
    engine = create_engine_for(str(db_path))
    Base.metadata.create_all(engine)
    engine.dispose()  # 建表引擎即弃，app 内部自建引擎

    app = create_app(settings)
    # 标签库两表是 pre_asset_tag 的外键父表：把 app 引擎绑定到测试库，
    # 非「空库」用例再灌一遍默认库（测试中途 write_library 会自动同步）
    from tests.library_fixture import bind_engine, seed_database, unbind_engine

    bind_engine(app.state.engine)
    if not request.node.get_closest_marker("empty_library"):
        seed_database(app.state.engine)
    with TestClient(app) as http_client:
        login = http_client.post("/api/v1/auth/login", json={
            "username": "admin",
            "password": os.environ.get("TAGPLT_ADMIN_PASSWORD", "admin123"),
        })
        assert login.status_code == 200, f"client fixture 登录失败：{login.text}"
        yield http_client
    unbind_engine(app.state.engine)
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
def session(tag_library, request):
    """函数级隔离的内存库 ORM 会话（每测试重建库与表）。

    models 延迟 import：models.py 属 Task 3 产物，若放在模块顶部，
    其未就位时整个测试套件的收集都会失败；放进 fixture 体内只影响
    使用 session 的测试。

    引擎绑成标签库"当前数据源"，并灌一遍默认库（目录 + 标签），这样
    `pre_asset_tag` 的外键父行、以及“库内已有标签”的用例都不必各自造数据。
    断言"空库"行数的模块用 `@pytest.mark.empty_library` 关掉这次预置。
    """
    from tagplatform.db import create_engine_for, make_session_factory
    from tagplatform.db.models import Base
    from tests.library_fixture import bind_engine, seed_database, unbind_engine

    engine = create_engine_for(":memory:")
    Base.metadata.create_all(engine)
    bind_engine(engine)
    if not request.node.get_closest_marker("empty_library"):
        seed_database(engine)
    factory = make_session_factory(engine)
    with factory() as orm_session:
        yield orm_session
    unbind_engine(engine)
    engine.dispose()
