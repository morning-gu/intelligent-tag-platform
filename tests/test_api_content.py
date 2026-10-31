"""内容导入与资产查询 API 测试（Task 11）。

覆盖契约（错误映射约定见 brief）：
- POST /api/v1/content/import：合法平台格式导入端到端（影片/场景/上下文
  落库 + 详情 contexts 含 recognition_status + 完整 content JSON 取回）；
  video_root 不存在 → 422（Review Focus 4）；未知 kind → 422 且 detail
  列出可用 kind（含 "platform"）；data 校验失败 → 422 带摘要；
  parent_id 指向不存在资产 → 422（修复 1：原先裸 IntegrityError 500）；
- GET /api/v1/assets：asset_type 过滤 + q 对 title 子串匹配，含
  video_uri/parent_id；
- GET /api/v1/assets/{asset_id}：详情含 contexts 摘要（不含 content_json
  大字段）；
- GET /api/v1/assets/{asset_id}/contexts/{context_id}：完整 content JSON；
  未知资产/上下文/上下文不属于该资产 → 404；
- GET /api/v1/adapters：已注册 kind 列表。
"""

import json
from pathlib import Path

# 原始内容 JSON dict（未经 parse_content 解析，供 POST body 直接携带）
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "content_movie.json"
CONTENT_FIXTURE_DICT = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def test_content_import_via_api(client):
    resp = client.post("/api/v1/content/import", json={
        "kind": "platform", "video_root": None, "data": CONTENT_FIXTURE_DICT})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["scene_asset_ids"]) == 1
    detail = client.get(f"/api/v1/assets/{body['asset_id']}").json()
    assert detail["contexts"][0]["recognition_status"] == "completed"
    full = client.get(f"/api/v1/assets/{body['asset_id']}/contexts/{body['context_id']}")
    assert full.json()["content"]["summary"]


def test_content_import_bad_video_root_422(client):   # Review Focus 4
    resp = client.post("/api/v1/content/import", json={
        "kind": "platform", "video_root": "Z:/不存在", "data": CONTENT_FIXTURE_DICT})
    assert resp.status_code == 422


def test_content_import_parent_missing_422(client):
    """修复 1：parent_id 指向不存在资产 → 422（原先裸 IntegrityError 500）。"""
    bad = {**CONTENT_FIXTURE_DICT,
           "asset": {**CONTENT_FIXTURE_DICT["asset"], "parent_id": "asset_none"}}
    resp = client.post("/api/v1/content/import", json={
        "kind": "platform", "video_root": None, "data": bad})
    assert resp.status_code == 422
    assert "parent 资产不存在" in resp.json()["detail"]


def test_content_import_unknown_kind_422(client):
    resp = client.post("/api/v1/content/import", json={
        "kind": "nonexistent", "video_root": None, "data": CONTENT_FIXTURE_DICT})
    assert resp.status_code == 422 and "platform" in resp.json()["detail"]


def test_content_import_bad_data_422(client):
    """data 缺必需字段：适配器内 pydantic.ValidationError → 422 带摘要。"""
    bad = {**CONTENT_FIXTURE_DICT,
           "asset": {**CONTENT_FIXTURE_DICT["asset"], "title": None}}
    resp = client.post("/api/v1/content/import", json={
        "kind": "platform", "video_root": None, "data": bad})
    assert resp.status_code == 422
    assert "校验失败" in resp.json()["detail"]


def test_adapters_endpoint(client):
    assert client.get("/api/v1/adapters").json() == ["platform"]


def test_assets_list_and_filters(client):
    resp = client.post("/api/v1/content/import", json={
        "kind": "platform", "video_root": None, "data": CONTENT_FIXTURE_DICT})
    body = resp.json()

    # 影片 + 场景两条资产；场景行含 parent_id 与 video_uri
    listing = client.get("/api/v1/assets").json()
    assert {a["asset_id"] for a in listing} == {
        body["asset_id"], *body["scene_asset_ids"]}
    scene = next(a for a in listing if a["asset_type"] == "scene")
    assert scene["parent_id"] == body["asset_id"] and scene["video_uri"]

    # asset_type 精确过滤
    movies = client.get("/api/v1/assets?asset_type=movie").json()
    assert [a["asset_id"] for a in movies] == [body["asset_id"]]

    # q 对 title 子串匹配（影片 title=示例电影；场景 title=场景描述）
    assert [a["asset_id"] for a in client.get("/api/v1/assets?q=示例").json()] == [
        body["asset_id"]]
    assert client.get("/api/v1/assets?q=仓库").json()[0]["asset_id"] in body["scene_asset_ids"]
    assert client.get("/api/v1/assets?q=不存在的词").json() == []


def test_asset_detail_and_404(client):
    resp = client.post("/api/v1/content/import", json={
        "kind": "platform", "video_root": None, "data": CONTENT_FIXTURE_DICT})
    body = resp.json()

    # 详情：contexts 摘要字段齐全且不含 content_json 大字段
    detail = client.get(f"/api/v1/assets/{body['asset_id']}").json()
    assert detail["asset_type"] == "movie" and detail["video_uri"]
    ctx = detail["contexts"][0]
    assert ctx["context_id"] == body["context_id"]
    assert ctx["model_version"] == "vlm_v1" and ctx["created_at"]
    assert "content_json" not in ctx

    # 场景资产没有 media_context：contexts 为空列表
    scene_detail = client.get(f"/api/v1/assets/{body['scene_asset_ids'][0]}").json()
    assert scene_detail["contexts"] == []

    # 未知资产 / 未知上下文 / 上下文不属于该资产 → 404
    assert client.get("/api/v1/assets/asset_none").status_code == 404
    assert client.get(
        f"/api/v1/assets/{body['asset_id']}/contexts/ctx_none").status_code == 404
    assert client.get(
        f"/api/v1/assets/{body['scene_asset_ids'][0]}/contexts/{body['context_id']}"
    ).status_code == 404
