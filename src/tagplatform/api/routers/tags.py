"""`/api/v1/tags` 路由：标签导入（multipart 上传）与标签查询。

薄路由层：解析（parse_tag_excel）→ 治理（run_governance）→ 响应模型，
业务规则全部在 tagplatform.governance 与 DB 层，router 只做协议转换与
错误映射（解析失败/坏表头 → 422；未知 tag_id → 404）。
"""

import json
from dataclasses import asdict

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.orm import Session

from tagplatform.api import get_embedder, get_session
from tagplatform.db.models import ImportBatch, TagCatalog
from tagplatform.governance import parse_tag_excel, run_governance

router = APIRouter(prefix="/api/v1/tags")


# ---------- 响应模型 ----------

class IssueOut(BaseModel):
    """单条治理问题明细（与 governance.Issue 字段一一对应）。"""

    tag_name: str
    code: str
    detail: str
    similar_to: str | None = None


class ImportSummary(BaseModel):
    """导入结果摘要：total/inserted/skipped 计数 + issues 明细列表。

    语义与 import_batch.result_summary（治理流水线写入）一致：total 为
    传入治理的行数（行级解析错误的行不进 rows，不计入），skipped =
    total - inserted，issues 为治理问题明细（与入库行不相交）。
    """

    total: int
    inserted: int
    skipped: int
    issues: list[IssueOut]


class ImportResponse(BaseModel):
    """导入响应：本批批次号 + 结果摘要。"""

    batch_id: str
    summary: ImportSummary


class TagOut(BaseModel):
    """标签列表项（不含 aliases——列表视图不展开别名 JSON）。"""

    tag_id: str
    tag_name: str
    tag_type: str
    description: str
    parent_id: str | None = None
    status: str | None = None


class TagListResponse(BaseModel):
    """标签列表响应（分页经 limit/offset，默认 100/0）。"""

    items: list[TagOut]


class TagDetail(TagOut):
    """标签详情：在列表字段之上展开 aliases 列表。"""

    aliases: list[str]


def _parse_aliases(raw: str | None) -> list[str]:
    """aliases JSON 数组字符串 → list[str]；空/损坏 JSON 按 [] 兜底。"""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


# ---------- 路由 ----------

@router.post("/import", response_model=ImportResponse)
def import_tags(
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
    embedder=Depends(get_embedder),
) -> ImportResponse:
    """上传标签 Excel：解析 → 治理入库 → 返回批次号与结果摘要。

    同步 def：治理流水线（embed + DB）为阻塞操作，走线程池不占事件循环。
    """
    data = file.file.read()
    try:
        parsed = parse_tag_excel(data)
    except Exception:
        # openpyxl 对损坏/非 xlsx 字节抛多种异常（BadZipFile、
        # InvalidFileException 等，Task 6 约定由本层捕获映射）：统一 422
        raise HTTPException(status_code=422, detail="文件无法解析为 xlsx 表格")
    if parsed.header_error:
        # 缺必需表头列：422，detail 为解析层给出的错误说明
        raise HTTPException(status_code=422, detail=parsed.header_error)

    result = run_governance(parsed.rows, session, embedder)

    # 回填真实上传文件名（治理流水线写入的是占位 "governance"）
    batch = session.get(ImportBatch, result.batch_id)
    if batch is not None:
        batch.file_name = file.filename or "upload.xlsx"
        session.commit()

    return ImportResponse(
        batch_id=result.batch_id,
        summary=ImportSummary(
            total=len(parsed.rows),
            inserted=len(result.inserted),
            skipped=len(parsed.rows) - len(result.inserted),
            issues=[IssueOut(**asdict(issue)) for issue in result.issues],
        ),
    )


@router.get("", response_model=TagListResponse)
def list_tags(
    type_: str | None = Query(None, alias="type"),
    q: str | None = Query(None),
    limit: int = Query(100, ge=1),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_session),
) -> TagListResponse:
    """标签目录列表：type 精确过滤；q 对 tag_name 与 aliases 子串匹配。"""
    query = session.query(TagCatalog)
    if type_ is not None:
        query = query.filter(TagCatalog.tag_type == type_)
    if q:
        # LIKE 通配符按字面转义，保证 q 为纯子串语义
        pattern = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        query = query.filter(or_(
            TagCatalog.tag_name.like(pattern, escape="\\"),
            TagCatalog.aliases.like(pattern, escape="\\"),
        ))
    rows = query.order_by(TagCatalog.tag_id).offset(offset).limit(limit).all()
    return TagListResponse(items=[
        TagOut(
            tag_id=t.tag_id,
            tag_name=t.tag_name,
            tag_type=t.tag_type,
            description=t.description,
            parent_id=t.parent_id,
            status=t.status,
        )
        for t in rows
    ])


@router.get("/{tag_id}", response_model=TagDetail)
def get_tag(tag_id: str, session: Session = Depends(get_session)) -> TagDetail:
    """单条标签详情：含解析后的 aliases 列表；未知 tag_id → 404。"""
    tag = session.get(TagCatalog, tag_id)
    if tag is None:
        raise HTTPException(status_code=404, detail=f"标签不存在：{tag_id}")
    return TagDetail(
        tag_id=tag.tag_id,
        tag_name=tag.tag_name,
        tag_type=tag.tag_type,
        description=tag.description,
        parent_id=tag.parent_id,
        status=tag.status,
        aliases=_parse_aliases(tag.aliases),
    )
