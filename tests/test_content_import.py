"""内容导入服务测试（v2 合并语义）。

覆盖契约（brief 用例逐字 + 沿 v1 已钉住的拦截规则）：
- 导入后 media_asset.content_json/source 落库（影片级行直写，场景行
  content_json 保持 NULL——内容挂在影片上，doc §3.1）；
- media_context 表已不存在（v2 合并进 media_asset）；
- 重复导入**覆盖** content_json/source（重识别=覆盖，不做历史堆叠）；
- scene 子资产照旧：asset_id = f"{movie}::{scene_id}"、upsert 不重复插行；
- ImportResult 无 context_id 字段（context 概念退役）；
- video_root 不存在 → ValueError；
- parent_id 指向不存在资产 → ValueError（写库前拦截，零副作用）；
- resolve_video_path 四规则（file:// 取路径部分 / 绝对路径原样 /
  相对路径 join video_root）。
"""

import json

import pytest

from tagplatform.content_import import import_content, resolve_video_path
from tagplatform.db.models import Base, MediaAsset


def test_import_writes_content_json_and_source(session, content_fixture):
    """导入后影片级 MediaAsset 行带上 content_json 与 source；场景行不带。"""
    res = import_content(content_fixture, session, video_root=None)
    movie = session.get(MediaAsset, res.asset_id)
    assert movie.asset_type == "movie" and len(res.scene_asset_ids) == 1
    assert movie.content_json is not None
    stored = json.loads(movie.content_json)
    assert stored["content"]["summary"] == "影片整体剧情摘要"
    assert movie.source == "multimodal_understanding"
    scene = session.get(MediaAsset, res.scene_asset_ids[0])
    assert scene.parent_id == res.asset_id and scene.asset_type == "scene"
    assert scene.content_json is None  # 内容只落影片级，场景行是层级占位


def test_media_context_table_gone():
    """v2 合并：media_context 表从 ORM metadata 中消失。"""
    assert "media_context" not in Base.metadata.tables


def test_import_result_has_no_context_id(session, content_fixture):
    """ImportResult 退役 context_id：字段只剩 asset_id 与 scene_asset_ids。"""
    res = import_content(content_fixture, session, video_root=None)
    assert not hasattr(res, "context_id")
    assert res.asset_id and res.scene_asset_ids == [f"{res.asset_id}::scene_001"]


def test_reimport_overwrites_content_json(session, content_fixture):
    """重复导入：content_json/source 直接覆盖，资产与场景不重复插行。"""
    r1 = import_content(content_fixture, session, None)
    first_json = session.get(MediaAsset, r1.asset_id).content_json

    content_fixture.content.summary = "第二版摘要"
    content_fixture.metadata.source = "multimodal_understanding_v2"
    content_fixture.asset.video_uri = "file:///data/movie_002.mp4"
    r2 = import_content(content_fixture, session, None)

    assert r2.asset_id == r1.asset_id
    movie = session.get(MediaAsset, r1.asset_id)
    assert movie.content_json != first_json
    stored = json.loads(movie.content_json)
    assert stored["content"]["summary"] == "第二版摘要"      # 覆盖，不留旧版
    assert movie.source == "multimodal_understanding_v2"    # source 一并覆盖
    assert session.query(MediaAsset).filter_by(parent_id=r1.asset_id).count() == 1  # 场景不重复
    assert session.get(MediaAsset, r1.asset_id).video_uri == "/data/movie_002.mp4"  # file:// 解析为路径部分


def test_import_video_root_missing(session, content_fixture):
    with pytest.raises(ValueError, match="video_root"):
        import_content(content_fixture, session, video_root="Z:/不存在")

def test_import_parent_asset_missing(session, content_fixture):
    """parent_id 指向不存在资产 → 写库前 ValueError，零副作用。

    MediaAsset.parent_id 有外键约束，非法值原先在影片行 flush 时抛未捕获
    IntegrityError（客户端拿裸 500）；于任何数据库写入之前拦截。
    """
    content_fixture.asset.parent_id = "asset_none"
    with pytest.raises(ValueError, match="parent 资产不存在"):
        import_content(content_fixture, session, video_root=None)
    assert session.query(MediaAsset).count() == 0

def test_resolve_video_path_rules(tmp_path):
    assert resolve_video_path("file:///data/a.mp4", None) == "/data/a.mp4"
    assert resolve_video_path("/abs/a.mp4", str(tmp_path)) == "/abs/a.mp4"
    assert resolve_video_path("rel/a.mp4", str(tmp_path)) == str(tmp_path / "rel" / "a.mp4")
