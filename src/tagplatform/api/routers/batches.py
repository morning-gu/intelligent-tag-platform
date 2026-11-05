"""`/api/v1/import/batches` 路由：导入批次列表与详情。

薄路由层：查询 import_batch → 响应模型；详情端点负责把批次记录里的
result_summary JSON（治理流水线写入）解析为结构化摘要（issues 为明细
列表），未知 batch_id → 404。
"""

import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from tagplatform.api import get_session
from tagplatform.api.routers.tags import ImportSummary, _build_export_archive
from tagplatform.db.models import AssetTag, TagCatalog, TagChangeLog, TagImportTask

router = APIRouter(prefix="/api/v1/import/batches")


class BatchOut(BaseModel):
    """批次列表项。"""

    batch_id: str
    file_name: str
    status: str
    created_at: datetime | None = None


class BatchDetailOut(BatchOut):
    """批次详情：含 result_summary 解析后的结果摘要（无摘要时为 None）。"""

    summary: ImportSummary | None = None


def _batch_tag_ids(batch_id: str, session: Session) -> set[str]:
    ids: set[str] = set()
    for log in session.query(TagChangeLog).filter(
        TagChangeLog.action == "import",
    ).all():
        if not log.after_json:
            continue
        try:
            after = json.loads(log.after_json)
        except json.JSONDecodeError:
            continue
        if after.get("batch_id") == batch_id:
            ids.add(log.tag_id)
    return ids


def _batch_tag_changes(batch_id: str, session: Session) -> dict[str, str | None]:
    changes: dict[str, str | None] = {}
    for log in session.query(TagChangeLog).filter(TagChangeLog.action == "import").all():
        if not log.after_json:
            continue
        try:
            after = json.loads(log.after_json)
        except json.JSONDecodeError:
            continue
        if after.get("batch_id") == batch_id:
            changes[log.tag_id] = log.before_json
    return changes


@router.get("", response_model=list[BatchOut])
def list_batches(session: Session = Depends(get_session)) -> list[BatchOut]:
    """全部导入批次，按 created_at 降序（最近导入在前）。"""
    batches = session.query(ImportBatch).order_by(ImportBatch.created_at.desc()).all()
    return [
        BatchOut(
            batch_id=b.batch_id,
            file_name=b.file_name,
            status=b.status,
            created_at=b.created_at,
        )
        for b in batches
    ]


@router.get("/{batch_id}", response_model=BatchDetailOut)
def get_batch(batch_id: str, session: Session = Depends(get_session)) -> BatchDetailOut:
    """批次详情：result_summary JSON 解析为结果摘要；未知 batch_id → 404。"""
    batch = session.get(ImportBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"导入批次不存在：{batch_id}")
    summary: ImportSummary | None = None
    if batch.result_summary:
        try:
            summary = ImportSummary.model_validate(json.loads(batch.result_summary))
        except (json.JSONDecodeError, ValidationError):
            # 摘要 JSON 损坏时降级为 None，不影响批次元数据返回
            summary = None
    return BatchDetailOut(
        batch_id=batch.batch_id,
        file_name=batch.file_name,
        status=batch.status,
        created_at=batch.created_at,
        summary=summary,
    )


@router.get("/{batch_id}/export")
def export_batch(batch_id: str, session: Session = Depends(get_session)) -> Response:
    batch = session.get(ImportBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"导入批次不存在：{batch_id}")
    changes = _batch_tag_changes(batch_id, session)
    tag_ids = set(changes)
    tags = session.query(TagCatalog).filter(TagCatalog.tag_id.in_(tag_ids)).all() if tag_ids else []
    by_id = {tag.tag_id: tag for tag in session.query(TagCatalog).all()}
    return Response(
        content=_build_export_archive(
            [tag for tag in tags if not session.query(TagCatalog.tag_id).filter(
                TagCatalog.parent_id == tag.tag_id
            ).first()],
            by_id,
        ),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{batch_id}-export.zip"'},
    )


@router.post("/{batch_id}/revoke", response_model=BatchOut)
def revoke_batch(batch_id: str, session: Session = Depends(get_session)) -> BatchOut:
    batch = session.get(ImportBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"导入批次不存在：{batch_id}")
    if batch.status == "revoked":
        raise HTTPException(status_code=409, detail="该批次已撤销")
    if batch.status != "completed":
        raise HTTPException(status_code=409, detail="只有已完成批次可以撤销")

    changes = _batch_tag_changes(batch_id, session)
    tag_ids = set(changes)
    if tag_ids and session.query(AssetTag.id).filter(AssetTag.tag_id.in_(tag_ids)).first():
        raise HTTPException(status_code=409, detail="批次标签已被资产引用，无法撤销")
    if tag_ids and session.query(TagCatalog.tag_id).filter(
        TagCatalog.parent_id.in_(tag_ids),
        ~TagCatalog.tag_id.in_(tag_ids),
    ).first():
        raise HTTPException(status_code=409, detail="批次标签存在其他批次的子目录，无法撤销")

    restore_ids = {tag_id for tag_id, before in changes.items() if before}
    delete_ids = tag_ids - restore_ids
    session.query(TagCatalog).filter(TagCatalog.tag_id.in_(delete_ids)).update(
        {TagCatalog.parent_id: None},
        synchronize_session=False,
    )
    session.query(TagCatalog).filter(TagCatalog.tag_id.in_(delete_ids)).delete(
        synchronize_session=False,
    )
    for tag_id in restore_ids:
        tag = session.get(TagCatalog, tag_id)
        before = json.loads(changes[tag_id] or "{}")
        if tag is not None:
            tag.status = before.get("status", "deprecate")
    batch.status = "revoked"
    session.commit()
    return BatchOut(
        batch_id=batch.batch_id,
        file_name=batch.file_name,
        status=batch.status,
        created_at=batch.created_at,
    )
