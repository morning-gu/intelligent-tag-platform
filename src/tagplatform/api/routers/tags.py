"""`/api/v1/tags` 路由：标签导入（Excel/XML 上传）与标签查询。

薄路由层：解析（按扩展名选择 Excel/XML）→ 治理（run_governance）→ 响应模型，
业务规则全部在 tagplatform.governance 与 DB 层，router 只做协议转换与
错误映射（解析失败/坏表头 → 422；未知 tag_id → 404）。
"""

import io
import json
import logging
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import asdict

import openpyxl
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, aliased

from tagplatform.api import get_embedder, get_session
from tagplatform.db.models import AssetTag, TagCatalog, TagChangeLog, TagImportTask
from tagplatform.governance import parse_tag_excel, parse_tag_xml, run_governance

router = APIRouter(prefix="/api/v1/tags")

logger = logging.getLogger(__name__)


# ---------- 响应模型 ----------

class IssueOut(BaseModel):
    """单条治理问题明细（与 governance.Issue 字段一一对应）。"""

    tag_name: str
    code: str
    detail: str
    similar_to: str | None = None


class ParseErrorOut(BaseModel):
    """单行解析失败明细（与 governance.excel.RowError 字段一一对应）。"""

    row_number: int
    tag_name: str | None = None
    code: str
    detail: str


class ImportSummary(BaseModel):
    """导入结果摘要：total/inserted/skipped 计数 + issues/parse_errors 明细列表。

    语义与 tag_import_task.result_summary（治理流水线写入）一致：total 为
    传入治理的行数（目录行与标签行合计；行级解析错误的行不进 rows，不计
    入），inserted 仅计标签行（目录行落 tag_catalog 不计入），skipped =
    total - inserted，issues 为治理问题明细（与入库行不相交）；
    parse_errors 为行级解析错误明细（治理流水线不感知解析层 errors，
    由本路由层与 file_name 回填同一模式并入 result_summary），计数以
    列表长度为准（前端阶段 3 直接消费明细）。
    """

    total: int
    inserted: int
    skipped: int
    issues: list[IssueOut]
    parse_errors: list[ParseErrorOut] = []


class ImportResponse(BaseModel):
    """导入响应：本批导入任务号 + 结果摘要（v1 batch_id 字段退役）。"""

    task_id: str
    summary: ImportSummary


class TagOut(BaseModel):
    """标签列表项（不含 aliases——列表视图不展开别名 JSON）。"""

    tag_id: str
    tag_name: str
    tag_type: str
    description: str
    parent_id: str | None = None
    status: str | None = None
    has_children: bool = False


class TagListResponse(BaseModel):
    """标签列表响应（分页经 limit/offset，默认 100/0）。"""

    items: list[TagOut]


class TagDetail(TagOut):
    """标签详情：在列表字段之上展开 aliases 列表。"""

    aliases: list[str]
    path: list[str] = []
    path_ids: list[str] = []


class TagDeleteResponse(BaseModel):
    """标签删除结果及目录页应跳转的上级目录。"""

    tag_id: str
    parent_id: str | None = None
    parent_name: str | None = None


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
    """上传标签 Excel/XML：解析 → 治理双表入库 → 返回导入任务号与结果摘要。

    同步 def：治理流水线（embed + DB）为阻塞操作，走线程池不占事件循环。
    """
    data = file.file.read()
    filename = (file.filename or "").lower()
    looks_like_xml = data.lstrip().startswith(b"<")
    try:
        if filename.endswith(".xml") or looks_like_xml:
            parsed = parse_tag_xml(data)
        elif filename.endswith(".xlsx"):
            parsed = parse_tag_excel(data)
        else:
            raise HTTPException(status_code=422, detail="仅支持 .xlsx 或 .xml 文件")
    except HTTPException:
        raise
    except Exception:
        # 解析器异常类型分别来自 openpyxl 与 ElementTree，统一映射为 422。
        logger.exception("标签文件解析失败: %s", file.filename or "upload")
        raise HTTPException(status_code=422, detail="文件无法解析为有效的 XML 或 xlsx 文件")
    if parsed.header_error:
        # 缺必需表头列：422，detail 为解析层给出的错误说明
        raise HTTPException(status_code=422, detail=parsed.header_error)

    result = run_governance(parsed.rows, session, embedder)

    # 回填真实上传文件名（治理流水线写入的是占位 "governance"），并把
    # 行级解析错误明细并入 result_summary——治理流水线不感知 parsed.errors，
    # 与 file_name 回填同一模式：本路由层更新已提交的任务行
    parse_errors = [ParseErrorOut(**asdict(e)) for e in parsed.errors]
    task = session.get(TagImportTask, result.task_id)
    if task is not None:
        task.file_name = file.filename or "upload"
        summary_data = json.loads(task.result_summary)
        summary_data["parse_errors"] = [asdict(e) for e in parsed.errors]
        task.result_summary = json.dumps(summary_data, ensure_ascii=False)
        session.commit()

    return ImportResponse(
        task_id=result.task_id,
        summary=ImportSummary(
            total=len(parsed.rows),
            inserted=len(result.inserted),
            skipped=len(parsed.rows) - len(result.inserted),
            issues=[IssueOut(**asdict(issue)) for issue in result.issues],
            parse_errors=parse_errors,
        ),
    )


@router.get("", response_model=TagListResponse)
def list_tags(
    type_: str | None = Query(None, alias="type"),
    q: str | None = Query(None),
    parent_id: str | None = Query(None),
    root: bool = Query(False),
    scope_id: str | None = Query(None),
    limit: int = Query(100, ge=1),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_session),
) -> TagListResponse:
    """标签目录列表：type 精确过滤；q 对 tag_name 与 aliases 子串匹配。"""
    query = session.query(TagCatalog)
    if root:
        query = query.filter(TagCatalog.parent_id.is_(None))
    elif parent_id is not None:
        query = query.filter(TagCatalog.parent_id == parent_id)
    elif scope_id is not None:
        descendants = select(TagCatalog.tag_id).where(TagCatalog.tag_id == scope_id).cte(
            name="tag_descendants", recursive=True
        )
        child = aliased(TagCatalog)
        descendants = descendants.union_all(
            select(child.tag_id).where(child.parent_id == descendants.c.tag_id)
        )
        query = query.filter(
            TagCatalog.tag_id.in_(select(descendants.c.tag_id).where(
                descendants.c.tag_id != scope_id
            ))
        )
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
            has_children=session.query(TagCatalog.tag_id)
            .filter(TagCatalog.parent_id == t.tag_id)
            .first() is not None,
        )
        for t in rows
    ])


@router.get("/export")
def export_tags(
    q: str | None = Query(None),
    scope_id: str | None = Query(None),
    session: Session = Depends(get_session),
) -> Response:
    """导出当前目录作用域或名称筛选命中的目录及其全部标签。"""
    all_tags = session.query(TagCatalog).order_by(TagCatalog.tag_id).all()
    by_id = {tag.tag_id: tag for tag in all_tags}
    children: dict[str | None, list[TagCatalog]] = {}
    for tag in all_tags:
        children.setdefault(tag.parent_id, []).append(tag)

    def descendants(tag_id: str) -> set[str]:
        result: set[str] = set()
        pending = [tag_id]
        while pending:
            current = pending.pop()
            for child in children.get(current, []):
                if child.tag_id not in result:
                    result.add(child.tag_id)
                    pending.append(child.tag_id)
        return result

    selected_ids = descendants(scope_id) if scope_id is not None else {
        tag.tag_id for tag in all_tags
    }
    if scope_id is not None and scope_id not in by_id:
        raise HTTPException(status_code=404, detail=f"目录不存在：{scope_id}")

    if q:
        keyword = q.casefold()
        matches = [
            tag for tag in all_tags
            if keyword in tag.tag_name.casefold()
            or (tag.aliases is not None and keyword in tag.aliases.casefold())
        ]
        selected_ids = {
            descendant_id
            for tag in matches
            if tag.tag_id in selected_ids
            for descendant_id in ({tag.tag_id} | descendants(tag.tag_id))
        }

    export_tags = [
        tag for tag in all_tags
        if tag.tag_id in selected_ids and not children.get(tag.tag_id)
    ]

    return Response(
        content=_build_export_archive(export_tags, by_id),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="tags-export.zip"'},
    )


def _build_export_archive(export_tags: list[TagCatalog], by_id: dict[str, TagCatalog]) -> bytes:
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.title = "标签"
    worksheet.append(["一级标签名称", "二级标签名称", "三级标签名称", "标签", "标签描述", "标签生成方式"])
    for tag in export_tags:
        path: list[str] = []
        current: TagCatalog | None = tag
        while current is not None:
            path.append(current.tag_name)
            current = by_id.get(current.parent_id) if current.parent_id else None
        path.reverse()
        levels = (path + ["", "", ""])[:3]
        worksheet.append([*levels, tag.tag_name, tag.description, "系统生成"])

    workbook_output = io.BytesIO()
    workbook.save(workbook_output)

    xml_root = ET.Element("tags")
    xml_nodes: dict[tuple[str, ...], ET.Element] = {}
    for tag in export_tags:
        path: list[str] = []
        current = tag
        while current is not None:
            path.append(current.tag_name)
            current = by_id.get(current.parent_id) if current.parent_id else None
        path.reverse()
        if len(path) == 1:
            ET.SubElement(
                xml_root,
                "tag",
                {"type": "system", "description": tag.description or ""},
            ).text = path[0]
            continue

        parent = xml_root
        for depth, name in enumerate(path[:-1], start=1):
            key = tuple(path[:depth])
            node = xml_nodes.get(key)
            if node is None:
                node = ET.SubElement(parent, f"level{depth}", {"name": name})
                xml_nodes[key] = node
            parent = node
        ET.SubElement(
            parent,
            "tag",
            {"type": "system", "description": tag.description or ""},
        ).text = path[-1]

    xml_output = ET.tostring(xml_root, encoding="utf-8", xml_declaration=True)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("tags-export.xlsx", workbook_output.getvalue())
        archive.writestr("tags-export.xml", xml_output)
    return output.getvalue()


@router.get("/{tag_id}", response_model=TagDetail)
def get_tag(tag_id: str, session: Session = Depends(get_session)) -> TagDetail:
    """单条标签详情：含解析后的 aliases 列表；未知 tag_id → 404。"""
    tag = session.get(TagCatalog, tag_id)
    if tag is None:
        raise HTTPException(status_code=404, detail=f"标签不存在：{tag_id}")
    path: list[str] = []
    current = tag
    while current is not None:
        path.append(current.tag_name)
        current = session.get(TagCatalog, current.parent_id) if current.parent_id else None
    path.reverse()
    path_ids = []
    current = tag
    while current is not None:
        path_ids.append(current.tag_id)
        current = session.get(TagCatalog, current.parent_id) if current.parent_id else None
    path_ids.reverse()
    return TagDetail(
        tag_id=tag.tag_id,
        tag_name=tag.tag_name,
        tag_type=tag.tag_type,
        description=tag.description,
        parent_id=tag.parent_id,
        status=tag.status,
        aliases=_parse_aliases(tag.aliases),
        path=path,
        path_ids=path_ids,
    )


def _set_tag_status(tag_id: str, status: str, session: Session) -> TagDetail:
    tag = session.get(TagCatalog, tag_id)
    if tag is None:
        raise HTTPException(status_code=404, detail=f"标签不存在：{tag_id}")
    if tag.status == status:
        return get_tag(tag_id, session)
    before = {"status": tag.status}
    tag.status = status
    session.add(TagChangeLog(
        tag_id=tag_id,
        action="deprecate" if status == "deprecate" else "update",
        before_json=json.dumps(before, ensure_ascii=False),
        after_json=json.dumps({"status": status}, ensure_ascii=False),
        operator="governance",
    ))
    session.commit()
    return get_tag(tag_id, session)


@router.post("/{tag_id}/deactivate", response_model=TagDetail)
def deactivate_tag(tag_id: str, session: Session = Depends(get_session)) -> TagDetail:
    return _set_tag_status(tag_id, "deprecate", session)


@router.post("/{tag_id}/activate", response_model=TagDetail)
def activate_tag(tag_id: str, session: Session = Depends(get_session)) -> TagDetail:
    return _set_tag_status(tag_id, "active", session)


@router.delete("/{tag_id}", response_model=TagDeleteResponse)
def delete_tag(tag_id: str, session: Session = Depends(get_session)) -> TagDeleteResponse:
    tag = session.get(TagCatalog, tag_id)
    if tag is None:
        raise HTTPException(status_code=404, detail=f"标签不存在：{tag_id}")
    if session.query(AssetTag.id).filter(AssetTag.tag_id == tag_id).first():
        raise HTTPException(status_code=409, detail="标签已被资产引用，无法删除")

    parent = session.get(TagCatalog, tag.parent_id) if tag.parent_id else None
    deleted_parent_ids: set[str] = set()
    session.delete(tag)
    session.flush()

    # 删除标签后，向上清理没有任何子节点的空目录。
    while parent is not None:
        has_children = session.query(TagCatalog.tag_id).filter(
            TagCatalog.parent_id == parent.tag_id
        ).first()
        if has_children:
            break
        next_parent = session.get(TagCatalog, parent.parent_id) if parent.parent_id else None
        deleted_parent_ids.add(parent.tag_id)
        session.delete(parent)
        session.flush()
        parent = next_parent

    session.add(TagChangeLog(
        tag_id=tag_id,
        action="delete",
        before_json=json.dumps({"tag_name": tag.tag_name}, ensure_ascii=False),
        after_json=None,
        operator="governance",
    ))
    redirect_target = parent
    if parent is not None and parent.tag_id in deleted_parent_ids:
        redirect_target = (
            session.get(TagCatalog, parent.parent_id)
            if parent.parent_id
            else None
        )
    session.commit()
    return TagDeleteResponse(
        tag_id=tag_id,
        parent_id=redirect_target.tag_id if redirect_target is not None else None,
        parent_name=redirect_target.tag_name if redirect_target is not None else None,
    )
