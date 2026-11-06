"""`/api/v1/import/tasks` 路由：导入任务列表、详情、导出与撤销。

薄路由层：查询 tag_import_task → 响应模型；详情端点负责把任务记录里的
result_summary JSON（治理流水线写入）解析为结构化摘要（issues 为明细
列表），未知 task_id → 404。

v1 的 /api/v1/import/batches 前缀与 batch_id 字段随表结构 v2 对照表退役
（import_batch → tag_import_task，doc §2.1/§3.7）。撤销端点照 v1 语义
迁移到双表：批内新建目录/标签经 tag_change_log（action='import'，
after_json.task_id 反查，kind 区分 dir/tag）定位，被 asset_tag 挂载、
pre_asset_tag 待审核候选（v1 单表时二者同表，引用检查随之双表完整映射）
或被他批子节点（标签/子目录）引用时 409，复活行按 before_json 还原。

export 端点（GET /{task_id}/export）与 tags.py 共用 `_build_export_archive`
（Task 5 落地）：批内标签行 = 叶子，祖先路径 = tag_catalog 链，zip 格式
逐字沿 v1。
"""

import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from tagplatform.api import get_session
from tagplatform.api.routers.tags import ImportSummary, _build_export_archive
from tagplatform.db.models import (
    AssetTag,
    PreAssetTag,
    TagCatalog,
    TagChangeLog,
    TagDefine,
    TagImportTask,
)

router = APIRouter(prefix="/api/v1/import/tasks")


class ImportTaskOut(BaseModel):
    """导入任务列表项（v1 BatchOut 的 task_id 形状）。"""

    task_id: str
    file_name: str
    status: str
    created_at: datetime | None = None


class ImportTaskDetailOut(ImportTaskOut):
    """导入任务详情：含 result_summary 解析后的结果摘要（无/损坏时为 None）。"""

    summary: ImportSummary | None = None


def _batch_entries(task_id: str, session: Session) -> list[tuple[TagChangeLog, dict]]:
    """反查本批审计行：[(change_log, 解析后的 after_json)]。

    扫描 action='import' 的变更日志、按 after_json.task_id 匹配（治理
    流水线写入的反查键，替代 v1 的 batch_id）；after_json 损坏行跳过。
    """
    entries: list[tuple[TagChangeLog, dict]] = []
    for log in session.query(TagChangeLog).filter(
        TagChangeLog.action == "import",
    ).all():
        if not log.after_json:
            continue
        try:
            after = json.loads(log.after_json)
        except json.JSONDecodeError:
            continue
        if after.get("task_id") == task_id:
            entries.append((log, after))
    return entries


def _batch_tag_changes(task_id: str, session: Session) -> dict[str, str | None]:
    """本批标签行的 {tag_id: before_json}（kind=dir 的目录行不含）。

    before_json 非空 = 该行是复活既有行（撤销时还原状态而非删除）。
    """
    changes: dict[str, str | None] = {}
    for log, after in _batch_entries(task_id, session):
        if after.get("kind", "tag") == "tag":
            changes[log.tag_id] = log.before_json
    return changes


def _batch_dir_ids(task_id: str, session: Session) -> set[str]:
    """本批新建目录的 dir_id 集合（复用目录不记日志、不在其中）。"""
    return {log.tag_id for log, after in _batch_entries(task_id, session)
            if after.get("kind") == "dir"}


@router.get("", response_model=list[ImportTaskOut])
def list_import_tasks(session: Session = Depends(get_session)) -> list[ImportTaskOut]:
    """全部导入任务，按 created_at 降序（最近导入在前）。"""
    tasks = session.query(TagImportTask).order_by(
        TagImportTask.created_at.desc()).all()
    return [
        ImportTaskOut(
            task_id=t.task_id,
            file_name=t.file_name,
            status=t.status,
            created_at=t.created_at,
        )
        for t in tasks
    ]


@router.get("/{task_id}", response_model=ImportTaskDetailOut)
def get_import_task(task_id: str, session: Session = Depends(get_session)) -> ImportTaskDetailOut:
    """导入任务详情：result_summary JSON 解析为结果摘要；未知 task_id → 404。"""
    task = session.get(TagImportTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"导入任务不存在：{task_id}")
    summary: ImportSummary | None = None
    if task.result_summary:
        try:
            summary = ImportSummary.model_validate(json.loads(task.result_summary))
        except (json.JSONDecodeError, ValidationError):
            # 摘要 JSON 损坏时降级为 None，不影响任务元数据返回
            summary = None
    return ImportTaskDetailOut(
        task_id=task.task_id,
        file_name=task.file_name,
        status=task.status,
        created_at=task.created_at,
        summary=summary,
    )


@router.get("/{task_id}/export")
def export_import_task(task_id: str, session: Session = Depends(get_session)) -> Response:
    """导出导入批次的标签为 zip（xlsx+xml）：批内标签行 = 叶子（tag_define，
    全部即叶子），祖先路径 = tag_catalog 链；构建器与 /tags/export 共用。"""
    task = session.get(TagImportTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"导入任务不存在：{task_id}")
    tag_ids = set(_batch_tag_changes(task_id, session))
    leaves = (
        session.query(TagDefine).filter(TagDefine.tag_id.in_(tag_ids)).all()
        if tag_ids else []
    )
    dirs_by_id = {d.dir_id: d for d in session.query(TagCatalog).all()}
    return Response(
        content=_build_export_archive(leaves, dirs_by_id),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{task_id}-export.zip"'},
    )


@router.post("/{task_id}/revoke", response_model=ImportTaskOut)
def revoke_import_task(task_id: str, session: Session = Depends(get_session)) -> ImportTaskOut:
    """撤销导入任务：删除批内新建目录与标签、复活行还原 before 状态。

    409 分支（v1 语义迁移到双表）：已撤销 / 非 completed / 批内标签被
    asset_tag 挂载或 pre_asset_tag 待审核候选引用 / 批内目录被他批子节点
    （标签或子目录）引用。
    """
    task = session.get(TagImportTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"导入任务不存在：{task_id}")
    if task.status == "revoked":
        raise HTTPException(status_code=409, detail="该批次已撤销")
    if task.status != "completed":
        raise HTTPException(status_code=409, detail="只有已完成批次可以撤销")

    changes = _batch_tag_changes(task_id, session)
    tag_ids = set(changes)
    dir_ids = _batch_dir_ids(task_id, session)
    if tag_ids and session.query(AssetTag.id).filter(AssetTag.tag_id.in_(tag_ids)).first():
        raise HTTPException(status_code=409, detail="批次标签已被资产引用，无法撤销")
    # 待审核候选（任意 review_status）经 tag_id 外键引用批内标签：v1 单表
    # 时与正式挂载同表一并拦截，v2 拆表后补齐第二档引用检查（防 FK 500）
    if tag_ids and session.query(PreAssetTag.id).filter(
        PreAssetTag.tag_id.in_(tag_ids)
    ).first():
        raise HTTPException(status_code=409, detail="批次标签已被待审核候选引用，无法撤销")
    if dir_ids:
        # 他批子标签挂在本批目录上（本批自身标签经 tag_ids 排除）
        if session.query(TagDefine.tag_id).filter(
            TagDefine.dir_id.in_(dir_ids),
            ~TagDefine.tag_id.in_(tag_ids),
        ).first():
            raise HTTPException(status_code=409,
                                detail="批次目录存在其他批次的子标签，无法撤销")
        # 他批子目录挂在本批目录下（本批自身目录经 dir_ids 排除）
        if session.query(TagCatalog.dir_id).filter(
            TagCatalog.parent_id.in_(dir_ids),
            ~TagCatalog.dir_id.in_(dir_ids),
        ).first():
            raise HTTPException(status_code=409,
                                detail="批次目录存在其他批次的子目录，无法撤销")

    restore_ids = {tag_id for tag_id, before in changes.items() if before}
    delete_ids = tag_ids - restore_ids
    # 先删批内新建标签（tag_define.dir_id → tag_catalog 外键先于目录删除解除）
    if delete_ids:
        session.query(TagDefine).filter(TagDefine.tag_id.in_(delete_ids)).delete(
            synchronize_session=False,
        )
    if dir_ids:
        # 不变量：candidate_dir_id 持有行必指向 tag_id 所在目录（review/
        # service.py 合入时保证）——该目录因上方 define/子目录双检查（他批
        # 标签挂本批目录即 409）永不可删；将来引入 unmount/rename 须先补
        # PreAssetTag.candidate_dir_id 检查
        # 目录自引用外键：先置空父链再删（v1 手法，子目录任意删除顺序皆安全）
        session.query(TagCatalog).filter(TagCatalog.dir_id.in_(dir_ids)).update(
            {TagCatalog.parent_id: None},
            synchronize_session=False,
        )
        session.query(TagCatalog).filter(TagCatalog.dir_id.in_(dir_ids)).delete(
            synchronize_session=False,
        )
    for tag_id in restore_ids:
        tag = session.get(TagDefine, tag_id)
        before = json.loads(changes[tag_id] or "{}")
        if tag is not None:
            tag.status = before.get("status", "deprecated")
    task.status = "revoked"
    session.commit()
    return ImportTaskOut(
        task_id=task.task_id,
        file_name=task.file_name,
        status=task.status,
        created_at=task.created_at,
    )
