"""内容导入与资产查询 API 测试（Task 11）。

覆盖契约（错误映射约定见 brief）：
- POST /api/v1/content/import：合法平台格式导入端到端（影片/场景落库 +
  内容落在影片行 + 完整 content JSON 取回）；
  video_root 不存在 → 422（Review Focus 4）；未知 kind → 422 且 detail
  列出可用 kind（含 "platform"）；data 校验失败 → 422 带摘要；
  parent_id 指向不存在资产 → 422（修复 1：原先裸 IntegrityError 500）；
- GET /api/v1/assets：asset_type 过滤 + q 对 title 子串匹配，含
  video_uri/parent_id；
- GET /api/v1/assets/{asset_id}：详情字段（不含 content_json 大字段）；
- GET /api/v1/assets/{asset_id}/content：完整 content JSON；
  未知资产/无内容 → 404；
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
    assert detail["has_content"] is True
    full = client.get(f"/api/v1/assets/{body['asset_id']}/content")
    assert full.json()["content"]["description"]


VISION_PAYLOAD = {
    "schema_version": "1.0",
    "asset": {"asset_id": "movie_vision_api", "asset_type": "movie",
              "title": "上游格式样例", "duration_ms": 3_600_000, "language": "zh-CN"},
    "content": {
        "description": "一名记者追查旧案，发现自己父亲当年被冤枉。",
        "characters": [{"character_id": "c1", "name": "周野", "description": "记者"}],
        "scenes": [{
            "scene_id": "s1", "start_ms": 0, "end_ms": 60_000,
            "description": "档案室翻找卷宗，灰尘在光柱里浮动。",
            "character_ids": ["c1"],
            "emotion": [{"name": "压抑"}],
            "dialogues": [{"speaker_id": "c1", "speaker_name": "周野",
                           "text": "这份笔录的签名是伪造的。",
                           "start_ms": 10_000, "end_ms": 13_000}],
            "audio_events": [{"event_type": "background_music",
                              "description": "单音钢琴", "start_ms": 0, "end_ms": 60_000}],
        }],
    },
}


def test_content_import_vision_kind(client):
    """kind="vision"：上游格式经适配器落库，读端点按上游模板形态返回。"""
    resp = client.post("/api/v1/content/import", json={
        "kind": "vision", "video_root": None, "data": VISION_PAYLOAD})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    ctx = client.get(f"/api/v1/assets/{body['asset_id']}/content").json()
    assert ctx["content"]["description"] == "一名记者追查旧案，发现自己父亲当年被冤枉。"
    assert ctx["content"]["characters"][0]["name"] == "周野"
    scene = ctx["content"]["scenes"][0]
    assert scene["start_ms"] == 0 and scene["end_ms"] == 60_000
    assert scene["emotion"][0]["name"] == "压抑"
    assert scene["audio_events"][0]["event_type"] == "background_music"
    # 模板字段全量：列表字段缺失写 []，标量缺失写 null
    assert scene["events"] == [] and scene["virtual_time"] is None
    assert "summary" not in ctx["content"] and "metadata" not in ctx


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
    # 排序返回：platform（平台标准格式）+ vision（上游视频理解格式）
    assert client.get("/api/v1/adapters").json() == ["platform", "vision"]


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

    # q 对 title 子串匹配：场景标题是 `{片名}_{scene_id}`，片名会同时命中
    # 影片行与它的场景行
    hits = {a["asset_id"] for a in client.get("/api/v1/assets?q=示例").json()}
    assert hits == {body["asset_id"], *body["scene_asset_ids"]}
    assert client.get("/api/v1/assets?q=不存在的词").json() == []


def test_asset_detail_and_404(client):
    resp = client.post("/api/v1/content/import", json={
        "kind": "platform", "video_root": None, "data": CONTENT_FIXTURE_DICT})
    body = resp.json()

    # 详情：资产字段齐全，不含 content_json 大字段
    detail = client.get(f"/api/v1/assets/{body['asset_id']}").json()
    assert detail["asset_type"] == "movie" and detail["video_uri"]
    assert detail["has_content"] is True and detail["updated_at"]
    assert "content_json" not in detail and "contexts" not in detail

    # 场景行是物理切片：没有内容
    scene_detail = client.get(f"/api/v1/assets/{body['scene_asset_ids'][0]}").json()
    assert scene_detail["has_content"] is False

    # 未知资产 / 无内容场景 → 404
    assert client.get("/api/v1/assets/asset_none").status_code == 404
    assert client.get(
        f"/api/v1/assets/{body['scene_asset_ids'][0]}/content").status_code == 404


def test_asset_content_endpoint_reads_media_asset(client):
    """GET /assets/{id}/content：主内容取自 media_asset.content_json（v2 唯一
    数据源）；无内容/未知资产 → 404。"""
    body = client.post("/api/v1/content/import", json={
        "kind": "platform", "video_root": None, "data": CONTENT_FIXTURE_DICT}).json()
    asset_id = body["asset_id"]

    content = client.get(f"/api/v1/assets/{asset_id}/content")
    assert content.status_code == 200
    assert content.json()["asset"]["asset_id"] == asset_id

    # 场景资产无主内容：404；未知资产：404
    assert client.get(
        f"/api/v1/assets/{body['scene_asset_ids'][0]}/content").status_code == 404
    assert client.get("/api/v1/assets/asset_none/content").status_code == 404
