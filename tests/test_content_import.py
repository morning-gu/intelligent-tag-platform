"""内容导入服务测试（Task 9）。

覆盖契约（brief 四用例逐字）：
- 层级与内容落库：影片/场景父子关系 + 内容整块覆盖在影片行
  （content_json / source / updated_at）；
- 重复导入 upsert 语义：asset_id 不变、场景不重复、内容覆盖、
  file:// URI 无论 video_root 与否都解析为路径部分；
- video_root 不存在 → ValueError；
- parent_id 指向不存在资产 → ValueError（修复 1：写库前拦截，杜绝外键
  IntegrityError 逃逸为 500）；
- resolve_video_path 四规则（file:// 取路径部分 / 绝对路径原样 /
  相对路径 join video_root）。
"""

import json

import pytest

from tagplatform.content_import import import_content, resolve_video_path
from tagplatform.db.models import MediaAsset


def test_import_creates_hierarchy(session, content_fixture):
    res = import_content(content_fixture, session, video_root=None)
    movie = session.get(MediaAsset, res.asset_id)
    assert movie.asset_type == "movie" and len(res.scene_asset_ids) == 1
    assert movie.content_json is not None  # v2：内容就落这一行，没有 context 行
    scene = session.get(MediaAsset, res.scene_asset_ids[0])
    assert scene.parent_id == res.asset_id and scene.asset_type == "scene"


def test_content_lands_on_asset_row(session, content_fixture):
    """内容写在 media_asset 行上：content_json / source / updated_at 齐全。"""
    res = import_content(content_fixture, session, None)
    movie = session.get(MediaAsset, res.asset_id)
    assert movie.content_json is not None
    assert movie.source == content_fixture.metadata.source
    assert movie.updated_at is not None
    stored = json.loads(movie.content_json)
    assert stored["asset"]["asset_id"] == res.asset_id
    # 场景行是物理切片：标题 {片名}_{scene_id}，不存内容
    scene_id = stored["content"]["scenes"][0]["scene_id"]
    scene = session.get(MediaAsset, f"{res.asset_id}::{scene_id}")
    assert scene.title == f"{content_fixture.asset.title}_{scene_id}"
    assert scene.content_json is None and scene.source is None


def test_generated_asset_id_written_back(session, content_fixture):
    """没给 asset_id：生成一个，并回写进 content_json，两边对得上。"""
    content_fixture.asset.asset_id = None
    res = import_content(content_fixture, session, None)
    assert res.asset_id.startswith("asset_")
    stored = json.loads(session.get(MediaAsset, res.asset_id).content_json)
    assert stored["asset"]["asset_id"] == res.asset_id


def test_scene_rows_pruned_when_gone(session, content_fixture):
    """重新导入后不再出现的场景行被删掉，不留孤儿。"""
    r1 = import_content(content_fixture, session, None)
    content_fixture.content.scenes.clear()
    import_content(content_fixture, session, None)
    assert session.get(MediaAsset, r1.scene_asset_ids[0]) is None


def test_prune_removes_tags_of_dropped_scene(session, content_fixture):
    """删场景行前先删其待审标签，否则外键会挡住。"""
    from tagplatform.db.models import PreAssetTag

    r1 = import_content(content_fixture, session, None)
    scene_id = r1.scene_asset_ids[0]
    session.add(PreAssetTag(id=7, asset_id=scene_id, target_type="content",
                            target_id=scene_id, origin="open",
                            candidate_name="夜景", review_status="pending"))
    session.commit()

    content_fixture.content.scenes.clear()
    import_content(content_fixture, session, None)

    assert session.get(PreAssetTag, 7) is None
    assert session.get(MediaAsset, scene_id) is None


def test_reimport_upserts_assets(session, content_fixture):
    r1 = import_content(content_fixture, session, None)
    content_fixture.asset.video_uri = "file:///data/movie_002.mp4"
    r2 = import_content(content_fixture, session, None)
    assert r2.asset_id == r1.asset_id and r2.scene_asset_ids == r1.scene_asset_ids
    assert session.query(MediaAsset).filter_by(parent_id=r1.asset_id).count() == 1  # 场景不重复
    # file:// URI 无论 video_root 与否都解析为路径部分（任务接口契约）
    assert session.get(MediaAsset, r1.asset_id).video_uri == "/data/movie_002.mp4"

def test_import_video_root_missing(session, content_fixture):
    with pytest.raises(ValueError, match="video_root"):
        import_content(content_fixture, session, video_root="Z:/不存在")

def test_import_parent_asset_missing(session, content_fixture):
    """修复 1：parent_id 指向不存在资产 → 写库前 ValueError，零副作用。

    MediaAsset.parent_id 有外键约束，非法值原先在影片行 flush 时抛未捕获
    IntegrityError（客户端拿裸 500）；现于任何数据库写入之前拦截。
    """
    content_fixture.asset.parent_id = "asset_none"
    with pytest.raises(ValueError, match="parent 资产不存在"):
        import_content(content_fixture, session, video_root=None)
    assert session.query(MediaAsset).count() == 0

def test_resolve_video_path_rules(tmp_path):
    assert resolve_video_path("file:///data/a.mp4", None) == "/data/a.mp4"
    assert resolve_video_path("/abs/a.mp4", str(tmp_path)) == "/abs/a.mp4"
    assert resolve_video_path("rel/a.mp4", str(tmp_path)) == str(tmp_path / "rel" / "a.mp4")
