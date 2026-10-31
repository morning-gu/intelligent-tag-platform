"""审核操作 API 测试（Task 4）。

覆盖契约（错误映射 + 批量逐条独立）：
- POST /api/v1/asset-tags/{id}/review：approve 200 回行摘要（与
  AssetTagOut 同形）、重复审核 409（Review Focus 1 并发保护，detail
  "该标签已被处理"）、未知行 404、非法 action 422、modify 空值 422、
  merge 缺/坏 merge_to 422、modify 成功回 pending；
- POST /api/v1/asset-tags/batch-review：逐条独立不中断（Review Focus
  4）——混合未知 id / 部分冲突均回 200 且 per-item ok=false 携带 detail，
  action 级 422 整请求拒绝且零副作用（DB 仍全 pending）、空 ids 422；
- reviewer 取会话用户名（client fixture 已 admin 登录 → reviewer_id
  "admin"）；未登录 401（create_app protected_deps 统一挂载验证）。

播种模式（计划指定）：参照 tests/test_results.py 的父行播种——经
client.app.state.session_factory 直接向 client fixture 的临时文件库补
MediaAsset/TagCatalog FK 父行与 AssetTag pending 行（审核语义测试无需
经生成 API 造数，零 MockLLM 采样依赖）。
"""

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from tagplatform.db.models import AssetTag, MediaAsset, ReviewRecord, TagCatalog

_NOW = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)

# 行摘要键集契约（与 generation.py 的 AssetTagOut 同形——Task 4 响应形状）
ASSET_TAG_KEYS = {"id", "tag_name", "origin", "relevance_score", "confidence",
                  "review_status", "evidence", "tag_id", "candidate_name"}


def _seed_rows(client, count=1, origin="generated") -> list[int]:
    """向 client fixture 的临时文件库播种 pending asset_tag 行，返回行 id 列表。

    外键父行（MediaAsset/TagCatalog）flush 后再插 AssetTag（外键开启）；
    catalog 行挂 tag_1、generated 行带候选字段。"""
    factory = client.app.state.session_factory
    with factory() as s:
        s.add(MediaAsset(asset_id="a1", asset_type="movie", title="测试影片",
                         created_at=_NOW))
        s.add(TagCatalog(tag_id="tag_1", tag_name="悬疑", tag_type="genre",
                         description="以悬念和谜题为核心"))
        s.flush()
        ids = []
        for i in range(count):
            is_generated = origin == "generated"
            row = AssetTag(
                asset_id="a1",
                tag_id=None if is_generated else "tag_1",
                candidate_name=f"候选{i}" if is_generated else None,
                candidate_type="narrative" if is_generated else None,
                candidate_description="候选描述" if is_generated else None,
                origin=origin,
                relevance_score=None if is_generated else 0.9,
                confidence=0.8,
                evidence_json="[]",
                review_status="pending",
                created_at=_NOW,
                updated_at=_NOW,
            )
            s.add(row)
            s.flush()
            ids.append(row.id)
        s.commit()
    return ids


# ---------- 单条审核 ----------


def test_review_approve_returns_row_summary(client):
    """approve 200：响应与 AssetTagOut 同形、状态落库、审核记录带
    reviewer（会话用户名 admin）与 record_id 前缀。"""
    (row_id,) = _seed_rows(client)
    resp = client.post(f"/api/v1/asset-tags/{row_id}/review",
                       json={"action": "approve"})
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == ASSET_TAG_KEYS
    assert body["id"] == row_id
    assert body["review_status"] == "approved"
    assert body["origin"] == "generated"
    assert body["candidate_name"] == "候选0"

    with client.app.state.session_factory() as s:
        rec = s.query(ReviewRecord).filter_by(result_id=row_id).one()
        assert rec.record_id.startswith("rev_")
        assert rec.action == "approve" and rec.reviewer_id == "admin"


def test_review_conflict_409(client):
    """Review Focus 1：同一行二次审核 → 409（真实冲突：第一次已把行置
    approved 并提交，第二次命中非 pending 前置）。"""
    (row_id,) = _seed_rows(client)
    first = client.post(f"/api/v1/asset-tags/{row_id}/review",
                        json={"action": "approve"})
    assert first.status_code == 200
    second = client.post(f"/api/v1/asset-tags/{row_id}/review",
                         json={"action": "approve"})
    assert second.status_code == 409
    assert second.json()["detail"] == "该标签已被处理"


def test_review_unknown_id_404(client):
    resp = client.post("/api/v1/asset-tags/999/review",
                       json={"action": "approve"})
    assert resp.status_code == 404
    assert "999" in resp.json()["detail"]


def test_review_invalid_action_422(client):
    (row_id,) = _seed_rows(client)
    resp = client.post(f"/api/v1/asset-tags/{row_id}/review",
                       json={"action": "alien"})
    assert resp.status_code == 422
    # 零副作用：行未被处理
    with client.app.state.session_factory() as s:
        assert s.get(AssetTag, row_id).review_status == "pending"


def test_review_modify_updates_and_stays_pending(client):
    """modify：候选字段更新、status 回 pending、comment/reviewer 落库。"""
    (row_id,) = _seed_rows(client)
    resp = client.post(f"/api/v1/asset-tags/{row_id}/review", json={
        "action": "modify",
        "payload": {"candidate_name": "多线叙事改", "comment": "措辞修正"}})
    assert resp.status_code == 200
    body = resp.json()
    assert body["review_status"] == "pending"
    assert body["candidate_name"] == "多线叙事改"

    with client.app.state.session_factory() as s:
        rec = s.query(ReviewRecord).filter_by(result_id=row_id).one()
        assert rec.action == "modify" and rec.comment == "措辞修正"
        assert rec.reviewer_id == "admin"


def test_review_modify_empty_422(client):
    """空 modify 值（无可改字段）→ 422（payload 缺失与仅备注两种）。"""
    (row_id,) = _seed_rows(client)
    for payload in [None, {}, {"comment": "仅备注"}]:
        resp = client.post(f"/api/v1/asset-tags/{row_id}/review",
                           json={"action": "modify", "payload": payload})
        assert resp.status_code == 422
    with client.app.state.session_factory() as s:
        assert s.query(ReviewRecord).count() == 0  # 零副作用


def test_review_merge_422_bad_or_missing_target(client):
    """merge 载荷校验：缺 merge_to_tag_id / 目标不在 tag_catalog → 422。"""
    (row_id,) = _seed_rows(client)
    missing = client.post(f"/api/v1/asset-tags/{row_id}/review",
                          json={"action": "merge", "payload": {}})
    assert missing.status_code == 422
    unknown = client.post(f"/api/v1/asset-tags/{row_id}/review", json={
        "action": "merge", "payload": {"merge_to_tag_id": "tag_none"}})
    assert unknown.status_code == 422
    with client.app.state.session_factory() as s:
        row = s.get(AssetTag, row_id)
        assert row.review_status == "pending" and row.tag_id is None


def test_review_merge_moves_to_catalog_tag(client):
    """merge 成功：tag_id=merge_to、candidate_* 清空、status → merged。"""
    (row_id,) = _seed_rows(client)
    resp = client.post(f"/api/v1/asset-tags/{row_id}/review", json={
        "action": "merge", "payload": {"merge_to_tag_id": "tag_1"}})
    assert resp.status_code == 200
    body = resp.json()
    assert body["review_status"] == "merged"
    assert body["tag_id"] == "tag_1"
    assert body["candidate_name"] is None

    with client.app.state.session_factory() as s:
        rec = s.query(ReviewRecord).filter_by(result_id=row_id).one()
        assert rec.action == "merge"


# ---------- 批量审核 ----------


def test_batch_review_per_item_independent(client):
    """批量逐条独立：未知 id 走 per-item 失败（ok=false + detail）不中断，
    成功条目照常落库；响应与 ids 等长同序。"""
    ids = _seed_rows(client, count=3)
    resp = client.post("/api/v1/asset-tags/batch-review", json={
        "ids": [ids[0], 999, ids[2]], "action": "approve"})
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert [r["id"] for r in results] == [ids[0], 999, ids[2]]
    assert results[0] == {"id": ids[0], "ok": True, "detail": None}
    assert results[1]["ok"] is False and "标签行不存在" in results[1]["detail"]
    assert results[2] == {"id": ids[2], "ok": True, "detail": None}

    with client.app.state.session_factory() as s:
        statuses = {r.id: r.review_status for r in s.query(AssetTag).all()}
        assert statuses[ids[0]] == "approved"
        assert statuses[ids[2]] == "approved"
        assert statuses[ids[1]] == "pending"  # 未被触及
        assert s.query(ReviewRecord).count() == 2


def test_batch_review_partial_conflict(client):
    """Review Focus 4：批量含已处理行 → 整请求 200，冲突条目 ok=false
    detail "该标签已被处理"，其余条目照常成功。"""
    ids = _seed_rows(client, count=2)
    first = client.post(f"/api/v1/asset-tags/{ids[0]}/review",
                        json={"action": "approve"})
    assert first.status_code == 200
    resp = client.post("/api/v1/asset-tags/batch-review", json={
        "ids": [ids[0], ids[1]], "action": "approve"})
    assert resp.status_code == 200  # 冲突不升级为请求级 409
    results = resp.json()["results"]
    assert results[0]["ok"] is False
    assert results[0]["detail"] == "该标签已被处理"
    assert results[1] == {"id": ids[1], "ok": True, "detail": None}


def test_batch_review_invalid_action_rejects_whole_request(client):
    """action 级 422 整请求拒绝：零部分执行（DB 仍全 pending）。"""
    ids = _seed_rows(client, count=2)
    resp = client.post("/api/v1/asset-tags/batch-review", json={
        "ids": ids, "action": "alien"})
    assert resp.status_code == 422
    with client.app.state.session_factory() as s:
        assert all(r.review_status == "pending" for r in s.query(AssetTag).all())
        assert s.query(ReviewRecord).count() == 0


def test_batch_review_empty_ids_422(client):
    resp = client.post("/api/v1/asset-tags/batch-review",
                       json={"ids": [], "action": "approve"})
    assert resp.status_code == 422


# ---------- 认证门槛 ----------


def test_review_requires_login(client):
    """未登录 401：新 TestClient 不带 cookie（create_app protected_deps
    统一挂载验证——review router 未被遗漏）。"""
    with TestClient(client.app) as fresh:
        assert fresh.post("/api/v1/asset-tags/1/review",
                          json={"action": "approve"}).status_code == 401
        assert fresh.post("/api/v1/asset-tags/batch-review",
                          json={"ids": [1], "action": "approve"}).status_code == 401
