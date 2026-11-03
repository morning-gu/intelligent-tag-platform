"""内容 JSON 存储形态测试（2026-11：库里存原文，读时归一化）。

覆盖：
- 上游模板形态入库后，`content_json` 原文与提交的一致（键不改名、不裁剪）；
- 读取时 `load_content` 能还原成平台内部 ContentJSON（API 响应形状不变）；
- 历史内部形态（summary/time_range）继续可读，不需要迁移；
- 判别函数对两种形态与空场景的判定。
"""

import json

from tagplatform.content_io import is_upstream_shape, load_content
from tagplatform.db.models import MediaContext
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


def test_template_is_stored_verbatim(client):
    """经 /content/import 提交的模板 JSON，库里逐字保存（含键名与空值）。"""
    resp = client.post("/api/v1/content/import",
                       json={"kind": "vision", "data": TEMPLATE})
    assert resp.status_code == 200, resp.text
    context_id = resp.json()["context_id"]

    with client.app.state.session_factory() as session:
        row = session.get(MediaContext, context_id)
        stored = json.loads(row.content_json)

    assert stored == TEMPLATE                       # 一字不差
    assert "start_ms" in stored["content"]["scenes"][0]
    assert "summary" not in stored["content"]        # 没有被改写成内部形态
    assert "time_range" not in stored["content"]["scenes"][0]


def test_api_reads_back_internal_shape(client):
    """读端点仍返回平台内部形状（前端 dist 不改也能用）。"""
    resp = client.post("/api/v1/content/import",
                       json={"kind": "vision", "data": TEMPLATE})
    context_id = resp.json()["context_id"]
    detail = client.get(f"/api/v1/assets/movie_tt1/contexts/{context_id}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["content"]["summary"] == "整片剧情"
    scene = body["content"]["scenes"][0]
    assert scene["time_range"] == {"start_ms": 0, "end_ms": 1000}
    assert scene["emotions"] == ["紧张压迫"]
    assert scene["dialogues"][0]["speaker_name"] == "张三"


def test_legacy_internal_shape_still_readable(client):
    """2026-11 之前入库的内部形态继续可读（自动判别，不需迁移）。"""
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
    context_id = resp.json()["context_id"]
    with client.app.state.session_factory() as session:
        row = session.get(MediaContext, context_id)
        # 平台格式：库内存的就是内部形态（原样）
        assert json.loads(row.content_json) == legacy
        assert load_content(row.content_json).content.summary == "旧摘要"


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
