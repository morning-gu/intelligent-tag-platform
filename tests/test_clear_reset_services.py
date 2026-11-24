"""clear-reset Task 1 服务层测试：级联删除原语与守卫计数。

覆盖 spec §1/§3.2：五操作的删除半径（dry_run 与实删同一计数实现）、
FK 子先父后删除顺序（全程无 IntegrityError）、queued 任务在删除半径内、
③ 后导入批次与审计日志幸存、闭包 BFS 多级 parent 链、active_task_counts
守卫口径（running 计、queued 不计、processing 导入恒全库）。
"""

from tagplatform.capabilities.storage.models import (
    AssetBuildTask,
    AssetTag,
    MediaAsset,
    PreAssetTag,
    TagCatalog,
    TagChangeLog,
    TagDefine,
    TagGenerationTask,
    TagImportTask,
    TagQualityEvaluation,
)
from tagplatform.services.content.reset import (
    active_task_counts,
    asset_closure,
    delete_assets,
)
from tagplatform.services.tags.reset import clear_library, clear_tag_data


def _seed(s):
    """m1(movie)+s1/s2(scene, parent=m1)+m2(movie)；m1 全链数据，m2 裸行。

    session fixture 预置库：tag_catalog 5 行、tag_define 7 行
    （tag_action/tag_drama/tag_epic/tag_revenge/tag_nonlinear/tag_twist/tag_hero）。
    """
    s.add_all([
        MediaAsset(asset_id="m1", asset_type="movie", title="片一"),
        MediaAsset(asset_id="m1-s1", asset_type="scene", parent_id="m1", title="场一"),
        MediaAsset(asset_id="m1-s2", asset_type="scene", parent_id="m1", title="场二"),
        MediaAsset(asset_id="m2", asset_type="movie", title="片二"),
    ])
    task = TagGenerationTask(task_id="g1", asset_id="m1", mode="closed",
                             status="succeeded")
    s.add(task)
    s.flush()
    s.add(TagQualityEvaluation(task_id="g1", status="completed", model="m",
                               prompt_version="v", labels=[], scores={},
                               frame_paths={}))
    s.add_all([
        PreAssetTag(asset_id="m1", target_type="content", target_id="m1",
                    tag_id="tag_action", origin="closed",
                    review_status="approved"),
        PreAssetTag(asset_id="m1", target_type="content", target_id="m1",
                    tag_id="tag_drama", origin="closed",
                    review_status="pending"),
        AssetTag(asset_id="m1", target_type="content", target_id="m1",
                 tag_id="tag_action", tag_name="动作"),
        AssetBuildTask(task_id="b1", asset_id="m1", movie_id="tt1",
                       status="succeeded"),
    ])
    s.commit()


def test_clear_tag_data_all_wipes_four_tables_keeps_library_and_assets(session):
    _seed(session)
    result = clear_tag_data(session)
    assert result == {
        "pre_asset_tag": 2, "asset_tag": 1, "generation_tasks": 1,
        "quality_evaluations": 1, "asset_build_task": 0, "media_assets": 0,
        "scenes": 0, "tag_define": 0, "tag_catalog": 0,
    }
    for model in (PreAssetTag, AssetTag, TagGenerationTask,
                  TagQualityEvaluation):
        assert session.query(model).count() == 0
    assert session.query(MediaAsset).count() == 4
    assert session.query(TagDefine).count() == 7
    assert session.query(TagCatalog).count() == 5


def test_clear_tag_data_scoped_to_asset_closure(session):
    _seed(session)
    # m2 另挂一条 pre 行：验证作用域只吃 m1 闭包、m2 幸存
    session.add(PreAssetTag(asset_id="m2", target_type="content",
                            target_id="m2", tag_id="tag_epic", origin="open",
                            review_status="pending"))
    session.commit()
    result = clear_tag_data(session, ["m1"])
    assert result["pre_asset_tag"] == 2
    assert result["generation_tasks"] == 1
    assert [r.asset_id for r in session.query(PreAssetTag).all()] == ["m2"]
    assert session.query(AssetTag).count() == 0
    assert session.query(MediaAsset).count() == 4
    assert session.query(TagDefine).count() == 7


def test_clear_tag_data_dry_run_counts_without_deleting(session):
    _seed(session)
    dry = clear_tag_data(session, ["m1"], dry_run=True)
    assert dry["pre_asset_tag"] == 2
    assert dry["asset_tag"] == 1
    assert session.query(PreAssetTag).count() == 2  # 只数不删
    assert session.query(TagGenerationTask).count() == 1
    wet = clear_tag_data(session, ["m1"])
    assert wet == dry  # dry_run 与实删同一计数实现，逐键相等
    assert session.query(PreAssetTag).count() == 0


def test_queued_task_rows_inside_delete_radius(session):
    _seed(session)
    session.add(TagGenerationTask(task_id="g-q", asset_id="m2", mode="closed",
                                  status="queued"))
    session.commit()
    result = clear_tag_data(session, ["m2"])
    assert result["generation_tasks"] == 1
    assert session.query(TagGenerationTask).filter_by(task_id="g-q").count() == 0
    assert session.get(TagGenerationTask, "g1") is not None  # m1 的任务幸存


def test_clear_library_wipes_all_keeps_imports_and_changelog(session):
    _seed(session)
    session.add_all([
        TagImportTask(task_id="imp-ok", file_name="f.xlsx", status="completed"),
        TagChangeLog(tag_id="tag_action", action="create"),
    ])
    session.commit()
    result = clear_library(session)
    assert result["tag_catalog"] == 5
    assert result["tag_define"] == 7
    assert result["pre_asset_tag"] == 2
    assert result["generation_tasks"] == 1
    assert session.query(TagCatalog).count() == 0
    assert session.query(TagDefine).count() == 0
    for model in (PreAssetTag, AssetTag, TagGenerationTask,
                  TagQualityEvaluation):
        assert session.query(model).count() == 0
    assert session.query(TagImportTask).count() == 1  # 批次幸存
    assert session.query(TagChangeLog).count() == 1  # 审计日志幸存
    assert session.query(MediaAsset).count() == 4  # 资产幸存


def test_delete_assets_scoped_deletes_closure_and_related(session):
    _seed(session)
    result = delete_assets(session, ["m1"])
    assert result == {
        "pre_asset_tag": 2, "asset_tag": 1, "generation_tasks": 1,
        "quality_evaluations": 1, "asset_build_task": 1, "media_assets": 1,
        "scenes": 2, "tag_define": 0, "tag_catalog": 0,
    }
    for asset_id in ("m1", "m1-s1", "m1-s2"):
        assert session.get(MediaAsset, asset_id) is None
    assert session.get(MediaAsset, "m2") is not None
    assert session.query(AssetBuildTask).count() == 0


def test_delete_assets_all_keeps_library(session):
    _seed(session)
    result = delete_assets(session)
    assert result["media_assets"] == 2
    assert result["scenes"] == 2
    assert result["asset_build_task"] == 1
    assert result["pre_asset_tag"] == 2
    assert session.query(MediaAsset).count() == 0
    assert session.query(AssetBuildTask).count() == 0
    for model in (PreAssetTag, AssetTag, TagGenerationTask,
                  TagQualityEvaluation):
        assert session.query(model).count() == 0
    assert session.query(TagDefine).count() == 7
    assert session.query(TagCatalog).count() == 5


def test_asset_closure_walks_deep_parent_chain(session):
    session.add_all([
        MediaAsset(asset_id="a", asset_type="movie", title="A"),
        MediaAsset(asset_id="b", asset_type="scene", parent_id="a", title="B"),
        MediaAsset(asset_id="c", asset_type="scene", parent_id="b", title="C"),
    ])
    session.commit()
    assert asset_closure(session, "a") == ["a", "b", "c"]
    delete_assets(session, ["a"])
    for asset_id in ("a", "b", "c"):
        assert session.get(MediaAsset, asset_id) is None


def test_asset_closure_unknown_returns_none(session):
    assert asset_closure(session, "nope") is None


def test_active_task_counts_scopes_and_ignores_queued(session):
    _seed(session)
    session.add_all([
        TagGenerationTask(task_id="g-run", asset_id="m1", mode="closed",
                          status="running"),
        TagGenerationTask(task_id="g-queue", asset_id="m1", mode="closed",
                          status="queued"),
        AssetBuildTask(task_id="b-run", asset_id="m2", movie_id="tt2",
                       status="running"),
        TagImportTask(task_id="imp-run", file_name="x.xlsx",
                      status="processing"),
    ])
    session.commit()
    assert active_task_counts(session, ["m1"]) == {
        "running_generation": 1, "running_build": 0, "processing_import": 1,
    }
    assert active_task_counts(session) == {
        "running_generation": 1, "running_build": 1, "processing_import": 1,
    }
    assert active_task_counts(session, ["m2"]) == {
        "running_generation": 0, "running_build": 1, "processing_import": 1,
    }
