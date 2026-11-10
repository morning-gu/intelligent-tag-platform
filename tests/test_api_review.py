"""审核操作 API 与审核列表/筛选 API 测试（v2：pre_asset_tag 单一数据源）。

审核操作：
- approve / reject 落终态，reviewer_id / review_comment 写在行内（不再有
  审核流水表）；重复审核 409、未知行 404、非法 action 422；
- modify 改完直接 approved（v2 语义），空值/空串/超长 422，strip 落库；
- merge 并入库内既有标签 → tag_id 赋值、候选清空、approved；缺/坏目标
  422；撞同资产已挂标签 409；
- 批量逐条独立：未知 id / 冲突 / 撞索引均 per-item ok=false，action 级
  422 整请求拒绝且零副作用；
- reviewer 取会话用户名（client fixture 已 admin 登录）。

审核列表：
- 七筛选参数（status/confidence_min/confidence_max/tag_type/mode/asset_id/
  origin），默认 confidence 升序；
- tag_type 只来自库内标签（库外新词无类型），mode 与 origin 同义；
- 行形状含 target_type（本期恒 content）与 reviewer_id / review_comment。
"""

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from tagplatform.db.models import MediaAsset, PreAssetTag, TagGenerationTask

_NOW = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)

# 行摘要键集契约（与 generation.py 的 AssetTagOut 同形）
ASSET_TAG_KEYS = {"id", "tag_name", "origin", "relevance_score", "confidence",
                  "review_status", "target_type", "dir_id", "dir_name",
                  "evidence", "tag_id", "candidate_name", "created_at"}

REVIEW_ITEM_KEYS = {"id", "asset_id", "asset_title", "tag_name", "origin",
                    "mode", "tag_type", "confidence", "relevance",
                    "review_status", "target_type", "dir_id", "dir_name",
                    "dir_path", "evidence", "task_id", "reviewer_id",
                    "review_comment"}


def _seed_rows(client, count=1, origin="open") -> list[int]:
    """向 client fixture 的临时库播种 pending pre_asset_tag 行，返回行 id。"""
    factory = client.app.state.session_factory
    with factory() as s:
        s.add(MediaAsset(asset_id="a1", asset_type="movie", title="测试影片"))
        s.flush()
        ids = []
        for i in range(count):
            row = PreAssetTag(
                asset_id="a1",
                target_type="content",
                target_id="a1",
                tag_id=None,
                candidate_name=f"候选{i}",
                candidate_description="候选描述",
                candidate_dir_id="dir_theme",
                origin=origin,
                relevance_score=None,
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
    """approve 200：响应形状固定、状态落库、reviewer_id 取会话用户名。"""
    (row_id,) = _seed_rows(client)
    resp = client.post(f"/api/v1/asset-tags/{row_id}/review",
                       json={"action": "approve"})
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == ASSET_TAG_KEYS
    assert body["id"] == row_id
    assert body["review_status"] == "approved"
    assert body["origin"] == "open"
    assert body["candidate_name"] == "候选0"
    assert body["target_type"] == "content"

    with client.app.state.session_factory() as s:
        row = s.get(PreAssetTag, row_id)
        assert row.reviewer_id == "admin"


def test_review_conflict_409(client):
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
    with client.app.state.session_factory() as s:
        assert s.get(PreAssetTag, row_id).review_status == "pending"


def test_review_modify_updates_and_approves(client):
    """modify：字段更新后直接 approved（v2），备注与审核人落行内。"""
    (row_id,) = _seed_rows(client)
    resp = client.post(f"/api/v1/asset-tags/{row_id}/review", json={
        "action": "modify",
        "payload": {"candidate_name": "多线叙事改", "comment": "措辞修正"}})
    assert resp.status_code == 200
    body = resp.json()
    assert body["review_status"] == "approved"
    assert body["candidate_name"] == "多线叙事改"

    with client.app.state.session_factory() as s:
        row = s.get(PreAssetTag, row_id)
        assert row.review_status == "approved"
        assert row.review_comment == "措辞修正"
        assert row.reviewer_id == "admin"


def test_review_modify_empty_422(client):
    (row_id,) = _seed_rows(client)
    for payload in [None, {}, {"comment": "仅备注"}]:
        resp = client.post(f"/api/v1/asset-tags/{row_id}/review",
                           json={"action": "modify", "payload": payload})
        assert resp.status_code == 422
    with client.app.state.session_factory() as s:
        assert s.get(PreAssetTag, row_id).review_status == "pending"


def test_review_merge_422_bad_or_missing_target(client):
    (row_id,) = _seed_rows(client)
    missing = client.post(f"/api/v1/asset-tags/{row_id}/review",
                          json={"action": "merge", "payload": {}})
    assert missing.status_code == 422
    unknown = client.post(f"/api/v1/asset-tags/{row_id}/review", json={
        "action": "merge", "payload": {"merge_to_tag_id": "tag_none"}})
    assert unknown.status_code == 422
    with client.app.state.session_factory() as s:
        row = s.get(PreAssetTag, row_id)
        assert row.review_status == "pending" and row.tag_id is None


def test_review_merge_moves_to_catalog_tag(client):
    """merge 成功：tag_id=merge_to、候选清空、终态 approved。"""
    (row_id,) = _seed_rows(client)
    resp = client.post(f"/api/v1/asset-tags/{row_id}/review", json={
        "action": "merge", "payload": {"merge_to_tag_id": "tag_action"}})
    assert resp.status_code == 200
    body = resp.json()
    assert body["review_status"] == "approved"
    assert body["tag_id"] == "tag_action"
    assert body["tag_name"] == "动作" and body["dir_id"] == "dir_genre"
    assert body["candidate_name"] is None


# ---------- 落库撞部分唯一索引 ----------


def test_review_merge_onto_mounted_tag_409(client):
    """第二行 merge 到同资产已挂标签 → 409，行状态不变。"""
    ids = _seed_rows(client, count=2)
    first = client.post(f"/api/v1/asset-tags/{ids[0]}/review",
                        json={"action": "merge",
                              "payload": {"merge_to_tag_id": "tag_action"}})
    assert first.status_code == 200
    second = client.post(f"/api/v1/asset-tags/{ids[1]}/review",
                         json={"action": "merge",
                               "payload": {"merge_to_tag_id": "tag_action"}})
    assert second.status_code == 409
    assert second.json()["detail"] == "该资产已挂此标签"
    with client.app.state.session_factory() as s:
        row = s.get(PreAssetTag, ids[1])
        assert row.review_status == "pending" and row.tag_id is None
        assert row.candidate_name == "候选1"


def test_batch_review_index_conflict_per_item(client):
    """批量含撞索引条目：整请求 200，后撞条目 ok=false，行已回滚。"""
    ids = _seed_rows(client, count=2)
    resp = client.post("/api/v1/asset-tags/batch-review", json={
        "ids": ids, "action": "merge",
        "payload": {"merge_to_tag_id": "tag_action"}})
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert results[0] == {"id": ids[0], "ok": True, "detail": None}
    assert results[1]["ok"] is False
    assert results[1]["detail"] == "该资产已挂此标签"
    with client.app.state.session_factory() as s:
        statuses = {r.id: r.review_status for r in s.query(PreAssetTag).all()}
        assert statuses[ids[0]] == "approved"
        assert statuses[ids[1]] == "pending"


# ---------- modify 字符串字段校验 ----------


def test_review_modify_string_field_validation_422(client):
    (row_id,) = _seed_rows(client)
    cases = [
        {"tag_name": ""},
        {"candidate_name": "   "},
        {"tag_name": "长" * 201},
    ]
    for payload in cases:
        resp = client.post(f"/api/v1/asset-tags/{row_id}/review",
                           json={"action": "modify", "payload": payload})
        assert resp.status_code == 422, f"{payload} 应 422"
    with client.app.state.session_factory() as s:
        row = s.get(PreAssetTag, row_id)
        assert row.review_status == "pending"
        assert row.candidate_name == "候选0"


def test_review_modify_strips_value(client):
    (row_id,) = _seed_rows(client)
    resp = client.post(f"/api/v1/asset-tags/{row_id}/review", json={
        "action": "modify", "payload": {"tag_name": "  紧张感  "}})
    assert resp.status_code == 200
    assert resp.json()["candidate_name"] == "紧张感"
    with client.app.state.session_factory() as s:
        assert s.get(PreAssetTag, row_id).candidate_name == "紧张感"


# ---------- 批量审核 ----------


def test_batch_review_per_item_independent(client):
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
        statuses = {r.id: r.review_status for r in s.query(PreAssetTag).all()}
        assert statuses[ids[0]] == "approved"
        assert statuses[ids[2]] == "approved"
        assert statuses[ids[1]] == "pending"


def test_batch_review_partial_conflict(client):
    ids = _seed_rows(client, count=2)
    first = client.post(f"/api/v1/asset-tags/{ids[0]}/review",
                        json={"action": "approve"})
    assert first.status_code == 200
    resp = client.post("/api/v1/asset-tags/batch-review", json={
        "ids": [ids[0], ids[1]], "action": "approve"})
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert results[0]["ok"] is False
    assert results[0]["detail"] == "该标签已被处理"
    assert results[1] == {"id": ids[1], "ok": True, "detail": None}


def test_batch_review_invalid_action_rejects_whole_request(client):
    ids = _seed_rows(client, count=2)
    resp = client.post("/api/v1/asset-tags/batch-review", json={
        "ids": ids, "action": "alien"})
    assert resp.status_code == 422
    with client.app.state.session_factory() as s:
        assert all(r.review_status == "pending"
                   for r in s.query(PreAssetTag).all())


def test_batch_review_empty_ids_422(client):
    resp = client.post("/api/v1/asset-tags/batch-review",
                       json={"ids": [], "action": "approve"})
    assert resp.status_code == 422


# ---------- 认证门槛 ----------


def test_review_requires_login(client):
    with TestClient(client.app) as fresh:
        assert fresh.post("/api/v1/asset-tags/1/review",
                          json={"action": "approve"}).status_code == 401
        assert fresh.post("/api/v1/asset-tags/batch-review",
                          json={"ids": [1], "action": "approve"}).status_code == 401


# ---------- 审核列表与筛选 ----------


def _seed_list_scene(client) -> dict[str, int]:
    """两资产、两库内标签、closed/open 两任务、五行错开置信度与状态。

    - r1：新词"反转"，closed，conf 0.3，pending，带真实 evidence；
    - r2：库内 tag_action（动作/val_fixed），closed，conf 0.7，rel 0.9，pending；
    - r3：库内 tag_epic（史诗/val_generalized），open，conf 0.5，approved；
    - r4：新词"群像"，open，conf 0.95，rejected；
    - r5：新词"老友重逢"，open，conf 0.6，pending。
    """
    factory = client.app.state.session_factory
    with factory() as s:
        s.add(MediaAsset(asset_id="a1", asset_type="movie", title="悬疑片A"))
        s.add(MediaAsset(asset_id="a2", asset_type="series", title="剧集B"))
        s.add(TagGenerationTask(task_id="gen_closed", asset_id="a1",
                                mode="closed", status="succeeded",
                                created_at=_NOW))
        s.add(TagGenerationTask(task_id="gen_open", asset_id="a1",
                                mode="open", status="succeeded",
                                created_at=_NOW))
        s.flush()

        def _row(asset_id, **kw) -> int:
            base = dict(asset_id=asset_id, target_type="content",
                        target_id=asset_id, created_at=_NOW, updated_at=_NOW,
                        review_status="pending", evidence_json="[]")
            base.update(kw)
            row = PreAssetTag(**base)
            s.add(row)
            s.flush()
            return row.id

        ids = {
            "r1": _row("a1", candidate_name="反转", origin="closed",
                       candidate_dir_id="dir_scene",
                       confidence=0.3, relevance_score=None,
                       evidence_json='[{"scene_id": "s1", "start_ms": 0, '
                                     '"end_ms": 100, "description": "开场反转"}]',
                       task_id="gen_closed"),
            "r2": _row("a1", tag_id="tag_action", origin="closed",
                       relevance_score=0.9, confidence=0.7,
                       task_id="gen_closed"),
            "r3": _row("a2", tag_id="tag_epic", origin="open", confidence=0.5,
                       relevance_score=0.8, review_status="approved",
                       task_id=None),
            "r4": _row("a2", candidate_name="群像", origin="open",
                       candidate_dir_id="dir_char",
                       confidence=0.95, relevance_score=None,
                       review_status="rejected", task_id=None),
            "r5": _row("a1", candidate_name="老友重逢", origin="open",
                       candidate_dir_id="dir_theme",
                       confidence=0.6, relevance_score=None,
                       task_id="gen_open"),
        }
        s.commit()
    return ids


def test_review_items_default_ordering_and_row_shape(client):
    ids = _seed_list_scene(client)
    resp = client.get("/api/v1/review/items")
    assert resp.status_code == 200
    body = resp.json()
    assert body["page"] == 1 and body["total"] == 5
    assert [it["id"] for it in body["items"]] == [
        ids["r1"], ids["r3"], ids["r5"], ids["r2"], ids["r4"]]

    by_id = {it["id"]: it for it in body["items"]}
    r1 = by_id[ids["r1"]]
    assert set(r1.keys()) == REVIEW_ITEM_KEYS
    assert r1["tag_name"] == "反转" and r1["tag_type"] is None  # 库外新词无类型
    assert r1["dir_id"] == "dir_scene" and r1["dir_name"] == "微观语义标签"
    assert r1["dir_path"] == "微观语义标签"
    assert r1["origin"] == "closed" and r1["mode"] == "closed"
    assert r1["asset_id"] == "a1" and r1["asset_title"] == "悬疑片A"
    assert r1["confidence"] == 0.3 and r1["relevance"] is None
    assert r1["review_status"] == "pending" and r1["task_id"] == "gen_closed"
    assert r1["target_type"] == "content"
    assert r1["evidence"] == [{"scene_id": "s1", "start_ms": 0, "end_ms": 100,
                               "description": "开场反转"}]
    r2 = by_id[ids["r2"]]
    assert r2["tag_name"] == "动作" and r2["tag_type"] == "val_fixed"
    assert r2["dir_id"] == "dir_genre" and r2["dir_name"] == "主类型"
    assert r2["dir_path"] == "内容特征标签/主类型"
    assert r2["relevance"] == 0.9 and r2["reviewer_id"] is None
    r3 = by_id[ids["r3"]]
    assert r3["tag_name"] == "史诗" and r3["tag_type"] == "val_generalized"
    assert r3["origin"] == "open" and r3["review_status"] == "approved"
    r4 = by_id[ids["r4"]]
    assert r4["tag_type"] is None and r4["review_status"] == "rejected"
    assert by_id[ids["r5"]]["mode"] == "open"


def test_review_items_filter_status(client):
    ids = _seed_list_scene(client)
    resp = client.get("/api/v1/review/items", params={"status": "approved"})
    assert resp.json()["total"] == 1
    assert [it["id"] for it in resp.json()["items"]] == [ids["r3"]]

    resp = client.get("/api/v1/review/items", params={"status": "pending"})
    assert resp.json()["total"] == 3
    assert client.get("/api/v1/review/items",
                      params={"status": "nonexistent"}).json()["total"] == 0


def test_review_items_filter_confidence_bounds(client):
    ids = _seed_list_scene(client)
    resp = client.get("/api/v1/review/items", params={
        "confidence_min": 0.5, "confidence_max": 0.7})
    assert resp.json()["total"] == 3
    assert [it["id"] for it in resp.json()["items"]] == [
        ids["r3"], ids["r5"], ids["r2"]]
    assert client.get("/api/v1/review/items",
                      params={"confidence_min": 0.8}).json()["total"] == 1
    assert client.get("/api/v1/review/items",
                      params={"confidence_max": 0.5}).json()["total"] == 2


def test_review_items_filter_tag_type(client):
    """tag_type 只认库内标签：新词行无类型，不会被任何 tag_type 命中。"""
    ids = _seed_list_scene(client)
    resp = client.get("/api/v1/review/items", params={"tag_type": "val_fixed"})
    assert resp.json()["total"] == 1
    assert resp.json()["items"][0]["id"] == ids["r2"]

    resp = client.get("/api/v1/review/items",
                      params={"tag_type": "val_generalized"})
    assert resp.json()["total"] == 1
    assert resp.json()["items"][0]["id"] == ids["r3"]


def test_review_items_filter_dir_includes_children(client):
    """目录筛选含子目录：选 dir_content 能看到 dir_genre 下的行。"""
    ids = _seed_list_scene(client)
    resp = client.get("/api/v1/review/items", params={"dir_id": "dir_content"})
    # dir_genre（r2 动作、r3 史诗）+ dir_theme（r5 新词）
    assert resp.json()["total"] == 3
    assert {it["id"] for it in resp.json()["items"]} == {
        ids["r2"], ids["r3"], ids["r5"]}

    resp = client.get("/api/v1/review/items", params={"dir_id": "dir_theme"})
    assert resp.json()["total"] == 1
    assert resp.json()["items"][0]["id"] == ids["r5"]


def test_review_items_filter_mode(client):
    ids = _seed_list_scene(client)
    resp = client.get("/api/v1/review/items", params={"mode": "closed"})
    assert resp.json()["total"] == 2
    assert {it["id"] for it in resp.json()["items"]} == {ids["r1"], ids["r2"]}

    resp = client.get("/api/v1/review/items", params={"mode": "open"})
    assert resp.json()["total"] == 3
    assert {it["id"] for it in resp.json()["items"]} == {
        ids["r3"], ids["r4"], ids["r5"]}


def test_review_items_filter_asset_and_origin(client):
    ids = _seed_list_scene(client)
    resp = client.get("/api/v1/review/items", params={"asset_id": "a1"})
    assert resp.json()["total"] == 3
    assert {it["id"] for it in resp.json()["items"]} == {
        ids["r1"], ids["r2"], ids["r5"]}

    resp = client.get("/api/v1/review/items", params={"origin": "closed"})
    assert resp.json()["total"] == 2

    assert client.get("/api/v1/review/items",
                      params={"origin": "open"}).json()["total"] == 3

    resp = client.get("/api/v1/review/items", params={
        "asset_id": "a1", "origin": "closed"})
    assert resp.json()["total"] == 2


def test_review_items_pagination(client):
    ids = _seed_list_scene(client)
    resp = client.get("/api/v1/review/items", params={"page": 2, "page_size": 2})
    body = resp.json()
    assert body["page"] == 2 and body["total"] == 5
    assert [it["id"] for it in body["items"]] == [ids["r5"], ids["r2"]]

    resp = client.get("/api/v1/review/items", params={"page": 3, "page_size": 2})
    assert [it["id"] for it in resp.json()["items"]] == [ids["r4"]]
    assert resp.json()["total"] == 5

    resp = client.get("/api/v1/review/items", params={"page": 4, "page_size": 2})
    assert resp.json()["items"] == [] and resp.json()["total"] == 5


def test_review_items_merged_row_uses_catalog_name(client):
    """merge 后：行变 approved + tag_id，列表回退库内标签名/类型。"""
    (row_id,) = _seed_rows(client)
    merged = client.post(f"/api/v1/asset-tags/{row_id}/review",
                         json={"action": "merge",
                               "payload": {"merge_to_tag_id": "tag_action"}})
    assert merged.status_code == 200

    resp = client.get("/api/v1/review/items", params={"status": "approved"})
    body = resp.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["id"] == row_id
    assert item["tag_name"] == "动作" and item["tag_type"] == "val_fixed"
    assert item["origin"] == "open"


def test_modify_display_name_visible_in_list_and_detail(client):
    """modify 后展示三处同步：审核列表、任务详情、资产已通过标签。"""
    factory = client.app.state.session_factory
    with factory() as s:
        s.add(MediaAsset(asset_id="a_mod", asset_type="movie", title="改名影片"))
        s.add(TagGenerationTask(task_id="gen_mod", asset_id="a_mod",
                                mode="closed", status="succeeded",
                                created_at=_NOW))
        s.flush()
        row = PreAssetTag(asset_id="a_mod", target_type="content",
                          target_id="a_mod", tag_id="tag_action",
                          origin="closed", confidence=0.8,
                          evidence_json="[]", review_status="pending",
                          task_id="gen_mod", created_at=_NOW, updated_at=_NOW)
        s.add(row)
        s.flush()
        row_id = row.id
        s.commit()

    # 改名前：列表/详情显示库内名；资产标签视图只收 approved 行 → 空
    items = client.get("/api/v1/review/items").json()["items"]
    assert items[0]["tag_name"] == "动作" and items[0]["tag_type"] == "val_fixed"
    detail = client.get("/api/v1/generation/tasks/gen_mod").json()
    assert detail["results"][0]["tag_name"] == "动作"
    assert client.get("/api/v1/assets/a_mod/tags").json() == []

    modified = client.post(f"/api/v1/asset-tags/{row_id}/review", json={
        "action": "modify", "payload": {"tag_name": "动作改"}})
    assert modified.status_code == 200
    assert modified.json()["tag_name"] == "动作改"

    # 改名后：三处均显示修改后的名字（库内标签仍是"动作"）
    items = client.get("/api/v1/review/items").json()["items"]
    assert items[0]["tag_name"] == "动作改"
    detail = client.get("/api/v1/generation/tasks/gen_mod").json()
    assert detail["results"][0]["tag_name"] == "动作改"
    tags = client.get("/api/v1/assets/a_mod/tags").json()
    assert tags[0]["tag_name"] == "动作改"


def test_bad_evidence_degrades_to_empty_list(client):
    factory = client.app.state.session_factory
    with factory() as s:
        s.add(MediaAsset(asset_id="a_bad", asset_type="movie", title="坏证据影片"))
        s.add(TagGenerationTask(task_id="gen_bad", asset_id="a_bad",
                                mode="closed", status="succeeded",
                                created_at=_NOW))
        s.flush()
        s.add(PreAssetTag(
            asset_id="a_bad", target_type="content", target_id="a_bad",
            candidate_name="坏证据行", origin="closed", confidence=0.4,
            evidence_json='[{"scene_id": "s1"}]',  # 缺 start_ms/end_ms/description
            review_status="pending", task_id="gen_bad",
            created_at=_NOW, updated_at=_NOW))
        s.commit()

    listing = client.get("/api/v1/review/items").json()
    assert listing["total"] == 1
    assert listing["items"][0]["evidence"] == []

    detail = client.get("/api/v1/generation/tasks/gen_bad")
    assert detail.status_code == 200
    assert detail.json()["results"][0]["evidence"] == []


def test_review_filters_endpoint(client):
    _seed_list_scene(client)
    resp = client.get("/api/v1/review/filters")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"tag_types", "statuses", "modes", "dirs"}
    assert body["tag_types"] == ["val_fixed", "val_generalized"]
    assert body["statuses"] == ["pending", "approved", "rejected"]
    assert body["modes"] == ["closed", "open"]
    dirs = {d["dir_id"]: d for d in body["dirs"]}
    assert dirs["dir_theme"]["applicable_scope"] == "content"
    assert dirs["dir_theme"]["dir_type"] == "dir_dynamic"
    assert dirs["dir_scene"]["applicable_scope"] == "scene"
    assert dirs["dir_char"]["applicable_scope"] == "character"
    assert dirs["dir_genre"]["path"] == "内容特征标签/主类型"


def test_review_filters_library_types(client):
    """筛选下拉来自 JSON 标签库，与 DB 无关（空 DB 也有值）。"""
    resp = client.get("/api/v1/review/filters")
    body = resp.json()
    assert body["tag_types"] == ["val_fixed", "val_generalized"]
    assert body["statuses"] == ["pending", "approved", "rejected"]


def test_review_list_and_filters_require_login(client):
    with TestClient(client.app) as fresh:
        assert fresh.get("/api/v1/review/items").status_code == 401
        assert fresh.get("/api/v1/review/filters").status_code == 401


def test_review_items_hides_system_auto_rows(client):
    """库内命中（同名/判重）是后端自动处理，不进审核中心列表。

    这两类行仍留在 pre_asset_tag 里供 `GET /assets/{id}/tags` 展示"已通过标签"，
    但审核列表按 reviewer_id='system' 排除——它们没有可审的东西（审核表也没有
    标签属性字段，类型无从显示）。
    """
    _seed_list_scene(client)
    factory = client.app.state.session_factory
    with factory() as s:
        s.add(PreAssetTag(
            asset_id="a1", target_type="content", target_id="a1",
            tag_id="tag_revenge", candidate_name=None, candidate_dir_id=None,
            candidate_alias="报仇", origin="open",
            relevance_score=0.9, confidence=0.99, evidence_json="[]",
            review_status="approved", reviewer_id="system",
            review_comment="系统自动并入库内标签「复仇」，原名「报仇」记为别名候选",
            created_at=_NOW, updated_at=_NOW))
        s.commit()

    body = client.get("/api/v1/review/items?status=approved").json()
    assert [it["reviewer_id"] for it in body["items"]] == [None]
    assert body["total"] == 1
    # 全量列表同样不含系统行（5 行种子 + 1 行系统行 → 仍是 5）
    assert client.get("/api/v1/review/items?page_size=50").json()["total"] == 5
