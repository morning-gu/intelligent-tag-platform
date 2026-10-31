"""`/api/v1/prompts` 路由：Prompt 模板列表。

薄路由层：只读 prompt_template 表（激活模板由迁移种子/治理侧管理，本层
不做写入），供前端查看各模式的模板版本清单（阶段 3 审核后台模板视图的
数据源）。模板正文大字段不出列表——正文仅供生成流水线（Task 8 执行器）
消费。
"""

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from tagplatform.api import get_session
from tagplatform.db.models import PromptTemplate

router = APIRouter(prefix="/api/v1")


class PromptOut(BaseModel):
    """Prompt 模板列表项：模式 + 版本 + 是否激活 + 创建时间。"""

    mode: str
    version: int
    is_active: bool
    created_at: datetime | None = None


@router.get("/prompts", response_model=list[PromptOut])
def list_prompts(
    mode: str | None = Query(None),
    session: Session = Depends(get_session),
) -> list[PromptOut]:
    """Prompt 模板列表：mode 精确过滤，按 mode + version 排序。"""
    query = session.query(PromptTemplate)
    if mode is not None:
        query = query.filter(PromptTemplate.mode == mode)
    rows = query.order_by(PromptTemplate.mode, PromptTemplate.version).all()
    return [
        PromptOut(mode=r.mode, version=r.version, is_active=r.is_active,
                  created_at=r.created_at)
        for r in rows
    ]
