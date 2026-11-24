"""标签数据与标签库的级联清空原语（clear-reset Task 1）。

五操作矩阵（spec §1）中 ①②③ 的删除半径落在本模块；④⑤ 经
services.content.reset 组合调用本模块的组合原语。删除顺序 FK 子先父后：
pre_asset_tag → asset_tag → tag_quality_evaluation → tag_generation_task
→（③）tag_define → tag_catalog。

分层红线：本模块属 services 层，不得 import fastapi / sqlalchemy.orm 之外
的 web 框架；HTTP 错误映射在 orchestration 层。

事务契约：`delete_tag_data_rows` 是组合原语——只删不 commit，由组合方
（`clear_library` / content 侧 `delete_assets`）保证"每操作单事务"；
`clear_tag_data` 独立使用时自持事务（末尾一次 commit）。dry_run 与实删
共用同一计数实现（先 COUNT 后 DELETE，数字同源不漂移）。
"""

from collections.abc import Collection

from sqlalchemy import func
from sqlalchemy.orm import Session

from tagplatform.capabilities.storage.models import (
    AssetTag,
    PreAssetTag,
    TagCatalog,
    TagDefine,
    TagGenerationTask,
    TagQualityEvaluation,
)

# 九键 counts 契约（spec §3.1）：每个清空/删除操作的响应与预览共用，
# 缺省 0 不省略；media_assets（顶层行）与 scenes 不重叠。
CLEAR_COUNT_KEYS: tuple[str, ...] = (
    "pre_asset_tag",
    "asset_tag",
    "generation_tasks",
    "quality_evaluations",
    "asset_build_task",
    "media_assets",
    "scenes",
    "tag_define",
    "tag_catalog",
)


def empty_counts() -> dict[str, int]:
    """九键全 0 计数（各操作以此为底填自己的键）。"""
    return dict.fromkeys(CLEAR_COUNT_KEYS, 0)


def _scoped(query, column, asset_ids: Collection[str] | None):
    """按资产行集合过滤（None = 全库）；集合为已展开的闭包/行 id。"""
    if asset_ids is None:
        return query
    return query.filter(column.in_(asset_ids))


def _task_ids_in_scope(session: Session, asset_ids: Collection[str] | None) -> list[str]:
    q = session.query(TagGenerationTask.task_id)
    return [row[0] for row in _scoped(q, TagGenerationTask.asset_id, asset_ids)]


def tag_data_counts(session: Session,
                    asset_ids: Collection[str] | None) -> dict[str, int]:
    """数四表的待删行数（dry_run 预览与实删响应的同一计数实现）。"""
    counts = empty_counts()
    counts["pre_asset_tag"] = _scoped(
        session.query(func.count(PreAssetTag.id)), PreAssetTag.asset_id,
        asset_ids).scalar() or 0
    counts["asset_tag"] = _scoped(
        session.query(func.count(AssetTag.id)), AssetTag.asset_id,
        asset_ids).scalar() or 0
    counts["generation_tasks"] = _scoped(
        session.query(func.count(TagGenerationTask.task_id)),
        TagGenerationTask.asset_id, asset_ids).scalar() or 0
    task_ids = _task_ids_in_scope(session, asset_ids)
    counts["quality_evaluations"] = (
        session.query(func.count(TagQualityEvaluation.task_id))
        .filter(TagQualityEvaluation.task_id.in_(task_ids)).scalar() or 0)
    return counts


def delete_tag_data_rows(session: Session,
                         asset_ids: Collection[str] | None) -> dict[str, int]:
    """按 FK 子先父后删除四表行（不 commit——组合方保证单事务）。

    queued 任务行在删除半径内（spec §3.3：删除即取消，不设防）。
    """
    counts = tag_data_counts(session, asset_ids)
    task_ids = _task_ids_in_scope(session, asset_ids)
    _scoped(session.query(PreAssetTag), PreAssetTag.asset_id,
            asset_ids).delete(synchronize_session=False)
    _scoped(session.query(AssetTag), AssetTag.asset_id,
            asset_ids).delete(synchronize_session=False)
    session.query(TagQualityEvaluation).filter(
        TagQualityEvaluation.task_id.in_(task_ids)).delete(
        synchronize_session=False)
    _scoped(session.query(TagGenerationTask), TagGenerationTask.asset_id,
            asset_ids).delete(synchronize_session=False)
    return counts


def clear_tag_data(session: Session,
                   asset_ids: Collection[str] | None = None,
                   *, dry_run: bool = False) -> dict[str, int]:
    """①② 主入口：清空（作用域）标签数据四表；dry_run 只数不删不 commit。"""
    if dry_run:
        return tag_data_counts(session, asset_ids)
    counts = delete_tag_data_rows(session, asset_ids)
    session.commit()
    return counts


def clear_library(session: Session, *, dry_run: bool = False) -> dict[str, int]:
    """③ 主入口：清空标签库（tag_define + tag_catalog）+ 全部标签数据。

    外键决定清库必须先清全部挂载（pre_asset_tag.tag_id/candidate_dir_id、
    asset_tag.tag_id 指向两库表）。tag_import_task / tag_change_log 不在
    半径内（spec §2 反目标）。单事务：末尾一次 commit。
    """
    counts = tag_data_counts(session, None)
    counts["tag_define"] = (
        session.query(func.count(TagDefine.tag_id)).scalar() or 0)
    counts["tag_catalog"] = (
        session.query(func.count(TagCatalog.dir_id)).scalar() or 0)
    if dry_run:
        return counts
    delete_tag_data_rows(session, None)
    session.query(TagDefine).delete(synchronize_session=False)
    session.query(TagCatalog).delete(synchronize_session=False)
    session.commit()
    return counts
