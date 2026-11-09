"""内容 JSON 存储形态测试（2026-11：库里存完整上游模板，读时归一化）。

覆盖：
- 上游模板形态入库后，`content_json` 是按模板补全后的形态（字段全量、
  缺值 null / 空列表 []，键名不变）；
- 读端点同样返回模板形态（description / start_ms / emotion[{name,code}]）；
- 历史内部形态（summary/time_range）入库即归一为模板，读取仍可还原；
- 判别函数对两种形态与空场景的判定。
"""

import json

from tagplatform.content_io import is_upstream_shape, load_content
from tagplatform.db.models import MediaAsset
from tagplatform.schemas import parse_content

TEMPLATE = {
    "schema_version": "1.0",
    "asset": {"asset_id": "movie_tt1", "asset_type": "movie", "title": "片名",
              "duration_ms": 1000, "video_uri": None, "language": "en"},
    "content": {
        "description": "整片剧情",
        "characters": [{"character_id": "character_001", "name": "张三",
                        "aliases": [], "description": "由 李四 饰演"}],
        "scenes": [{
            "scene_id": "s1", "start_ms": 0, "end_ms": 1000,
            "description": "场景描述", "virtual_time": None, "location": None,
            "character_ids": ["character_001"],
            "events": [{"event_id": "e1", "description": "事件",
                        "participant_ids": [{"character_id": "character_001",
                                             "emotion": [{"name": "愤怒",
                                                          "code": "ANGER"}]}]}],
            "emotion": [{"name": "紧张压迫", "code": "TENSE_OPPRESSIVE"}],
            "dialogues": [{"dialogue_id": "d1", "speaker_id": "character_001",
                           "speaker_name": "张三", "text": "你好",
                           "start_ms": 0, "end_ms": 100}],
            "audio_events": [],
        }],
    },
}


def test_template_is_stored_as_template(client):
    """经 /content/import 提交的模板 JSON，库里按模板全量保存（键名不变）。"""
    resp = client.post("/api/v1/content/import",
                       json={"kind": "vision", "data": TEMPLATE})
    assert resp.status_code == 200, resp.text
    asset_id = resp.json()["asset_id"]

    with client.app.state.session_factory() as session:
        row = session.get(MediaAsset, asset_id)
        stored = json.loads(row.content_json)

    assert stored == TEMPLATE                       # 一字不差
    assert "start_ms" in stored["content"]["scenes"][0]
    assert "summary" not in stored["content"]        # 没有被改写成内部形态
    assert "time_range" not in stored["content"]["scenes"][0]


def test_api_reads_back_template_shape(client):
    """读端点返回上游模板形态（description / start_ms / emotion[{name,code}]）。"""
    resp = client.post("/api/v1/content/import",
                       json={"kind": "vision", "data": TEMPLATE})
    asset_id = resp.json()["asset_id"]
    detail = client.get(f"/api/v1/assets/{asset_id}/content")
    assert detail.status_code == 200
    body = detail.json()
    assert body["content"]["description"] == "整片剧情"
    assert "summary" not in body["content"]
    scene = body["content"]["scenes"][0]
    assert (scene["start_ms"], scene["end_ms"]) == (0, 1000)
    assert scene["emotion"] == [{"name": "紧张压迫", "code": "TENSE_OPPRESSIVE"}]
    assert scene["dialogues"][0]["speaker_name"] == "张三"
    assert "time_range" not in scene


def test_legacy_internal_shape_read_as_template(client):
    """2026-11 之前入库的内部形态：入库即归一为模板，读端点也按模板返回。"""
    legacy = {
        "schema_version": "1.0",
        "asset": {"asset_id": "movie_old", "asset_type": "movie", "title": "旧片"},
        "content": {"summary": "旧摘要", "scenes": [
            {"scene_id": "s1", "time_range": {"start_ms": 0, "end_ms": 10},
             "description": "旧场景"}]},
        "metadata": {"source": "platform", "recognition_status": "completed"},
    }
    resp = client.post("/api/v1/content/import",
                       json={"kind": "platform", "data": legacy})
    assert resp.status_code == 200, resp.text
    asset_id = resp.json()["asset_id"]
    with client.app.state.session_factory() as session:
        row = session.get(MediaAsset, asset_id)
        stored = json.loads(row.content_json)
        # 平台格式：库里也归一成模板形态（description / start_ms）
        assert stored["content"]["description"] == "旧摘要"
        assert "summary" not in stored["content"]
        assert stored["content"]["scenes"][0]["start_ms"] == 0
        # 内部读取路径不受影响
        assert load_content(row.content_json).content.summary == "旧摘要"
    detail = client.get(f"/api/v1/assets/{asset_id}/content").json()
    assert detail["content"]["description"] == "旧摘要"


def test_stored_template_fills_every_field(client):
    """模板字段全量：缺值 null、空列表 []（新建资产的最小上游 JSON）。"""
    minimal = {
        "schema_version": "1.0",
        "asset": {"asset_id": "movie_min", "asset_type": "movie", "title": "最小片"},
        "content": {"description": "只有一句话", "scenes": [
            {"scene_id": "s1", "start_ms": 0, "end_ms": 10}]},
    }
    resp = client.post("/api/v1/content/import",
                       json={"kind": "vision", "data": minimal})
    assert resp.status_code == 200, resp.text
    asset_id = resp.json()["asset_id"]
    with client.app.state.session_factory() as app_ctx:
        row = app_ctx.get(MediaAsset, asset_id)
        stored = json.loads(row.content_json)
    assert stored["asset"]["language"] is None
    assert stored["asset"]["video_uri"] is None
    assert stored["content"]["characters"] == []
    scene = stored["content"]["scenes"][0]
    assert scene["virtual_time"] is None and scene["location"] is None
    assert scene["character_ids"] == [] and scene["events"] == []
    assert scene["emotion"] == [] and scene["dialogues"] == []
    assert scene["audio_events"] == []


def test_shape_detection():
    assert is_upstream_shape(TEMPLATE) is True
    assert is_upstream_shape({"content": {"summary": "x", "scenes": []}}) is False
    assert is_upstream_shape({"content": {"description": "x", "scenes": []}}) is True
    assert is_upstream_shape({"content": {"description": "x", "scenes": [
        {"start_ms": 0, "end_ms": 1}]}}) is True
    assert is_upstream_shape({}) is False


def test_load_content_round_trip():
    content = load_content(json.dumps(TEMPLATE, ensure_ascii=False))
    assert content.content.summary == "整片剧情"
    assert content.content.scenes[0].scene_id == "s1"
    assert content.content.characters[0].description == "由 李四 饰演"
    # 内部形态直接可解析
    internal = parse_content({"schema_version": "1.0",
                              "asset": {"asset_id": "a", "asset_type": "movie",
                                        "title": "t"},
                              "content": {"summary": "s", "scenes": []},
                              "metadata": {"source": "platform",
                                           "recognition_status": "completed"}})
    assert internal.content.summary == "s"
