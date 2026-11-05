"""审核操作 API 测试与审核列表/筛选 API 测试（v2 重写，Task 8）。

覆盖契约（brief + 契约速查 + doc §6.2）：
- POST /api/v1/asset-tags/{id}/review（URL 不动，操作 pre_asset_tag 行）：
  approve 200 回 pre 行摘要（PreTagOut 同形）+ 复制挂载落库、重复审核 409
  （detail "该标签已被处理"）、未知行 404、非法 action 422、modify 一步
  到位（approved + 挂载）、modify 自由候选 422、modify 字段空/超长 422
  零副作用、merge 两支（并入既有/合入新建）、merge 未知目标 422、合入
  新建同名 422（提示并入既有）、重复并入撞唯一索引 409（"该资产已挂此
  标签"）；
- POST /api/v1/asset-tags/batch-review：仅 approve/reject（modify/merge
  载荷逐行各异 → action 级 422 整请求拒绝零副作用）、逐条独立（Review
  Focus 4：混合未知 id/部分冲突回 200 且 per-item ok=false）、空 ids 422；
- GET /api/v1/review/items：过滤参数 status/confidence_min/confidence_max/
  dir/origin/asset_id（dir=目录名精确匹配；v1 mode/tag_type 过滤随词表
  退役）+ 组合、默认 confidence 升序 nulls-last（低分先审）、分页与
  total、行形状（ReviewItemOut：tag_name = candidate 或 define 名、
  dir_name = define 目录 or candidate 目录 or None、evidence 解析）；
- GET /api/v1/review/filters：{statuses, origins, dirs}——dirs 为
  tag_catalog 库内目录名去重升序（空库空列表）；
- reviewer 取会话用户名（client fixture 已 admin 登录）；四端点未登录 401。

播种模式：经 client.app.state.session_factory 直接向临时文件库补 v2 父行
（media_asset/tag_catalog/tag_define/tag_generation_task）与 pre 行（审核
语义测试无需经生成 API 造数，零 MockLLM 采样依赖）。
"""

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from tagplatform.db.models import (
    AssetTag,
    MediaAsset,
    PreAssetTag,
    TagCatalog,
    TagChangeLog,
    TagDefine,
    TagGenerationTask,
)

_NOW = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)

# 单条审核响应键集契约（与 generation.py 的 PreTagOut 同形——pre 行摘要）
RESULT_KEYS = {"id", "tag_name", "tag_id", "candidate_name", "origin",
               "relevance_score", "confidence", "review_status", "evidence"}
# 审核列表行键集契约（ReviewItemOut：dir_name 新增，mode/tag_type 退役）
REVIEW_ITEM_KEYS = {"id", "asset_id", "asset_title", "tag_name", "tag_id",
                    "candidate_name", "origin", "dir_name", "confidence",
                    "relevance", "review_status", "evidence", "task_id"}


def _seed_parents(s) -> None:
    """播种 v2 外键父行：两资产由调用方补、两目录、三定义、两任务（flush
    分步——目录行先落库再插定义，任务行先落库再插带 task_id 的 pre 行）。"""
    s.add(TagCatalog(dir_id="dir_1", dir_name="剧情", dir_type="dir_static",
                     description="剧情类目录", created_at=_NOW, updated_at=_NOW))
    s.add(TagCatalog(dir_id="dir_2", dir_name="情绪", dir_type="dir_static",
                     description="情绪类目录", created_at=_NOW, updated_at=_NOW))
    s.flush()
    s.add_all([
        TagDefine(tag_id="tag_1", tag_name="悬疑", tag_type="val_fixed",
                  description="以悬念和谜题为核心", dir_id="dir_1",
                  created_at=_NOW, updated_at=_NOW),
        TagDefine(tag_id="tag_2", tag_name="紧张", tag_type="val_fixed",
                  description="以紧张氛围为核心", dir_id="dir_2",
                  created_at=_NOW, updated_at=_NOW),
        TagDefine(tag_id="tag_3", tag_name="喜剧", tag_type="val_fixed",
                  description="以笑点为核心", dir_id="dir_1",
                  created_at=_NOW, updated_at=_NOW),
    ])
    s.add_all([
        TagGenerationTask(task_id="gen_closed", asset_id="a1", mode="closed",
                          status="succeeded", created_at=_NOW),
        TagGenerationTask(task_id="gen_open", asset_id="a1", mode="open",
                          status="succeeded", created_at=_NOW),
    ])
    s.flush()


def _seed(client, rows: list[dict]) -> list[int]:
    """播种 pre 行（父行齐备），返回行 id 列表（按序）。

    行默认值：a1 content 粒度自由候选（open/pending），spec 按列覆盖。"""
    factory = client.app.state.session_factory
    with factory() as s:
        s.add(MediaAsset(asset_id="a1", asset_type="movie", title="悬疑片A",
                         created_at=_NOW))
        s.flush()
        _seed_parents(s)
        ids = []
        base = dict(asset_id="a1", target_type="content", target_id="a1",
                    origin="open", review_status="pending",
                    evidence_json="[]", created_at=_NOW, updated_at=_NOW)
        for spec in rows:
            row = PreAssetTag(**{**base, **spec})
            s.add(row)
            s.flush()
            ids.append(row.id)
        s.commit()
    return ids


def _seed_scene(client) -> dict[str, int]:
    """播种审核列表场景：六行错开置信度（0.3/0.5/0.6/0.7/0.95/None）与
    状态、覆盖全部行形态——open 自由候选、closed 定义行、合入新建行
    （tag_id+candidate_dir_id 并存）、open 归一命中行、无任务行、无置信度
    行（排序 nulls-last 断言用）。返回键 → 行 id 映射。"""
    factory = client.app.state.session_factory
    with factory() as s:
        s.add(MediaAsset(asset_id="a1", asset_type="movie", title="悬疑片A",
                         created_at=_NOW))
        s.add(MediaAsset(asset_id="a2", asset_type="series", title="剧集B",
                         created_at=_NOW))
        s.flush()
        _seed_parents(s)
        ids = {}

        def _row(asset_id="a1", **kw) -> int:
            base = dict(asset_id=asset_id, target_type="content",
                        target_id=asset_id, origin="open",
                        review_status="pending", evidence_json="[]",
                        created_at=_NOW, updated_at=_NOW)
            base.update(kw)
            row = PreAssetTag(**base)
            s.add(row)
            s.flush()
            return row.id

        ids["r1"] = _row(  # open 自由候选 + open 任务 + 真实证据
            candidate_name="反转", origin="open", confidence=0.3,
            evidence_json='[{"scene_id": "s1", "start_ms": 0, "end_ms": 100, '
                          '"description": "开场反转"}]', task_id="gen_open")
        ids["r2"] = _row(  # closed 定义行（无候选字段）+ closed 任务
            tag_id="tag_1", origin="closed", candidate_name=None,
            candidate_description=None, relevance_score=0.9, confidence=0.7,
            task_id="gen_closed")
        ids["r3"] = _row(  # 合入新建行：tag_id + candidate_dir_id 并存、approved
            asset_id="a2", tag_id="tag_2", candidate_name="紧张感",
            candidate_dir_id="dir_2", confidence=0.5, review_status="approved")
        ids["r4"] = _row(  # closed 定义行、rejected
            asset_id="a2", tag_id="tag_3", origin="closed", candidate_name=None,
            candidate_description=None, confidence=0.95,
            review_status="rejected")
        ids["r5"] = _row(  # open 归一命中行（tag_id+candidate 并存）、无置信度
            asset_id="a2", tag_id="tag_1", candidate_name="悬疑变")
        ids["r6"] = _row(  # open 自由候选、无任务
            candidate_name="群像", confidence=0.6)
        s.commit()
    return ids


# ---------- 单条审核 ----------


def test_review_approve_returns_pre_row_summary(client):
    """approve 200：响应与 PreTagOut 同形、状态落库、reviewer 落会话用户名
    （admin）、复制挂载 asset_tag（tag_name 取 define 名）。"""
    (row_id,) = _seed(client, [{
        "tag_id": "tag_1", "origin": "closed", "candidate_name": None,
        "candidate_description": None, "relevance_score": 0.9,
        "confidence": 0.7, "task_id": "gen_closed"}])
    resp = client.post(f"/api/v1/asset-tags/{row_id}/review",
                       json={"action": "approve"})
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == RESULT_KEYS
    assert body["id"] == row_id
    assert body["review_status"] == "approved"
    assert body["tag_name"] == "悬疑"  # define 名（candidate 为空回退）
    assert body["tag_id"] == "tag_1" and body["origin"] == "closed"
    assert body["relevance_score"] == 0.9 and body["confidence"] == 0.7

    with client.app.state.session_factory() as s:
        row = s.get(PreAssetTag, row_id)
        assert row.reviewer_id == "admin" and row.review_comment is None
        mount = s.query(AssetTag).one()
        assert mount.asset_id == "a1" and mount.tag_id == "tag_1"
        assert mount.tag_name == "悬疑"
        assert mount.target_type == "content" and mount.target_id == "a1"


def test_review_conflict_409(client):
    """同一行二次审核 → 409（终态不被覆盖，detail "该标签已被处理"）。"""
    (row_id,) = _seed(client, [{"tag_id": "tag_1", "origin": "closed",
                                "candidate_name": None,
                                "candidate_description": None}])
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
    (row_id,) = _seed(client, [{"tag_id": "tag_1", "origin": "closed",
                                "candidate_name": None,
                                "candidate_description": None}])
    resp = client.post(f"/api/v1/asset-tags/{row_id}/review",
                       json={"action": "alien"})
    assert resp.status_code == 422
    # 零副作用：行未被处理
    with client.app.state.session_factory() as s:
        assert s.get(PreAssetTag, row_id).review_status == "pending"


def test_review_modify_approves_in_one_step(client):
    """modify：候选名/描述更新 + 直接 approved（不回 pending）+ 复制挂载
    （tag_name 取 define 名）；comment/reviewer 落行。"""
    (row_id,) = _seed(client, [{"tag_id": "tag_1", "origin": "open",
                                "candidate_name": "悬疑变"}])
    resp = client.post(f"/api/v1/asset-tags/{row_id}/review", json={
        "action": "modify",
        "payload": {"candidate_name": "悬疑改", "candidate_description": "新描述",
                    "comment": "措辞修正"}})
    assert resp.status_code == 200
    body = resp.json()
    assert body["review_status"] == "approved"
    assert body["candidate_name"] == "悬疑改"

    with client.app.state.session_factory() as s:
        row = s.get(PreAssetTag, row_id)
        assert row.candidate_name == "悬疑改"
        assert row.candidate_description == "新描述"
        assert row.reviewer_id == "admin" and row.review_comment == "措辞修正"
        mount = s.query(AssetTag).one()
        assert (mount.tag_id, mount.tag_name) == ("tag_1", "悬疑")


def test_review_free_candidate_approve_or_modify_422(client):
    """open 自由候选行（tag_id 空）直接 approve/modify → 422（须先合并入库）。"""
    (row_id,) = _seed(client, [{"candidate_name": "自由候选"}])
    for action in ("approve", "modify"):
        resp = client.post(f"/api/v1/asset-tags/{row_id}/review", json={
            "action": action,
            "payload": {"candidate_name": "新名"} if action == "modify" else None})
        assert resp.status_code == 422, action
        assert "合并" in resp.json()["detail"]
    with client.app.state.session_factory() as s:
        row = s.get(PreAssetTag, row_id)
        assert row.review_status == "pending" and row.tag_id is None
        assert s.query(AssetTag).count() == 0


def test_review_modify_string_field_validation_422(client):
    """modify 字符串字段空串/纯空白/超长 → 422，零副作用（行原样）。"""
    (row_id,) = _seed(client, [{"tag_id": "tag_1", "origin": "open",
                                "candidate_name": "悬疑变"}])
    cases = [
        {"candidate_name": ""},          # 空串
        {"candidate_name": "   "},       # 纯空白
        {"candidate_name": "长" * 201},  # 超长（上限 200）
        {},                              # 无可改字段
    ]
    for payload in cases:
        resp = client.post(f"/api/v1/asset-tags/{row_id}/review",
                           json={"action": "modify", "payload": payload})
        assert resp.status_code == 422, f"{payload} 应 422"
    with client.app.state.session_factory() as s:
        row = s.get(PreAssetTag, row_id)
        assert row.review_status == "pending"
        assert row.candidate_name == "悬疑变"
        assert s.query(AssetTag).count() == 0


# ---------- merge 两支 ----------


def test_review_merge_into_existing(client):
    """merge 并入既有：tag_id 赋值、candidate_name 原文保留、approved + 挂载
    （tag_name 取 define 名）；不建新标签。"""
    (row_id,) = _seed(client, [{"candidate_name": "多线叙事"}])
    resp = client.post(f"/api/v1/asset-tags/{row_id}/review", json={
        "action": "merge", "payload": {"merge_to_tag_id": "tag_2",
                                       "comment": "并入既有"}})
    assert resp.status_code == 200
    body = resp.json()
    assert body["review_status"] == "approved"
    assert body["tag_id"] == "tag_2"
    assert body["candidate_name"] == "多线叙事"  # 原文保留可溯

    with client.app.state.session_factory() as s:
        assert s.query(TagDefine).count() == 3  # 未新建定义
        mount = s.query(AssetTag).one()
        assert (mount.tag_id, mount.tag_name) == ("tag_2", "紧张")


def test_review_merge_into_new_tag(client):
    """merge 合入新建：建 TagDefine（val_fixed）+ change_log(action='merge')
    + pre 行赋 tag_id/candidate_dir_id + 挂载取新定义名。"""
    (row_id,) = _seed(client, [{"candidate_name": "光污染"}])
    resp = client.post(f"/api/v1/asset-tags/{row_id}/review", json={
        "action": "merge", "payload": {
            "new_tag_name": "视觉冲击", "new_dir_id": "dir_1",
            "new_description": "视效冲击力", "comment": "库内确无对应"}})
    assert resp.status_code == 200
    body = resp.json()
    assert body["review_status"] == "approved"
    assert body["candidate_name"] == "光污染"  # 原文保留

    with client.app.state.session_factory() as s:
        define = (s.query(TagDefine)
                  .filter(TagDefine.tag_name == "视觉冲击").one())
        assert define.tag_type == "val_fixed" and define.dir_id == "dir_1"
        log = s.query(TagChangeLog).one()
        assert (log.tag_id, log.action, log.operator) == (
            define.tag_id, "merge", "admin")
        row = s.get(PreAssetTag, row_id)
        assert row.tag_id == define.tag_id and row.candidate_dir_id == "dir_1"
        mount = s.query(AssetTag).one()
        assert mount.tag_id == define.tag_id
        assert mount.tag_name == "视觉冲击"


def test_review_merge_422_bad_or_missing_payload(client):
    """merge 载荷校验：缺载荷 / 未知目标 / 合入新建同名（提示并入既有）/
    未知目录 → 422，零副作用。"""
    (row_id,) = _seed(client, [{"candidate_name": "悬疑"}])  # 与 tag_1 同名
    missing = client.post(f"/api/v1/asset-tags/{row_id}/review",
                          json={"action": "merge", "payload": {}})
    assert missing.status_code == 422
    unknown = client.post(f"/api/v1/asset-tags/{row_id}/review", json={
        "action": "merge", "payload": {"merge_to_tag_id": "tag_none"}})
    assert unknown.status_code == 422
    assert "目标标签不存在" in unknown.json()["detail"]
    same_name = client.post(f"/api/v1/asset-tags/{row_id}/review", json={
        "action": "merge", "payload": {"new_tag_name": "悬疑",
                                       "new_dir_id": "dir_1"}})
    assert same_name.status_code == 422
    assert "并入既有" in same_name.json()["detail"]
    unknown_dir = client.post(f"/api/v1/asset-tags/{row_id}/review", json={
        "action": "merge", "payload": {"new_tag_name": "视觉冲击",
                                       "new_dir_id": "dir_none"}})
    assert unknown_dir.status_code == 422
    with client.app.state.session_factory() as s:
        row = s.get(PreAssetTag, row_id)
        assert row.review_status == "pending" and row.tag_id is None
        assert s.query(TagDefine).count() == 3
        assert s.query(TagChangeLog).count() == 0


def test_review_merge_duplicate_target_409(client):
    """重复并入撞唯一索引：同资产两条候选先后并入同一标签，第二条撞
    ux_pre_tag_catalog/ux_asset_tag → 409 detail "该资产已挂此标签"，
    行状态不变、无重复挂载。"""
    row_a, row_b = _seed(client, [{"candidate_name": "候选甲"},
                                  {"candidate_name": "候选乙"}])
    first = client.post(f"/api/v1/asset-tags/{row_a}/review", json={
        "action": "merge", "payload": {"merge_to_tag_id": "tag_1"}})
    assert first.status_code == 200
    second = client.post(f"/api/v1/asset-tags/{row_b}/review", json={
        "action": "merge", "payload": {"merge_to_tag_id": "tag_1"}})
    assert second.status_code == 409
    assert second.json()["detail"] == "该资产已挂此标签"
    with client.app.state.session_factory() as s:
        row = s.get(PreAssetTag, row_b)
        assert row.review_status == "pending" and row.tag_id is None
        assert row.candidate_name == "候选乙"
        assert s.query(AssetTag).count() == 1  # 仅首条挂载


# ---------- 批量审核 ----------


def test_batch_review_per_item_independent(client):
    """批量逐条独立：未知 id 走 per-item 失败（ok=false + detail）不中断，
    成功条目照常落库；响应与 ids 等长同序。"""
    ids = _seed(client, [{"tag_id": "tag_1", "origin": "closed",
                          "candidate_name": None,
                          "candidate_description": None},
                         {"candidate_name": "候选一"},
                         {"tag_id": "tag_2", "origin": "closed",
                          "candidate_name": None,
                          "candidate_description": None}])
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
        assert statuses[ids[1]] == "pending"  # 未被触及
        assert s.query(AssetTag).count() == 2


def test_batch_review_partial_conflict(client):
    """批量含已处理行 → 整请求 200，冲突条目 ok=false detail "该标签已被
    处理"，其余条目照常成功。"""
    ids = _seed(client, [{"tag_id": "tag_1", "origin": "closed",
                          "candidate_name": None,
                          "candidate_description": None},
                         {"tag_id": "tag_2", "origin": "closed",
                          "candidate_name": None,
                          "candidate_description": None}])
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


def test_batch_review_reject(client):
    """批量拒绝：全部 rejected 且零挂载。"""
    ids = _seed(client, [{"candidate_name": "候选一"},
                         {"candidate_name": "候选二"}])
    resp = client.post("/api/v1/asset-tags/batch-review", json={
        "ids": ids, "action": "reject", "payload": {"comment": "批量驳回"}})
    assert resp.status_code == 200
    assert all(r["ok"] for r in resp.json()["results"])
    with client.app.state.session_factory() as s:
        statuses = {r.review_status for r in s.query(PreAssetTag).all()}
        assert statuses == {"rejected"}
        assert s.query(AssetTag).count() == 0


def test_batch_review_merge_action_rejected_422(client):
    """批量仅 approve/reject（modify/merge 载荷逐行各异）：merge → action
    级 422 整请求拒绝，零部分执行。"""
    ids = _seed(client, [{"candidate_name": "候选一"},
                         {"candidate_name": "候选二"}])
    resp = client.post("/api/v1/asset-tags/batch-review", json={
        "ids": ids, "action": "merge",
        "payload": {"merge_to_tag_id": "tag_1"}})
    assert resp.status_code == 422
    with client.app.state.session_factory() as s:
        assert all(r.review_status == "pending"
                   for r in s.query(PreAssetTag).all())
        assert s.query(AssetTag).count() == 0


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
        assert fresh.get("/api/v1/review/items").status_code == 401
        assert fresh.get("/api/v1/review/filters").status_code == 401


# ---------- 审核列表 ----------


def test_review_items_default_ordering_and_row_shape(client):
    """默认列表：confidence 升序 nulls-last（低分先审，无置信度行置后）；
    行键集与展示解析——tag_name = candidate 或 define 名、dir_name =
    define 目录 or candidate 目录 or None、asset_title、evidence 解析、
    task_id 直取行内列。"""
    ids = _seed_scene(client)
    resp = client.get("/api/v1/review/items")
    assert resp.status_code == 200
    body = resp.json()
    assert body["page"] == 1 and body["total"] == 6
    # 0.3 → 0.5 → 0.6 → 0.7 → 0.95 → None（nulls-last）
    assert [it["id"] for it in body["items"]] == [
        ids["r1"], ids["r3"], ids["r6"], ids["r2"], ids["r4"], ids["r5"]]

    by_id = {it["id"]: it for it in body["items"]}
    # r1：open 自由候选——tag_name 取候选名、dir_name None、任务关联
    r1 = by_id[ids["r1"]]
    assert set(r1.keys()) == REVIEW_ITEM_KEYS
    assert r1["tag_name"] == "反转" and r1["tag_id"] is None
    assert r1["candidate_name"] == "反转" and r1["dir_name"] is None
    assert r1["origin"] == "open" and r1["review_status"] == "pending"
    assert r1["asset_id"] == "a1" and r1["asset_title"] == "悬疑片A"
    assert r1["confidence"] == 0.3 and r1["relevance"] is None
    assert r1["task_id"] == "gen_open"
    assert r1["evidence"] == [{"scene_id": "s1", "start_ms": 0, "end_ms": 100,
                               "description": "开场反转"}]
    # r2：closed 定义行——tag_name 回退 define 名、dir_name 经 define 取
    r2 = by_id[ids["r2"]]
    assert r2["tag_name"] == "悬疑" and r2["dir_name"] == "剧情"
    assert r2["relevance"] == 0.9 and r2["origin"] == "closed"
    # r3：合入新建行——candidate 优先显示、dir_name 经 define 取（候选目录同值）
    r3 = by_id[ids["r3"]]
    assert r3["tag_name"] == "紧张感" and r3["dir_name"] == "情绪"
    assert r3["tag_id"] == "tag_2" and r3["review_status"] == "approved"
    # r5：open 归一命中行——candidate 优先于 define 名、无置信度排最后
    r5 = by_id[ids["r5"]]
    assert r5["tag_name"] == "悬疑变" and r5["dir_name"] == "剧情"
    assert r5["confidence"] is None
    # r6：无任务行 task_id None
    assert by_id[ids["r6"]]["task_id"] is None


def test_review_items_filter_status(client):
    """status 筛选：三态精确匹配；未知状态 → 空列表不报错。"""
    ids = _seed_scene(client)
    resp = client.get("/api/v1/review/items", params={"status": "approved"})
    assert resp.json()["total"] == 1
    assert resp.json()["items"][0]["id"] == ids["r3"]

    resp = client.get("/api/v1/review/items", params={"status": "rejected"})
    assert resp.json()["total"] == 1
    assert resp.json()["items"][0]["id"] == ids["r4"]

    resp = client.get("/api/v1/review/items", params={"status": "pending"})
    assert resp.json()["total"] == 4

    assert client.get("/api/v1/review/items",
                      params={"status": "nonexistent"}).json()["total"] == 0


def test_review_items_filter_dir_exact_match(client):
    """dir 筛选：目录名精确匹配（define 目录 or 候选目录任一命中）；
    子串不命中。"""
    ids = _seed_scene(client)
    resp = client.get("/api/v1/review/items", params={"dir": "剧情"})
    body = resp.json()
    assert body["total"] == 3  # r2/r4（define 目录 dir_1）+ r5（tag_1@dir_1）
    assert {it["id"] for it in body["items"]} == {
        ids["r2"], ids["r4"], ids["r5"]}

    resp = client.get("/api/v1/review/items", params={"dir": "情绪"})
    assert resp.json()["total"] == 1
    assert resp.json()["items"][0]["id"] == ids["r3"]

    # 精确匹配：子串/超集均不命中
    assert client.get("/api/v1/review/items",
                      params={"dir": "剧"}).json()["total"] == 0
    assert client.get("/api/v1/review/items",
                      params={"dir": "剧情类"}).json()["total"] == 0


def test_review_items_filter_origin_and_asset(client):
    """origin 与 asset_id 筛选各自生效且可组合。"""
    ids = _seed_scene(client)
    resp = client.get("/api/v1/review/items", params={"origin": "closed"})
    assert resp.json()["total"] == 2
    assert {it["id"] for it in resp.json()["items"]} == {ids["r2"], ids["r4"]}

    resp = client.get("/api/v1/review/items", params={"origin": "open"})
    assert resp.json()["total"] == 4

    resp = client.get("/api/v1/review/items", params={"asset_id": "a2"})
    assert resp.json()["total"] == 3
    assert {it["id"] for it in resp.json()["items"]} == {
        ids["r3"], ids["r4"], ids["r5"]}

    # 组合：a2 上的 open 行只剩 r3/r5
    resp = client.get("/api/v1/review/items", params={
        "asset_id": "a2", "origin": "open"})
    assert resp.json()["total"] == 2

    # 组合：dir + status
    resp = client.get("/api/v1/review/items", params={
        "dir": "剧情", "status": "pending"})
    assert {it["id"] for it in resp.json()["items"]} == {ids["r2"], ids["r5"]}


def test_review_items_filter_confidence_bounds(client):
    """confidence_min/max 闭区间筛选：上下界同时给与单独给均可；无置信度行
    不落任何区间。"""
    ids = _seed_scene(client)
    resp = client.get("/api/v1/review/items", params={
        "confidence_min": 0.5, "confidence_max": 0.7})
    body = resp.json()
    assert body["total"] == 3
    assert [it["id"] for it in body["items"]] == [
        ids["r3"], ids["r6"], ids["r2"]]  # 区间内仍按升序
    assert client.get("/api/v1/review/items",
                      params={"confidence_min": 0.8}).json()["total"] == 1
    assert client.get("/api/v1/review/items",
                      params={"confidence_max": 0.5}).json()["total"] == 2


def test_review_items_pagination(client):
    """分页：page/page_size 截断且 total 恒为过滤后总数、page 回显请求页；
    末页之后为空列表。"""
    ids = _seed_scene(client)
    resp = client.get("/api/v1/review/items", params={"page": 2, "page_size": 2})
    body = resp.json()
    assert body["page"] == 2 and body["total"] == 6
    assert [it["id"] for it in body["items"]] == [ids["r6"], ids["r2"]]

    resp = client.get("/api/v1/review/items", params={"page": 3, "page_size": 2})
    assert [it["id"] for it in resp.json()["items"]] == [ids["r4"], ids["r5"]]

    resp = client.get("/api/v1/review/items", params={"page": 4, "page_size": 2})
    assert resp.json()["items"] == [] and resp.json()["total"] == 6


# ---------- 审核筛选下拉 ----------


def test_review_filters_endpoint(client):
    """filters 端点：statuses/origins 契约固定枚举，dirs 为 tag_catalog 库内
    目录名去重升序。"""
    _seed_scene(client)
    resp = client.get("/api/v1/review/filters")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"statuses", "origins", "dirs"}
    assert body["statuses"] == ["pending", "approved", "rejected"]
    assert body["origins"] == ["closed", "open"]
    assert body["dirs"] == ["剧情", "情绪"]  # 升序（Unicode 序）


def test_review_filters_empty_catalog(client):
    """空库：dirs 为空列表（statuses/origins 仍回固定枚举）。"""
    resp = client.get("/api/v1/review/filters")
    assert resp.status_code == 200
    body = resp.json()
    assert body["dirs"] == []
    assert body["statuses"] == ["pending", "approved", "rejected"]
    assert body["origins"] == ["closed", "open"]
