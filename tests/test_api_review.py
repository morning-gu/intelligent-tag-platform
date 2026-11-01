"""审核操作 API 测试（Task 4）与审核列表/筛选 API 测试（Task 5）。

Task 4 覆盖契约（错误映射 + 批量逐条独立）：
- POST /api/v1/asset-tags/{id}/review：approve 200 回行摘要（与
  AssetTagOut 同形）、重复审核 409（Review Focus 1 并发保护，detail
  "该标签已被处理"）、未知行 404、非法 action 422、modify 空值 422、
  modify 字符串字段空串/纯空白/超长 422（Review Focus 5）+ strip 落库、
  merge 缺/坏 merge_to 422、modify 成功回 pending、merge/modify 落库撞
  部分唯一索引 409（终审 Important 2，原裸 500）；
- POST /api/v1/asset-tags/batch-review：逐条独立不中断（Review Focus
  4）——混合未知 id / 部分冲突 / 撞唯一索引均回 200 且 per-item ok=false
  携带 detail，action 级 422 整请求拒绝且零副作用（DB 仍全 pending）、
  空 ids 422；
- reviewer 取会话用户名（client fixture 已 admin 登录 → reviewer_id
  "admin"）；未登录 401（create_app protected_deps 统一挂载验证）。

Task 5 覆盖契约（审核中心页面数据源）：
- GET /api/v1/review/items：七筛选参数逐个 + 组合、默认 confidence 升序
  （低分先审）、分页与 total、行形状（asset_title/tag_name origin 感知 +
  merged 回退目录名/mode 经 generation_task 关联/tag_type origin 感知）、
  evidence shape 非法降级空列表（不再 500，任务详情路径同步收紧）；
- GET /api/v1/review/filters：tag_types 库内去重 + statuses/modes 固定枚举；
- 新端点未登录 401。

播种模式（计划指定）：参照 tests/test_results.py 的父行播种——经
client.app.state.session_factory 直接向 client fixture 的临时文件库补
MediaAsset/TagCatalog FK 父行与 AssetTag pending 行（审核语义测试无需
经生成 API 造数，零 MockLLM 采样依赖）。
"""

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from tagplatform.db.models import (
    AssetTag,
    GenerationTask,
    MediaAsset,
    ReviewRecord,
    TagCatalog,
)

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


# ---------- 落库撞部分唯一索引（终审 Important 2：原裸 500） ----------


def test_review_merge_onto_mounted_tag_409(client):
    """merge 撞 ux_asset_tag_catalog：同资产已有该目录标签挂载 → 409
    detail "该资产已挂此标签"，行状态不变、该行审核记录零写入。"""
    ids = _seed_rows(client, count=2)
    # 第一条 merge 到 tag_1 成功——(a1, tag_1) 目录挂载位被占
    first = client.post(f"/api/v1/asset-tags/{ids[0]}/review",
                        json={"action": "merge",
                              "payload": {"merge_to_tag_id": "tag_1"}})
    assert first.status_code == 200
    # 第二条 merge 到同一目录标签 → commit 撞索引
    second = client.post(f"/api/v1/asset-tags/{ids[1]}/review",
                         json={"action": "merge",
                               "payload": {"merge_to_tag_id": "tag_1"}})
    assert second.status_code == 409
    assert second.json()["detail"] == "该资产已挂此标签"
    with client.app.state.session_factory() as s:
        row = s.get(AssetTag, ids[1])
        assert row.review_status == "pending" and row.tag_id is None
        assert row.candidate_name == "候选1"
        assert s.query(ReviewRecord).filter_by(result_id=ids[1]).count() == 0


def test_batch_review_index_conflict_per_item(client):
    """批量含撞索引条目：整请求 200，先 merge 的条目占住 (asset, tag) 唯一位
    成功，后撞的条目 ok=false detail "该资产已挂此标签"（per-item 独立
    不中断）。"""
    ids = _seed_rows(client, count=2)
    resp = client.post("/api/v1/asset-tags/batch-review", json={
        "ids": ids, "action": "merge",
        "payload": {"merge_to_tag_id": "tag_1"}})
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert results[0] == {"id": ids[0], "ok": True, "detail": None}
    assert results[1]["ok"] is False
    assert results[1]["detail"] == "该资产已挂此标签"
    with client.app.state.session_factory() as s:
        statuses = {r.id: r.review_status for r in s.query(AssetTag).all()}
        assert statuses[ids[0]] == "merged"
        assert statuses[ids[1]] == "pending"  # 冲突条目已回滚，行未动
        assert s.query(ReviewRecord).count() == 1  # 仅成功条目写记录


# ---------- modify 字符串字段校验（Review Focus 5，终审 Important 3） ----------


def test_review_modify_string_field_validation_422(client):
    """modify 字符串字段空串/纯空白/超长 → 422，零副作用（终审复现：
    原 200 产出空名行/无上限行）。"""
    (row_id,) = _seed_rows(client)
    cases = [
        {"tag_name": ""},             # 空串
        {"candidate_name": "   "},    # 纯空白
        {"tag_name": "长" * 201},     # 超长（上限 200）
    ]
    for payload in cases:
        resp = client.post(f"/api/v1/asset-tags/{row_id}/review",
                           json={"action": "modify", "payload": payload})
        assert resp.status_code == 422, f"{payload} 应 422"
    with client.app.state.session_factory() as s:
        row = s.get(AssetTag, row_id)
        assert row.review_status == "pending"
        assert row.candidate_name == "候选0"
        assert s.query(ReviewRecord).count() == 0


def test_review_modify_strips_value(client):
    """modify 字符串字段 strip 后落库：" 紧张感 " → "紧张感"（纯空白名
    由空值校验拦截，不会再产出）。"""
    (row_id,) = _seed_rows(client)
    resp = client.post(f"/api/v1/asset-tags/{row_id}/review", json={
        "action": "modify", "payload": {"tag_name": "  紧张感  "}})
    assert resp.status_code == 200
    assert resp.json()["candidate_name"] == "紧张感"
    with client.app.state.session_factory() as s:
        assert s.get(AssetTag, row_id).candidate_name == "紧张感"


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


# ---------- 审核列表与筛选（Task 5） ----------

# 审核列表行键集契约（计划：id/asset_id/asset_title/tag_name/origin/mode/
# tag_type/confidence/relevance/review_status/evidence/task_id）
REVIEW_ITEM_KEYS = {"id", "asset_id", "asset_title", "tag_name", "origin",
                    "mode", "tag_type", "confidence", "relevance",
                    "review_status", "evidence", "task_id"}


def _seed_list_scene(client) -> dict[str, int]:
    """播种审核列表场景：两资产、两类型目录标签、closed/open 两任务、
    五行错开置信度与状态（0.3/0.5/0.6/0.7/0.95），覆盖四种行形态——
    generated（带/不带任务）、catalog、merged（generated 行并入目录，
    candidate_* 清空）。返回 键 → 行 id 映射。

    行清单（键 → 语义）：
    - r1：generated 候选"反转"（narrative），conf 0.3，pending，closed 任务，
      带真实 evidence（排序与形状断言用）；
    - r2：catalog 挂 tag_1（悬疑/genre），conf 0.7，rel 0.9，pending，closed 任务；
    - r3：merged 行（origin=generated + tag_id=tag_2 紧张/mood，candidate
      已清空），conf 0.5，approved，无任务——tag_name 回退断言用；
    - r4：generated 候选"群像"（candidate_type=genre），conf 0.95，rejected，
      无任务——generated 行按候选类型命中 tag_type 筛选用；
    - r5：generated 候选"老友重逢"（candidate_type=mood），conf 0.6，pending，
      open 任务。
    """
    factory = client.app.state.session_factory
    with factory() as s:
        s.add(MediaAsset(asset_id="a1", asset_type="movie", title="悬疑片A",
                         created_at=_NOW))
        s.add(MediaAsset(asset_id="a2", asset_type="series", title="剧集B",
                         created_at=_NOW))
        s.add(TagCatalog(tag_id="tag_1", tag_name="悬疑", tag_type="genre",
                         description="以悬念和谜题为核心"))
        s.add(TagCatalog(tag_id="tag_2", tag_name="紧张", tag_type="mood",
                         description="以紧张氛围为核心"))
        s.add(GenerationTask(task_id="gen_closed", mode="closed",
                             status="succeeded", created_at=_NOW))
        s.add(GenerationTask(task_id="gen_open", mode="open",
                             status="succeeded", created_at=_NOW))
        s.flush()

        def _row(**kw) -> int:
            base = dict(created_at=_NOW, updated_at=_NOW,
                        review_status="pending", evidence_json="[]")
            base.update(kw)
            row = AssetTag(**base)
            s.add(row)
            s.flush()
            return row.id

        ids = {
            "r1": _row(asset_id="a1", tag_id=None, candidate_name="反转",
                       candidate_type="narrative", origin="generated",
                       confidence=0.3, relevance_score=None,
                       evidence_json='[{"scene_id": "s1", "start_ms": 0, '
                                     '"end_ms": 100, "description": "开场反转"}]',
                       task_id="gen_closed"),
            "r2": _row(asset_id="a1", tag_id="tag_1", origin="catalog",
                       relevance_score=0.9, confidence=0.7,
                       task_id="gen_closed"),
            "r3": _row(asset_id="a2", tag_id="tag_2", origin="generated",
                       candidate_name=None, candidate_type=None,
                       candidate_description=None, confidence=0.5,
                       relevance_score=None, review_status="approved",
                       task_id=None),
            "r4": _row(asset_id="a2", tag_id=None, candidate_name="群像",
                       candidate_type="genre", origin="generated",
                       confidence=0.95, relevance_score=None,
                       review_status="rejected", task_id=None),
            "r5": _row(asset_id="a1", tag_id=None, candidate_name="老友重逢",
                       candidate_type="mood", origin="generated",
                       confidence=0.6, relevance_score=None,
                       task_id="gen_open"),
        }
        s.commit()
    return ids


def test_review_items_default_ordering_and_row_shape(client):
    """默认列表：confidence 升序（低分先审，0.3→0.95）；行键集与 origin
    感知取值齐全——tag_name/tag_type 按行形态取（catalog 取目录、generated
    取候选、merged 回退目录名）、mode 经 generation_task 关联（无任务 None）、
    asset_title 取 MediaAsset.title、evidence 解析 evidence_json。"""
    ids = _seed_list_scene(client)
    resp = client.get("/api/v1/review/items")
    assert resp.status_code == 200
    body = resp.json()
    assert body["page"] == 1 and body["total"] == 5
    assert [it["id"] for it in body["items"]] == [
        ids["r1"], ids["r3"], ids["r5"], ids["r2"], ids["r4"]]

    by_id = {it["id"]: it for it in body["items"]}
    # r1：generated + closed 任务 + 真实 evidence
    r1 = by_id[ids["r1"]]
    assert set(r1.keys()) == REVIEW_ITEM_KEYS
    assert r1["tag_name"] == "反转" and r1["tag_type"] == "narrative"
    assert r1["origin"] == "generated" and r1["mode"] == "closed"
    assert r1["asset_id"] == "a1" and r1["asset_title"] == "悬疑片A"
    assert r1["confidence"] == 0.3 and r1["relevance"] is None
    assert r1["review_status"] == "pending" and r1["task_id"] == "gen_closed"
    assert r1["evidence"] == [{"scene_id": "s1", "start_ms": 0, "end_ms": 100,
                               "description": "开场反转"}]
    # r2：catalog 行取目录名/类型 + 关联度
    r2 = by_id[ids["r2"]]
    assert r2["tag_name"] == "悬疑" and r2["tag_type"] == "genre"
    assert r2["relevance"] == 0.9
    # r3：merged 行（origin=generated）tag_name 回退目录名——candidate 已清空
    r3 = by_id[ids["r3"]]
    assert r3["tag_name"] == "紧张" and r3["tag_type"] == "mood"
    assert r3["origin"] == "generated" and r3["review_status"] == "approved"
    # r4：无任务 → mode/task_id 为 None
    r4 = by_id[ids["r4"]]
    assert r4["mode"] is None and r4["task_id"] is None
    assert r4["tag_name"] == "群像" and r4["review_status"] == "rejected"
    # r5：open 任务
    assert by_id[ids["r5"]]["mode"] == "open"


def test_review_items_filter_status(client):
    """status 筛选：review_status 精确匹配；未知状态 → 空列表不报错。"""
    ids = _seed_list_scene(client)
    resp = client.get("/api/v1/review/items", params={"status": "approved"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1 and [it["id"] for it in body["items"]] == [ids["r3"]]

    resp = client.get("/api/v1/review/items", params={"status": "pending"})
    assert resp.json()["total"] == 3
    assert client.get("/api/v1/review/items",
                      params={"status": "nonexistent"}).json()["total"] == 0


def test_review_items_filter_confidence_bounds(client):
    """confidence_min/max 闭区间筛选：上下界同时给与单独给均可。"""
    ids = _seed_list_scene(client)
    resp = client.get("/api/v1/review/items", params={
        "confidence_min": 0.5, "confidence_max": 0.7})
    body = resp.json()
    assert body["total"] == 3
    assert [it["id"] for it in body["items"]] == [
        ids["r3"], ids["r5"], ids["r2"]]  # 区间内仍按升序
    assert client.get("/api/v1/review/items",
                      params={"confidence_min": 0.8}).json()["total"] == 1
    assert client.get("/api/v1/review/items",
                      params={"confidence_max": 0.5}).json()["total"] == 2


def test_review_items_filter_tag_type(client):
    """tag_type 筛选 origin 感知：catalog/merged 行按目录类型命中，
    generated 行按候选类型命中（merged 行 candidate 已清空不误配）。"""
    ids = _seed_list_scene(client)
    resp = client.get("/api/v1/review/items", params={"tag_type": "genre"})
    assert resp.json()["total"] == 2
    assert {it["id"] for it in resp.json()["items"]} == {ids["r2"], ids["r4"]}

    resp = client.get("/api/v1/review/items", params={"tag_type": "mood"})
    assert resp.json()["total"] == 2
    assert {it["id"] for it in resp.json()["items"]} == {ids["r3"], ids["r5"]}

    resp = client.get("/api/v1/review/items", params={"tag_type": "narrative"})
    assert resp.json()["total"] == 1
    assert resp.json()["items"][0]["id"] == ids["r1"]


def test_review_items_filter_mode(client):
    """mode 筛选：行 task_id → generation_task.mode 关联过滤；无任务行
    （mode 为 None）不被任何 mode 值命中。"""
    ids = _seed_list_scene(client)
    resp = client.get("/api/v1/review/items", params={"mode": "closed"})
    assert resp.json()["total"] == 2
    assert {it["id"] for it in resp.json()["items"]} == {ids["r1"], ids["r2"]}

    resp = client.get("/api/v1/review/items", params={"mode": "open"})
    assert resp.json()["total"] == 1
    assert resp.json()["items"][0]["id"] == ids["r5"]


def test_review_items_filter_asset_and_origin(client):
    """asset_id 与 origin 筛选各自生效且可组合。"""
    ids = _seed_list_scene(client)
    resp = client.get("/api/v1/review/items", params={"asset_id": "a1"})
    assert resp.json()["total"] == 3
    assert {it["id"] for it in resp.json()["items"]} == {
        ids["r1"], ids["r2"], ids["r5"]}

    resp = client.get("/api/v1/review/items", params={"origin": "catalog"})
    assert resp.json()["total"] == 1
    assert resp.json()["items"][0]["id"] == ids["r2"]

    assert client.get("/api/v1/review/items",
                      params={"origin": "generated"}).json()["total"] == 4

    # 组合：a1 上的 catalog 行只剩 r2
    resp = client.get("/api/v1/review/items", params={
        "asset_id": "a1", "origin": "catalog"})
    assert resp.json()["total"] == 1
    assert resp.json()["items"][0]["id"] == ids["r2"]


def test_review_items_pagination(client):
    """分页：page/page_size 截断且 total 恒为过滤后总数、page 回显请求页；
    末页之后为空列表；默认 page_size=50 一页全收。"""
    ids = _seed_list_scene(client)
    resp = client.get("/api/v1/review/items", params={"page": 2, "page_size": 2})
    body = resp.json()
    assert body["page"] == 2 and body["total"] == 5
    assert [it["id"] for it in body["items"]] == [ids["r5"], ids["r2"]]

    resp = client.get("/api/v1/review/items", params={"page": 3, "page_size": 2})
    body = resp.json()
    assert [it["id"] for it in body["items"]] == [ids["r4"]]  # 末页剩余 1 行
    assert body["total"] == 5

    resp = client.get("/api/v1/review/items", params={"page": 4, "page_size": 2})
    assert resp.json()["items"] == [] and resp.json()["total"] == 5

    assert len(client.get("/api/v1/review/items").json()["items"]) == 5


def test_review_items_merged_row_uses_catalog_name(client):
    """Task 4 遗留修复验证：generated 行经 merge 动作并入目录后，列表
    tag_name/tag_type 按目录回退（candidate_* 已清空，不再显示空候选名）。"""
    (row_id,) = _seed_rows(client)  # generated 候选"候选0" + 目录 tag_1（悬疑/genre）
    merged = client.post(f"/api/v1/asset-tags/{row_id}/review",
                         json={"action": "merge",
                               "payload": {"merge_to_tag_id": "tag_1"}})
    assert merged.status_code == 200

    resp = client.get("/api/v1/review/items", params={"status": "merged"})
    body = resp.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["id"] == row_id
    assert item["tag_name"] == "悬疑" and item["tag_type"] == "genre"
    assert item["origin"] == "generated"


def test_modify_display_name_visible_in_list_and_detail(client):
    """终审 Important 1：modify 只写 candidate_* 而列表/任务详情/资产标签
    对带 tag_id 行一律显示目录名——审核员改 catalog 行 tag_name 后表格
    不变。修复后展示解析 candidate_* 优先：modify 前三处显示目录名
    "悬疑"，modify 后三处均显示修改后的名字。"""
    factory = client.app.state.session_factory
    with factory() as s:
        s.add(MediaAsset(asset_id="a_mod", asset_type="movie", title="改名影片",
                         created_at=_NOW))
        s.add(TagCatalog(tag_id="tag_mod", tag_name="悬疑", tag_type="genre",
                         description="以悬念和谜题为核心"))
        s.add(GenerationTask(task_id="gen_mod", mode="closed",
                             status="succeeded", created_at=_NOW))
        s.flush()
        row = AssetTag(asset_id="a_mod", tag_id="tag_mod", origin="catalog",
                       confidence=0.8, evidence_json="[]",
                       review_status="pending", task_id="gen_mod",
                       created_at=_NOW, updated_at=_NOW)
        s.add(row)
        s.flush()
        row_id = row.id
        s.commit()

    # 改名前：审核列表 / 任务详情 / 资产标签三处均显示目录名"悬疑"
    items = client.get("/api/v1/review/items").json()["items"]
    assert items[0]["tag_name"] == "悬疑" and items[0]["tag_type"] == "genre"
    detail = client.get("/api/v1/generation/tasks/gen_mod").json()
    assert detail["results"][0]["tag_name"] == "悬疑"
    tags = client.get("/api/v1/assets/a_mod/tags").json()
    assert tags[0]["tag_name"] == "悬疑"

    # modify（catalog 行按别名落 candidate_*，共享目录字典不动）
    modified = client.post(f"/api/v1/asset-tags/{row_id}/review", json={
        "action": "modify",
        "payload": {"tag_name": "悬疑改", "tag_type": "thriller"}})
    assert modified.status_code == 200
    assert modified.json()["tag_name"] == "悬疑改"

    # 改名后：三处均显示修改后的名字/类型（目录字典仍是"悬疑"）
    items = client.get("/api/v1/review/items").json()["items"]
    assert items[0]["tag_name"] == "悬疑改" and items[0]["tag_type"] == "thriller"
    detail = client.get("/api/v1/generation/tasks/gen_mod").json()
    assert detail["results"][0]["tag_name"] == "悬疑改"
    tags = client.get("/api/v1/assets/a_mod/tags").json()
    assert tags[0]["tag_name"] == "悬疑改"
    with factory() as s:
        assert s.get(TagCatalog, "tag_mod").tag_name == "悬疑"  # 目录未被触碰


def test_bad_evidence_degrades_to_empty_list(client):
    """evidence 条目 shape 非法（JSON 合法但缺必填字段 → pydantic
    ValidationError）：审核列表与生成任务详情两条消费路径均 200 且该行
    evidence 降级空列表（终审 deferred 落地——不再 500）。"""
    factory = client.app.state.session_factory
    with factory() as s:
        s.add(MediaAsset(asset_id="a_bad", asset_type="movie", title="坏证据影片",
                         created_at=_NOW))
        s.add(GenerationTask(task_id="gen_bad", mode="closed",
                             status="succeeded", created_at=_NOW))
        s.flush()
        s.add(AssetTag(
            asset_id="a_bad", tag_id=None, candidate_name="坏证据行",
            candidate_type="mood", origin="generated", confidence=0.4,
            evidence_json='[{"scene_id": "s1"}]',  # 缺 start_ms/end_ms/description
            review_status="pending", task_id="gen_bad",
            created_at=_NOW, updated_at=_NOW))
        s.commit()

    listing = client.get("/api/v1/review/items").json()
    assert listing["total"] == 1
    assert listing["items"][0]["evidence"] == []

    detail = client.get("/api/v1/generation/tasks/gen_bad")
    assert detail.status_code == 200  # 收紧前此处 500
    assert detail.json()["results"][0]["evidence"] == []


def test_review_filters_endpoint(client):
    """filters 端点：tag_types 为 tag_catalog 库内去重（确定性排序），
    statuses/modes 为契约固定枚举。"""
    _seed_list_scene(client)
    resp = client.get("/api/v1/review/filters")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"tag_types", "statuses", "modes"}
    assert body["tag_types"] == ["genre", "mood"]
    assert body["statuses"] == ["pending", "approved", "rejected", "merged"]
    assert body["modes"] == ["closed", "open"]


def test_review_filters_empty_catalog(client):
    """空库：tag_types 为空列表（statuses/modes 仍回固定枚举）。"""
    resp = client.get("/api/v1/review/filters")
    assert resp.status_code == 200
    body = resp.json()
    assert body["tag_types"] == []
    assert body["statuses"] == ["pending", "approved", "rejected", "merged"]


def test_review_list_and_filters_require_login(client):
    """新端点同受全 API 保护：未登录 401（protected_deps 挂载覆盖）。"""
    with TestClient(client.app) as fresh:
        assert fresh.get("/api/v1/review/items").status_code == 401
        assert fresh.get("/api/v1/review/filters").status_code == 401
