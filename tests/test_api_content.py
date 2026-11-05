"""内容导入与资产查询 API 测试（v2 合并语义）。

覆盖契约（错误映射约定见 brief）：
- POST /api/v1/content/import：合法平台格式导入端到端（影片/场景落库 +
  content_json 直写影片级行 + 详情带 recognized + 完整 content JSON 经
  GET /assets/{asset_id}/content 取回）；响应形状 {asset_id, scene_asset_ids}
  （context_id 退役）；video_root 不存在 → 422；未知 kind → 422 且 detail
  列出可用 kind（含 "platform"）；data 校验失败 → 422 带摘要；
  parent_id 指向不存在资产 → 422；
- GET /api/v1/assets：asset_type 过滤 + q 对 title 子串匹配，含
  video_uri/parent_id（本任务不动，回归钉住）；
- GET /api/v1/assets/{asset_id}：详情带 recognized（content_json 非空），
  不再有 contexts 数组；
- GET /api/v1/assets/{asset_id}/content：完整 content JSON；未知资产 → 404
  （"资产不存在"）、content_json 为空 → 404（"未完成内容识别"）；
- GET /api/v1/assets/{asset_id}/contexts/{context_id}：路由已删除 → 404；
- GET /api/v1/adapters：已注册 kind 列表（本任务不动，回归钉住）。
"""

import json
from pathlib import Path

from tagplatform.db.models import MediaAsset

# 原始内容 JSON dict（未经 parse_content 解析，供 POST body 直接携带）
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "content_movie.json"
CONTENT_FIXTURE_DICT = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def test_content_import_via_api(client):
    resp = client.post("/api/v1/content/import", json={
        "kind": "platform", "video_root": None, "data": CONTENT_FIXTURE_DICT})
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"asset_id", "scene_asset_ids"}   # context_id 退役
    assert len(body["scene_asset_ids"]) == 1
    detail = client.get(f"/api/v1/assets/{body['asset_id']}").json()
    assert detail["recognized"] is True
    full = client.get(f"/api/v1/assets/{body['asset_id']}/content")
    assert full.status_code == 200
    parsed = full.json()
    assert parsed["content"]["summary"]
    # 识别元信息在 content_json 内（doc §3.1：不单设 model_version 列）
    assert parsed["metadata"]["recognition_status"] == "completed"
    assert parsed["metadata"]["model_version"] == "vlm_v1"


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
    """kind="vision"：上游格式经适配器落库，字段进入统一内容 JSON。"""
    resp = client.post("/api/v1/content/import", json={
        "kind": "vision", "video_root": None, "data": VISION_PAYLOAD})
    assert resp.status_code == 200, resp.text
    asset_id = resp.json()["asset_id"]
    ctx = client.get(f"/api/v1/assets/{asset_id}/content").json()
    assert ctx["metadata"]["source"] == "video_understanding"
    assert ctx["content"]["characters"][0]["name"] == "周野"
    assert ctx["content"]["scenes"][0]["emotions"] == ["压抑"]
    assert ctx["content"]["scenes"][0]["audio_events"][0]["event_type"] == "background_music"


def test_content_endpoint_404_unrecognized(client):
    """content_json 为空（上传占位资产未识别）→ 404，detail 含"未完成内容识别"。"""
    with client.app.state.session_factory() as session:
        session.add(MediaAsset(asset_id="movie_raw", asset_type="movie",
                               title="未识别占位"))
        session.commit()

    resp = client.get("/api/v1/assets/movie_raw/content")
    assert resp.status_code == 404
    assert "未完成内容识别" in resp.json()["detail"]


def test_content_endpoint_404_unknown_asset(client):
    resp = client.get("/api/v1/assets/asset_none/content")
    assert resp.status_code == 404
    assert "资产不存在" in resp.json()["detail"]


def test_contexts_route_removed(client):
    """v2 合并：contexts 端点整条退役 → 404（无此路由）。"""
    resp = client.post("/api/v1/content/import", json={
        "kind": "platform", "video_root": None, "data": CONTENT_FIXTURE_DICT})
    asset_id = resp.json()["asset_id"]
    assert client.get(
        f"/api/v1/assets/{asset_id}/contexts/ctx_any").status_code == 404


def test_content_import_bad_video_root_422(client):
    resp = client.post("/api/v1/content/import", json={
        "kind": "platform", "video_root": "Z:/不存在", "data": CONTENT_FIXTURE_DICT})
    assert resp.status_code == 422


def test_content_import_parent_missing_422(client):
    """parent_id 指向不存在资产 → 422（写库前拦截，杜绝裸 IntegrityError 500）。"""
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

    # q 对 title 子串匹配（影片 title=示例电影；场景 title=场景描述）
    assert [a["asset_id"] for a in client.get("/api/v1/assets?q=示例").json()] == [
        body["asset_id"]]
    assert client.get("/api/v1/assets?q=仓库").json()[0]["asset_id"] in body["scene_asset_ids"]
    assert client.get("/api/v1/assets?q=不存在的词").json() == []


def test_asset_detail_recognized_and_404(client):
    resp = client.post("/api/v1/content/import", json={
        "kind": "platform", "video_root": None, "data": CONTENT_FIXTURE_DICT})
    body = resp.json()

    # 详情：recognized=True（content_json 非空），contexts 数组已退役，
    # content_json 大字段不出详情
    detail = client.get(f"/api/v1/assets/{body['asset_id']}").json()
    assert detail["asset_type"] == "movie" and detail["video_uri"]
    assert detail["recognized"] is True
    assert "contexts" not in detail
    assert "content_json" not in detail

    # 场景资产没有内容识别：recognized=False
    scene_detail = client.get(f"/api/v1/assets/{body['scene_asset_ids'][0]}").json()
    assert scene_detail["recognized"] is False

    # 未知资产 → 404
    assert client.get("/api/v1/assets/asset_none").status_code == 404
