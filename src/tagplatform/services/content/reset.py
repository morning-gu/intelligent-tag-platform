"""内容资产的级联删除原语与守卫计数（clear-reset Task 1）。

五操作矩阵（spec §1）中 ④⑤ 的删除半径：闭包（顶层 + parent_id 后代）
内的标签数据（组合 tags.reset 的原语，不中途 commit）+ asset_build_task
+ media_asset 后代先删。分层红线：本模块属 services 层，不得 import
fastapi；跨域 import services.tags.reset 有先例（content/ingest →
tags/library）。

守卫口径（spec §3.3）：active_task_counts 只数 **running** 的生成/构建
任务（queued 不计——在删除半径内，删除即取消）与 **processing** 的导入
任务（恒全库）；是否挡 409 由 orchestration 层按操作裁量。
"""

from collections.abc import Collection

from sqlalchemy import func
from sqlalchemy.orm import Session

from tagplatform.capabilities.storage.models import (
    AssetBuildTask,
    MediaAsset,
    TagGenerationTask,
    TagImportTask,
)
from tagplatform.services.tags.reset import (
    empty_counts,
    tag_data_counts,
    delete_tag_data_rows,
)


def asset_closure(session: Session, asset_id: str) -> list[str] | None:
    """顶层资产行 + parent_id 递归的全部后代行（BFS，父在前）；未知 → None。

    返回序即删除的逆序基础：reverse 后后代先于父级，满足 parent_id
    自引用外键的子先父后。
    """
    row = session.get(MediaAsset, asset_id)
    if row is None:
        return None
    closure = [asset_id]
    seen = {asset_id}
    frontier = [asset_id]
    while frontier:
        children = [r[0] for r in session.query(MediaAsset.asset_id).filter(
            MediaAsset.parent_id.in_(frontier))]
        children = [c for c in children if c not in seen]  # 环保护
        closure.extend(children)
        seen.update(children)
        frontier = children
    return closure


def _expand(session: Session,
            asset_ids: Collection[str] | None) -> list[str] | None:
    """顶层 id 集合 → 闭包行集合（None 保留为全库语义）。"""
    if asset_ids is None:
        return None
    rows: list[str] = []
    for asset_id in asset_ids:
        if session.get(MediaAsset, asset_id) is not None:
            rows.extend(asset_closure(session, asset_id) or [])
    return rows


def _asset_delete_counts(session: Session,
                         closure: Collection[str] | None) -> dict[str, int]:
    """④⑤ 的计数（dry_run 与实删同源）：tag 四键 + build + media/scenes。"""
    counts = tag_data_counts(session, closure)
    if closure is None:
        rows = session.query(MediaAsset.asset_type).all()
        build_q = session.query(func.count(AssetBuildTask.task_id))
    else:
        rows = session.query(MediaAsset.asset_type).filter(
            MediaAsset.asset_id.in_(closure)).all()
        build_q = session.query(func.count(AssetBuildTask.task_id)).filter(
            AssetBuildTask.asset_id.in_(closure))
    counts["asset_build_task"] = build_q.scalar() or 0
    types = [r[0] for r in rows]
    counts["media_assets"] = sum(1 for t in types if t != "scene")
    counts["scenes"] = sum(1 for t in types if t == "scene")
    return counts


def _delete_order(session: Session,
                  closure: Collection[str] | None) -> list[str]:
    """删除序：作用域内行按"叶子在前"重排（闭包来自多个根时父级可能后出）。

    闭包情形：BFS 序父在前 → reverse 即后代先。全库情形：以 parent 不在
    作用域内的行为根做 BFS，再 reverse。
    """
    if closure is None:
        scope_ids = [r[0] for r in session.query(MediaAsset.asset_id)]
        in_scope = set(scope_ids)
        roots = [r[0] for r in session.query(MediaAsset.asset_id).filter(
            MediaAsset.parent_id.is_(None)
            | ~MediaAsset.parent_id.in_(in_scope))]
    else:
        in_scope = set(closure)
        roots = [i for i in closure
                 if _parent_of(session, i) not in in_scope]
    order: list[str] = []
    seen: set[str] = set()
    frontier = roots
    while frontier:
        frontier = [i for i in frontier if i not in seen]
        if not frontier:
            break
        order.extend(frontier)
        seen.update(frontier)
        frontier = [r[0] for r in session.query(MediaAsset.asset_id).filter(
            MediaAsset.parent_id.in_(frontier))]
    return list(reversed(order))


def _parent_of(session: Session, asset_id: str) -> str | None:
    row = session.get(MediaAsset, asset_id)
    return row.parent_id if row is not None else None


def delete_assets(session: Session,
                  asset_ids: Collection[str] | None = None,
                  *, dry_run: bool = False) -> dict[str, int]:
    """④⑤ 主入口：删除（作用域）资产闭包及其全部关联行。

    作用域 = asset_ids（顶层 id 集合，None=全库）经闭包展开。单事务：
    tag 数据 + asset_build_task + media_asset（后代先）末尾一次 commit；
    dry_run 只数不删不 commit。视频源文件一律不碰（spec §2 反目标）。
    """
    closure = _expand(session, asset_ids)
    counts = _asset_delete_counts(session, closure)
    if dry_run:
        return counts
    if closure is not None and closure == []:
        session.commit()
        return counts
    delete_tag_data_rows(session, closure)
    if closure is None:
        session.query(AssetBuildTask).delete(synchronize_session=False)
    else:
        session.query(AssetBuildTask).filter(
            AssetBuildTask.asset_id.in_(closure)).delete(
            synchronize_session=False)
    for asset_id in _delete_order(session, closure):
        session.query(MediaAsset).filter(
            MediaAsset.asset_id == asset_id).delete(
            synchronize_session=False)
    session.commit()
    return counts


def active_task_counts(session: Session,
                       asset_ids: Collection[str] | None = None
                       ) -> dict[str, int]:
    """守卫计数：running 生成/构建（按资产行集合过滤，None=全库）+
    processing 导入（恒全库）。queued 不计（spec §3.3）。
    """
    def _running(status, model_pk, status_col, filter_col):
        q = session.query(func.count(model_pk)).filter(status_col == status)
        if asset_ids is not None:
            q = q.filter(filter_col.in_(asset_ids))
        return q.scalar() or 0

    return {
        "running_generation": _running("running", TagGenerationTask.task_id,
                                       TagGenerationTask.status,
                                       TagGenerationTask.asset_id),
        "running_build": _running("running", AssetBuildTask.task_id,
                                  AssetBuildTask.status,
                                  AssetBuildTask.asset_id),
        "processing_import": session.query(func.count(TagImportTask.task_id))
        .filter(TagImportTask.status == "processing").scalar() or 0,
    }
