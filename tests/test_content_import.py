"""内容导入服务测试（Task 9）。

覆盖契约（brief 四用例逐字）：
- 层级与上下文落库：影片/场景父子关系 + media_context 每次导入新建
  （embedding 留空，由生产任务写入）；
- 重复导入 upsert 语义：asset_id 不变、场景不重复、context 新建、
  file:// URI 无论 video_root 与否都解析为路径部分；
- video_root 不存在 → ValueError；
- resolve_video_path 四规则（file:// 取路径部分 / 绝对路径原样 /
  相对路径 join video_root）。
"""

import pytest

from tagplatform.content_import import import_content, resolve_video_path
from tagplatform.db.models import MediaAsset, MediaContext


def test_import_creates_hierarchy_and_context(session, content_fixture):
    res = import_content(content_fixture, session, video_root=None)
    movie = session.get(MediaAsset, res.asset_id)
    assert movie.asset_type == "movie" and len(res.scene_asset_ids) == 1
    scene = session.get(MediaAsset, res.scene_asset_ids[0])
    assert scene.parent_id == res.asset_id and scene.asset_type == "scene"
    ctx = session.get(MediaContext, res.context_id)
    assert ctx.asset_id == res.asset_id and ctx.embedding is None

def test_reimport_new_context_upserts_assets(session, content_fixture):
    r1 = import_content(content_fixture, session, None)
    content_fixture.asset.video_uri = "file:///data/movie_002.mp4"
    r2 = import_content(content_fixture, session, None)
    assert r2.asset_id == r1.asset_id and r2.context_id != r1.context_id
    assert session.query(MediaAsset).filter_by(parent_id=r1.asset_id).count() == 1  # 场景不重复
    assert session.query(MediaContext).count() == 2
    # file:// URI 无论 video_root 与否都解析为路径部分（任务接口契约）
    assert session.get(MediaAsset, r1.asset_id).video_uri == "/data/movie_002.mp4"

def test_import_video_root_missing(session, content_fixture):
    with pytest.raises(ValueError, match="video_root"):
        import_content(content_fixture, session, video_root="Z:/不存在")

def test_resolve_video_path_rules(tmp_path):
    assert resolve_video_path("file:///data/a.mp4", None) == "/data/a.mp4"
    assert resolve_video_path("/abs/a.mp4", str(tmp_path)) == "/abs/a.mp4"
    assert resolve_video_path("rel/a.mp4", str(tmp_path)) == str(tmp_path / "rel" / "a.mp4")
