"""`/api/v1/import/batches` 路由：导入批次列表与详情。

薄路由层：查询 import_batch → 响应模型；详情端点负责把批次记录里的
result_summary JSON（治理流水线写入）解析为结构化摘要（issues 为明细
列表），未知 batch_id → 404。
"""

import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from tagplatform.api import get_session
from tagplatform.api.routers.tags import ImportSummary
from tagplatform.db.models import ImportBatch

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
