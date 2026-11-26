"""核心表 ORM 模型与数据库约束测试。

覆盖契约：
- `AssetTag` 的 `ux_asset_tag` 唯一键真实触发 IntegrityError；同一标签不同
  tag_name（泛化值）允许共存；
- `PreAssetTag` 两个部分唯一索引、外键与 target_type CHECK 真实触发 IntegrityError；
- `TagCatalog` / `MediaAsset` / `AssetBuildTask` 枚举 CHECK 约束真实拦截非法值。
"""

import pytest
from sqlalchemy.exc import IntegrityError

from tagplatform.capabilities.storage.models import (
    AssetTag,
    MediaAsset,
    PreAssetTag,
    TagCatalog,
    TagDefine,
)


def _add_library(session, dir_id="dir_1", tag_id="t1"):
    """补齐标签库父行：目录 → 标签定义。"""
    session.add(TagCatalog(dir_id=dir_id, dir_name="类型", dir_type="dir_static",
                           description="d", applicable_scope="content"))
    session.add(TagDefine(tag_id=tag_id, tag_name="悬疑", tag_type="val_fixed",
                          description="d", dir_id=dir_id))


def test_asset_tag_unique(session):
    """ux_asset_tag：同资产同实体同标签同展示值只能一行。"""
    session.add(MediaAsset(asset_id="a", asset_type="movie", title="测试影片"))
    _add_library(session)
    session.flush()
    session.add_all([
        AssetTag(asset_id="a", target_id="a", tag_id="t1", tag_name="悬疑"),
        AssetTag(asset_id="a", target_id="a", tag_id="t1", tag_name="悬疑"),
    ])
    with pytest.raises(IntegrityError):
        session.commit()


def test_asset_tag_generalized_name_allowed(session):
    """唯一键含 tag_name：同一标签挂不同实际值（泛化值预留）允许共存。"""
    session.add(MediaAsset(asset_id="a", asset_type="movie", title="测试影片"))
    _add_library(session)
    session.flush()
    session.add_all([
        AssetTag(asset_id="a", target_id="a", tag_id="t1", tag_name="张三"),
        AssetTag(asset_id="a", target_id="a", tag_id="t1", tag_name="李四"),
    ])
    session.commit()
    assert session.query(AssetTag).count() == 2


def test_pre_asset_tag_partial_unique(session):
    # 同资产同实体不重复挂同一库内标签（ux_pre_tag_catalog）
    session.add(MediaAsset(asset_id="a", asset_type="movie", title="测试影片"))
    _add_library(session)
    session.flush()
    session.add_all([
        PreAssetTag(asset_id="a", target_id="a", tag_id="t1", origin="closed"),
        PreAssetTag(asset_id="a", target_id="a", tag_id="t1", origin="closed"),
    ])
    with pytest.raises(IntegrityError):
        session.commit()


def test_pre_asset_tag_candidate_unique(session):
    # 同资产同实体不重复收录同一候选名（ux_pre_tag_candidate）
    session.add(MediaAsset(asset_id="a", asset_type="movie", title="测试影片"))
    session.flush()
    session.add_all([
        PreAssetTag(asset_id="a", target_id="a", candidate_name="新词",
                    origin="open"),
        PreAssetTag(asset_id="a", target_id="a", candidate_name="新词",
                    origin="open"),
    ])
    with pytest.raises(IntegrityError):
        session.commit()


def test_pre_asset_tag_foreign_keys(session):
    """tag_id 指 tag_define、candidate_dir_id 指 tag_catalog(dir_id)。"""
    session.add(MediaAsset(asset_id="a", asset_type="movie", title="测试影片"))
    _add_library(session)
    session.flush()
    session.add(PreAssetTag(asset_id="a", target_id="a", tag_id="nope",
                            origin="closed"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_pre_asset_tag_target_type_check(session):
    session.add(MediaAsset(asset_id="a", asset_type="movie", title="测试影片"))
    session.flush()
    session.add(PreAssetTag(asset_id="a", target_id="a", target_type="episode",
                            origin="closed"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_tag_catalog_dir_type_enum(session):
    """ck_tag_catalog_dir_type：dir_asset 放行，非法值仍被 CHECK 拦截。"""
    session.add(TagCatalog(dir_id="dir_asset_1", dir_name="演员标签",
                           dir_type="dir_asset", description="d",
                           applicable_scope="character"))
    session.commit()
    assert session.get(TagCatalog, "dir_asset_1").dir_type == "dir_asset"
    session.add(TagCatalog(dir_id="dir_bad", dir_name="坏目录",
                           dir_type="dir_hybrid", description="d"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_media_asset_and_build_task_checks_reject_invalid_enums(session):
    """media_asset.asset_type 与 asset_build_task.status 的 CHECK 约束真实拦截非法值。"""
    from tagplatform.capabilities.storage.models import AssetBuildTask

    session.add(MediaAsset(asset_id="a_bad", asset_type="film", title="x"))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()

    session.add(MediaAsset(asset_id="a_ok", asset_type="movie", title="x"))
    session.flush()
    session.add(AssetBuildTask(task_id="t_bad", asset_id="a_ok", movie_id="m", status="done"))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()

    session.add(MediaAsset(asset_id="a_ok", asset_type="movie", title="x"))
    session.flush()
    session.add(AssetBuildTask(task_id="t_ok", asset_id="a_ok", movie_id="m", status="queued"))
    session.commit()
