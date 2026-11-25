"""clear-reset Task 2 API 测试：五端点 + 两预览的契约。

覆盖 spec §3.1/§3.3：counts 九键精确断言、preview 与 DELETE 同数、
404（未知资产）/409（running 生成构建、processing 导入）/403（reviewer）/
401（未登录）、queued 随删不设防、③ 后既有接口空库可用、③ 幸存
（导入批次/审计日志）。
"""

from fastapi.testclient import TestClient

import pytest

from tagplatform.capabilities.storage.models import (
    AssetBuildTask,
    AssetTag,
    MediaAsset,
    PreAssetTag,
    TagCatalog,
    TagChangeLog,
    TagDefine,
    TagGenerationTask,
    TagImportTask,
    TagQualityEvaluation,
)

# admin 专属的七个端点（403/401 用例逐一探测）
_ENDPOINTS = [
    ("delete", "/api/v1/assets/m1/tags"),
    ("delete", "/api/v1/content/tags"),
    ("delete", "/api/v1/tags"),
    ("delete", "/api/v1/assets/m1"),
    ("delete", "/api/v1/assets"),
    ("get", "/api/v1/content/clear-preview?scope=all-tags"),
    ("get", "/api/v1/tags/clear-preview"),
]


def _db(client):
    return client.app.state.session_factory()


def _seed(client):
    """形状同 Task 1 夹具：m1(movie)+s1/s2(scene)+m2(movie)，m1 全链数据。"""
    with _db(client) as s:
        s.add_all([
            MediaAsset(asset_id="m1", asset_type="movie", title="片一"),
            MediaAsset(asset_id="m1-s1", asset_type="scene", parent_id="m1",
                       title="场一"),
            MediaAsset(asset_id="m1-s2", asset_type="scene", parent_id="m1",
                       title="场二"),
            MediaAsset(asset_id="m2", asset_type="movie", title="片二"),
        ])
        task = TagGenerationTask(task_id="g1", asset_id="m1", mode="closed",
                                 status="succeeded")
        s.add(task)
        s.flush()
        s.add(TagQualityEvaluation(task_id="g1", status="completed",
                                   model="m", prompt_version="v", labels=[],
                                   scores={}, frame_paths={}))
        s.add_all([
            PreAssetTag(asset_id="m1", target_type="content", target_id="m1",
                        tag_id="tag_action", origin="closed",
                        review_status="approved"),
            PreAssetTag(asset_id="m1", target_type="content",
                        target_id="m1", tag_id="tag_drama", origin="closed",
                        review_status="pending"),
            AssetTag(asset_id="m1", target_type="content", target_id="m1",
                     tag_id="tag_action", tag_name="动作"),
            AssetBuildTask(task_id="b1", asset_id="m1", movie_id="tt1",
                           status="succeeded"),
        ])
        s.commit()


def test_clear_asset_tags_endpoint(client):
    _seed(client)
    resp = client.delete("/api/v1/assets/m1/tags")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["counts"] == {
        "pre_asset_tag": 2, "asset_tag": 1, "generation_tasks": 1,
        "quality_evaluations": 1, "asset_build_task": 0, "media_assets": 0,
        "scenes": 0, "tag_define": 0, "tag_catalog": 0,
    }
    with _db(client) as s:
        for model in (PreAssetTag, AssetTag, TagGenerationTask,
                      TagQualityEvaluation):
            assert s.query(model).count() == 0
        assert s.query(MediaAsset).count() == 4
        assert s.query(TagDefine).count() == 7


def test_clear_all_content_tags_endpoint(client):
    _seed(client)
    resp = client.delete("/api/v1/content/tags")
    assert resp.status_code == 200
    counts = resp.json()["counts"]
    assert counts["pre_asset_tag"] == 2
    assert counts["generation_tasks"] == 1
    with _db(client) as s:
        for model in (PreAssetTag, AssetTag, TagGenerationTask,
                      TagQualityEvaluation):
            assert s.query(model).count() == 0
        assert s.query(MediaAsset).count() == 4  # 资产幸存
        assert s.query(TagDefine).count() == 7  # 库幸存
        assert s.query(TagCatalog).count() == 5


def test_clear_library_endpoint(client):
    _seed(client)
    with _db(client) as s:
        s.add_all([
            TagImportTask(task_id="imp-ok", file_name="f.xlsx",
                          status="completed"),
            TagChangeLog(tag_id="tag_action", action="create"),
        ])
        s.commit()
    resp = client.delete("/api/v1/tags")
    assert resp.status_code == 200
    counts = resp.json()["counts"]
    assert counts["tag_catalog"] == 5
    assert counts["tag_define"] == 7
    assert counts["pre_asset_tag"] == 2
    with _db(client) as s:
        assert s.query(TagCatalog).count() == 0
        assert s.query(TagDefine).count() == 0
        for model in (PreAssetTag, AssetTag, TagGenerationTask,
                      TagQualityEvaluation):
            assert s.query(model).count() == 0
        assert s.query(TagImportTask).count() == 1  # 批次幸存
        assert s.query(TagChangeLog).count() == 1  # 审计日志幸存
        assert s.query(MediaAsset).count() == 4  # 资产幸存


def test_delete_asset_endpoint(client):
    _seed(client)
    resp = client.delete("/api/v1/assets/m1")
    assert resp.status_code == 200
    assert resp.json()["counts"] == {
        "pre_asset_tag": 2, "asset_tag": 1, "generation_tasks": 1,
        "quality_evaluations": 1, "asset_build_task": 1, "media_assets": 1,
        "scenes": 2, "tag_define": 0, "tag_catalog": 0,
    }
    with _db(client) as s:
        for asset_id in ("m1", "m1-s1", "m1-s2"):
            assert s.get(MediaAsset, asset_id) is None
        assert s.get(MediaAsset, "m2") is not None
        assert s.query(AssetBuildTask).count() == 0
        assert s.query(TagDefine).count() == 7  # 库幸存


def test_delete_all_assets_endpoint(client):
    _seed(client)
    resp = client.delete("/api/v1/assets")
    assert resp.status_code == 200
    counts = resp.json()["counts"]
    assert counts["media_assets"] == 2
    assert counts["scenes"] == 2
    assert counts["asset_build_task"] == 1
    with _db(client) as s:
        assert s.query(MediaAsset).count() == 0
        assert s.query(AssetBuildTask).count() == 0
        assert s.query(TagDefine).count() == 7  # 库幸存
        assert s.query(TagCatalog).count() == 5


def test_preview_matches_delete_counts(client):
    _seed(client)
    preview = client.get(
        "/api/v1/content/clear-preview?scope=all-assets")
    assert preview.status_code == 200
    pv = preview.json()
    assert pv["scope"] == "all-assets"
    deleted = client.delete("/api/v1/assets")
    assert deleted.json()["counts"] == pv["counts"]  # 九键逐项相等


def test_preview_asset_scopes(client):
    _seed(client)
    ok = client.get(
        "/api/v1/content/clear-preview?scope=asset-tags&asset_id=m1")
    assert ok.status_code == 200
    assert ok.json()["counts"]["pre_asset_tag"] == 2
    missing = client.get("/api/v1/content/clear-preview?scope=asset-tags")
    assert missing.status_code == 422
    bad_scope = client.get(
        "/api/v1/content/clear-preview?scope=library")
    assert bad_scope.status_code == 422
    unknown = client.get(
        "/api/v1/content/clear-preview?scope=asset&asset_id=nope")
    assert unknown.status_code == 404


def test_tags_preview_endpoint(client):
    _seed(client)
    resp = client.get("/api/v1/tags/clear-preview")
    assert resp.status_code == 200
    body = resp.json()
    assert body["scope"] == "library"
    assert body["counts"]["tag_define"] == 7
    assert body["counts"]["tag_catalog"] == 5
    assert body["counts"]["pre_asset_tag"] == 2


def test_unknown_asset_404(client):
    _seed(client)
    assert client.delete("/api/v1/assets/nope/tags").status_code == 404
    assert client.delete("/api/v1/assets/nope").status_code == 404


def test_running_tasks_409(client):
    _seed(client)
    with _db(client) as s:
        s.add(TagGenerationTask(task_id="g-run", asset_id="m1",
                                mode="closed", status="running"))
        s.commit()
    blocked = client.delete("/api/v1/assets/m1/tags")  # ① 作用域内 running
    assert blocked.status_code == 409
    assert "生成/构建任务正在执行" in blocked.json()["detail"]
    with _db(client) as s:
        s.add(AssetBuildTask(task_id="b-run", asset_id="m2",
                             movie_id="tt2", status="running"))
        s.commit()
    assert client.delete("/api/v1/content/tags").status_code == 409  # ②
    assert client.delete("/api/v1/assets").status_code == 409  # ⑤
    # ④ 单删该资产：b-run 挂在 m2，闭包内 running → 409
    assert client.delete("/api/v1/assets/m2").status_code == 409


def test_processing_import_409_library_only(client):
    _seed(client)
    with _db(client) as s:
        s.add(TagImportTask(task_id="imp-run", file_name="x.xlsx",
                            status="processing"))
        s.commit()
    blocked = client.delete("/api/v1/tags")  # ③
    assert blocked.status_code == 409
    assert "标签导入任务正在执行" in blocked.json()["detail"]
    fine = client.delete("/api/v1/content/tags")  # ② 导入不涉 content 域
    assert fine.status_code == 200


def test_queued_tasks_deleted_not_blocked(client):
    _seed(client)
    with _db(client) as s:
        s.add(TagGenerationTask(task_id="g-q", asset_id="m2",
                                mode="closed", status="queued"))
        s.commit()
    resp = client.delete("/api/v1/content/tags")
    assert resp.status_code == 200  # queued 不挡
    assert resp.json()["counts"]["generation_tasks"] == 2  # g1 + g-q
    with _db(client) as s:
        assert s.query(TagGenerationTask).count() == 0  # 行已删


def test_library_clear_keeps_api_usable(client):
    _seed(client)
    assert client.delete("/api/v1/tags").status_code == 200
    tags = client.get("/api/v1/tags")
    assert tags.status_code == 200
    assert tags.json()["items"] == []
    assert client.get("/api/v1/assets").status_code == 200
    assert client.delete("/api/v1/assets").status_code == 200  # ⑤
    assert client.get("/api/v1/tags").status_code == 200  # ⑤ 后库仍在


def test_admin_only_403(client):
    _seed(client)
    created = client.post("/api/v1/users", json={
        "username": "bob", "password": "pw-bob-1", "role": "reviewer"})
    assert created.status_code == 201 or created.status_code == 200
    login = client.post("/api/v1/auth/login", json={
        "username": "bob", "password": "pw-bob-1"})
    assert login.status_code == 200
    for method, path in _ENDPOINTS:
        resp = getattr(client, method)(path)
        assert resp.status_code == 403, f"{method} {path} → {resp.status_code}"


def test_requires_login_401(client):
    _seed(client)
    anon = TestClient(client.app)  # 不进 lifespan、不登录
    for method, path in _ENDPOINTS:
        resp = getattr(anon, method)(path)
        assert resp.status_code == 401, f"{method} {path} → {resp.status_code}"


# ---------- 终审 Important-1：同步导入的 processing 守卫锚点 ----------

class _FakeParsed:
    rows: list = []
    errors: list = []


class _FakeResult:
    task_id = ""


def _patch_sync_import(monkeypatch, run_governance_impl):
    """桩掉同步导入的解析与治理：parse_upload 返回空批，run_governance 可控。"""
    from tagplatform.orchestration import tags as tags_orch_mod

    monkeypatch.setattr(tags_orch_mod, "parse_upload",
                        lambda data, file_name: _FakeParsed())
    monkeypatch.setattr(tags_orch_mod, "run_governance", run_governance_impl)


def test_sync_import_writes_processing_row_during_governance(
        client, monkeypatch):
    """同步导入执行期间库内须有 processing 行——③ 清空标签库守卫的数据源。

    终审 Important-1：原实现治理全程不落 task 行（收尾才 completed 一次
    提交），③ 的 processing 守卫对同步导入在途窗口失效。
    """
    seen = {}

    def fake_run(rows, session, embedder, *, task=None,
                 file_name="governance", parse_errors=None):
        seen["status_at_run"] = task.status
        seen["task_id"] = task.task_id
        # 治理执行期间的"③ 并发查"视角：跨会话读 processing 行
        with _db(client) as other:
            seen["processing_rows_during"] = other.query(
                TagImportTask).filter_by(status="processing").count()
        result = _FakeResult()
        result.task_id = task.task_id
        # 收尾模仿 _write_import_summary 的最小行为（ImportSummary 契约）
        task.status = "completed"
        task.result_summary = (
            '{"total": 0, "inserted": 0, "skipped": 0, "issues": []}')
        session.add(task)
        session.commit()
        return result

    _patch_sync_import(monkeypatch, fake_run)
    resp = client.post("/api/v1/tags/import",
                       files={"file": ("tags.xlsx", b"stub", "application/octet-stream")})
    assert resp.status_code == 200
    assert seen["status_at_run"] == "processing"
    assert seen["processing_rows_during"] == 1
    assert resp.json()["task_id"] == seen["task_id"]


def test_sync_import_failure_marks_task_failed(client, monkeypatch):
    """治理中途异常：processing 行复位 failed（不留永久 processing 守卫死锁）。"""

    def fake_run(rows, session, embedder, *, task=None,
                 file_name="governance", parse_errors=None):
        raise RuntimeError("治理崩溃")

    _patch_sync_import(monkeypatch, fake_run)
    with pytest.raises(RuntimeError):
        client.post(
            "/api/v1/tags/import",
            files={"file": ("tags.xlsx", b"stub", "application/octet-stream")})
    with _db(client) as s:
        row = s.query(TagImportTask).one()
        assert row.status == "failed"
