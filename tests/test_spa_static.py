"""静态托管与 SPA 深链回归测试（run_server.py）。

历史故障：把 frontend/dist/assets 单独 mount 到 /assets，与前端路由
/assets/:id 同名冲突——点开影片详情后再刷新，请求被 StaticFiles 接管，
直接回 404 JSON（{"detail":"Not Found"}），页面白屏且无法恢复。

这里用临时 dist 目录直接驱动 run_server 的兜底路由，锁定三条契约：
1. SPA 深链（/assets/<id>、/review）回 index.html（text/html）；
2. 真实的构建产物（/assets/app.js）按文件返回，不被吞成 index.html；
3. 不允许 ../ 穿越到 dist 之外。
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def spa_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "assets" / "app.js").write_text("console.log('ok')", encoding="utf-8")
    (dist / "index.html").write_text("<!doctype html><div id=root>", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("nope", encoding="utf-8")

    # run_server 在 import 时构建真应用（要 DB）；这里替换成空 app，
    # 只验证静态兜底路由本身
    import tagplatform.api as api_pkg

    monkeypatch.setattr(api_pkg, "create_app", lambda: FastAPI())
    monkeypatch.syspath_prepend(str(REPO_ROOT))
    sys.modules.pop("run_server", None)
    module = importlib.import_module("run_server")
    module.DIST = dist

    return TestClient(module.app)


def test_spa_deep_link_returns_index_html(spa_client: TestClient) -> None:
    for path in ("/assets/asset_0e46b55f", "/assets/movie_tt0102926", "/review", "/tasks"):
        resp = spa_client.get(path)
        assert resp.status_code == 200, path
        assert resp.headers["content-type"].startswith("text/html"), path
        assert "<div id=root>" in resp.text, path


def test_static_asset_file_served_as_file(spa_client: TestClient) -> None:
    resp = spa_client.get("/assets/app.js")
    assert resp.status_code == 200
    assert "javascript" in resp.headers["content-type"]
    assert "console.log('ok')" in resp.text


def test_path_traversal_outside_dist_is_blocked(spa_client: TestClient) -> None:
    resp = spa_client.get("/../secret.txt")
    assert resp.status_code == 200
    assert "nope" not in resp.text
    assert "<div id=root>" in resp.text
