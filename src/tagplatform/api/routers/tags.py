"""`/api/v1/tags` 路由：混合树查询、详情、生命周期与导出 + 标签导入。

v2 双表口径（doc §3.2/3.3/3.6）：目录树在 tag_catalog、叶子标签在
tag_define（经 dir_id 挂目录）、正式挂载在 asset_tag。

- 查询：目录与标签在 Python 侧合并为统一节点 TagNodeOut（不写 SQL
  UNION），排序目录先、名字序，分页在合并列表上切；
- 生命周期三端点只作用于 tag_define（目录 id → 404）；
- 导出 zip（xlsx+xml）格式逐字沿 v1：叶子 = tag_define 行、祖先路径 =
  tag_catalog 链（imports.py 的批次导出共用 _build_export_archive）。

薄路由层：导入解析（按扩展名选择 Excel/XML）→ 治理（run_governance）→
响应模型，业务规则全部在 tagplatform.governance 与 DB 层，router 只做
协议转换与错误映射（解析失败/坏表头 → 422；未知 tag_id → 404）。
"""

import io
import json
import logging
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import asdict
from datetime import datetime, timezone

import openpyxl
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, aliased

from tagplatform.api import get_embedder, get_session
from tagplatform.db.models import (
    AssetTag,
    PreAssetTag,
    TagCatalog,
    TagChangeLog,
    TagDefine,
    TagImportTask,
)
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


class TagNodeOut(BaseModel):
    """混合树节点（目录与标签统一形状，前端消费契约）。

    id 对目录是 dir_id、对标签是 tag_id；kind 区分节点种类（dir|tag）；
    dir_type 仅目录有值（dir_static|dir_dynamic，标签为 null）；目录无
    status 列恒为 null（标签 active|deprecated）；has_children 对目录 =
    存在子目录或目录下挂有标签（与列表过滤无关、按全库判定），标签恒
    False；aliases 列表视图即展开（v1 列表不展开别名的约定随统一节点
    契约退役）。
    """

    id: str
    name: str
    kind: str
    dir_type: str | None = None
    description: str
    parent_id: str | None = None
    status: str | None = None
    has_children: bool = False
    aliases: list[str] = []


class TagNodeDetail(TagNodeOut):
    """节点详情：列表字段之上补从根到本节点的名称/ID 路径（含本节点）。"""

    path: list[str] = []
    path_ids: list[str] = []


class TagListResponse(BaseModel):
    """混合树列表响应（分页经 limit/offset，默认 100/0）。"""

    items: list[TagNodeOut]


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


def _like_pattern(q: str) -> str:
    """q → LIKE 子串模式：通配符按字面转义，保证纯子串语义。"""
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


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
    q: str | None = Query(None),
    parent_id: str | None = Query(None),
    root: bool = Query(False),
    scope_id: str | None = Query(None),
    kind: str | None = Query(None, pattern="^(dir|tag)$"),
    limit: int = Query(100, ge=1),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_session),
) -> TagListResponse:
    """混合树列表：目录与标签合并（目录先、名字序），分页在合并列表上切。

    - 无过滤：全部目录 + 全部标签；
    - root：仅顶层目录（标签必挂目录，不存在根标签）；
    - parent_id：该目录的子目录 + 直挂标签；
    - scope_id：递归子树（CTE 沿 v1 手法跨双表）——后代目录 + 后代目录下
      的标签；标签的后代以其目录的后代目录界定，直挂 scope 本体的标签
      不在结果内；
    - q：目录与标签的 name+aliases 子串（LIKE 转义沿 v1）；
    - kind：dir|tag 二值过滤；v1 的自由 type 分类随 tag_type 枚举化退役。
    """
    dir_query = session.query(TagCatalog)
    tag_query = session.query(TagDefine)

    if scope_id is not None:
        descendants = select(TagCatalog.dir_id).where(
            TagCatalog.dir_id == scope_id).cte(name="dir_descendants", recursive=True)
        child = aliased(TagCatalog)
        descendants = descendants.union_all(
            select(child.dir_id).where(child.parent_id == descendants.c.dir_id)
        )
        descendant_ids = select(descendants.c.dir_id).where(
            descendants.c.dir_id != scope_id
        )
        dir_query = dir_query.filter(TagCatalog.dir_id.in_(descendant_ids))
        tag_query = tag_query.filter(TagDefine.dir_id.in_(descendant_ids))
    elif parent_id is not None:
        dir_query = dir_query.filter(TagCatalog.parent_id == parent_id)
        tag_query = tag_query.filter(TagDefine.dir_id == parent_id)
    elif root:
        dir_query = dir_query.filter(TagCatalog.parent_id.is_(None))
        tag_query = None  # 标签必挂目录：顶层无标签

    if q:
        pattern = _like_pattern(q)
        dir_query = dir_query.filter(or_(
            TagCatalog.dir_name.like(pattern, escape="\\"),
            TagCatalog.aliases.like(pattern, escape="\\"),
        ))
        if tag_query is not None:
            tag_query = tag_query.filter(or_(
                TagDefine.tag_name.like(pattern, escape="\\"),
                TagDefine.aliases.like(pattern, escape="\\"),
            ))

    dir_rows: list[TagCatalog] = dir_query.all() if kind in (None, "dir") else []
    tag_rows: list[TagDefine] = (
        tag_query.all() if kind in (None, "tag") and tag_query is not None else [])

    # 目录 has_children 按全库判定（与本层过滤无关）：有子目录或挂有标签
    occupied: set[str] = set()
    if dir_rows:
        occupied = {
            pid for (pid,) in session.query(TagCatalog.parent_id).filter(
                TagCatalog.parent_id.isnot(None)).distinct()
        } | {
            did for (did,) in session.query(TagDefine.dir_id).distinct()
        }

    dir_nodes = [
        TagNodeOut(
            id=d.dir_id, name=d.dir_name, kind="dir", dir_type=d.dir_type,
            description=d.description, parent_id=d.parent_id, status=None,
            has_children=d.dir_id in occupied, aliases=_parse_aliases(d.aliases),
        )
        for d in dir_rows
    ]
    tag_nodes = [
        TagNodeOut(
            id=t.tag_id, name=t.tag_name, kind="tag", dir_type=None,
            description=t.description, parent_id=t.dir_id, status=t.status,
            has_children=False, aliases=_parse_aliases(t.aliases),
        )
        for t in tag_rows
    ]
    nodes = (sorted(dir_nodes, key=lambda n: n.name)
             + sorted(tag_nodes, key=lambda n: n.name))
    return TagListResponse(items=nodes[offset:offset + limit])


@router.get("/export")
def export_tags(
    q: str | None = Query(None),
    scope_id: str | None = Query(None),
    session: Session = Depends(get_session),
) -> Response:
    """导出当前目录作用域或名称筛选命中的目录及其全部标签（zip）。

    叶子 = tag_define 行（v2 目录不再是节点，全部标签即叶子），祖先路径 =
    tag_catalog 链；scope_id 为目录时导出该目录及其子树下全部标签（含直挂
    标签，v1「该目录及其全部标签」语义）；q 命中目录导出其子树叶子、命中
    标签导出该标签本身。
    """
    all_dirs = session.query(TagCatalog).all()
    dirs_by_id = {d.dir_id: d for d in all_dirs}
    children: dict[str | None, list[TagCatalog]] = {}
    for d in all_dirs:
        children.setdefault(d.parent_id, []).append(d)

    def descendant_dirs(dir_id: str) -> set[str]:
        result: set[str] = set()
        pending = [dir_id]
        while pending:
            current = pending.pop()
            for child_dir in children.get(current, []):
                if child_dir.dir_id not in result:
                    result.add(child_dir.dir_id)
                    pending.append(child_dir.dir_id)
        return result

    all_tags = session.query(TagDefine).order_by(TagDefine.tag_id).all()
    if scope_id is not None:
        if scope_id not in dirs_by_id:
            raise HTTPException(status_code=404, detail=f"目录不存在：{scope_id}")
        selected_dirs = {scope_id} | descendant_dirs(scope_id)
        selected_tags = {t.tag_id for t in all_tags if t.dir_id in selected_dirs}
    else:
        selected_dirs = set(dirs_by_id)
        selected_tags = {t.tag_id for t in all_tags}

    if q:
        keyword = q.casefold()
        keep_dirs: set[str] = set()
        for d in all_dirs:
            if (keyword in d.dir_name.casefold()
                    or (d.aliases is not None and keyword in d.aliases.casefold())):
                keep_dirs |= {d.dir_id} | descendant_dirs(d.dir_id)
        keep_dirs &= selected_dirs
        keep_tags = {
            t.tag_id for t in all_tags
            if keyword in t.tag_name.casefold()
            or (t.aliases is not None and keyword in t.aliases.casefold())
        }
        keep_tags |= {t.tag_id for t in all_tags if t.dir_id in keep_dirs}
        selected_tags = keep_tags & selected_tags

    export_leaves = [t for t in all_tags if t.tag_id in selected_tags]
    return Response(
        content=_build_export_archive(export_leaves, dirs_by_id),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="tags-export.zip"'},
    )


def _tag_path(tag: TagDefine, dirs_by_id: dict[str, TagCatalog]) -> list[str]:
    """叶子全路径名：根→直属目录的目录名链 + 叶子标签名（含叶子，沿 v1）。"""
    ancestors: list[str] = []
    current: TagCatalog | None = dirs_by_id.get(tag.dir_id)
    while current is not None:
        ancestors.append(current.dir_name)
        current = dirs_by_id.get(current.parent_id) if current.parent_id else None
    ancestors.reverse()
    return [*ancestors, tag.tag_name]


def _build_export_archive(
    export_tags: list[TagDefine], dirs_by_id: dict[str, TagCatalog]
) -> bytes:
    """叶子标签行 + 目录祖先链 → zip（xlsx 六列 + xml 嵌套，v1 格式逐字）。

    xlsx 三级列取路径前三段（含叶子名，沿 v1 的 path 计算口径），xml 按
    路径嵌套 level1/level2/…、叶子为 <tag> 节点；标签生成方式列固定
    "系统生成"（v1 常量）。
    """
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.title = "标签"
    worksheet.append(["一级标签名称", "二级标签名称", "三级标签名称", "标签", "标签描述", "标签生成方式"])
    for tag in export_tags:
        path = _tag_path(tag, dirs_by_id)
        levels = (path + ["", "", ""])[:3]
        worksheet.append([*levels, tag.tag_name, tag.description, "系统生成"])

    workbook_output = io.BytesIO()
    workbook.save(workbook_output)

    xml_root = ET.Element("tags")
    xml_nodes: dict[tuple[str, ...], ET.Element] = {}
    for tag in export_tags:
        path = _tag_path(tag, dirs_by_id)
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


@router.get("/{node_id}", response_model=TagNodeDetail)
def get_node(node_id: str, session: Session = Depends(get_session)) -> TagNodeDetail:
    """节点详情：id 可为 dir_id（目录）或 tag_id（标签），含 aliases 与
    根→本节点路径；未知 id → 404。"""
    d = session.get(TagCatalog, node_id)
    if d is not None:
        names: list[str] = []
        ids: list[str] = []
        current: TagCatalog | None = d
        while current is not None:
            names.append(current.dir_name)
            ids.append(current.dir_id)
            current = session.get(TagCatalog, current.parent_id) if current.parent_id else None
        names.reverse()
        ids.reverse()
        has_children = (
            session.query(TagCatalog.dir_id).filter(
                TagCatalog.parent_id == node_id).first() is not None
            or session.query(TagDefine.tag_id).filter(
                TagDefine.dir_id == node_id).first() is not None
        )
        return TagNodeDetail(
            id=d.dir_id, name=d.dir_name, kind="dir", dir_type=d.dir_type,
            description=d.description, parent_id=d.parent_id, status=None,
            has_children=has_children, aliases=_parse_aliases(d.aliases),
            path=names, path_ids=ids,
        )

    tag = session.get(TagDefine, node_id)
    if tag is not None:
        dir_chain: list[TagCatalog] = []
        current = session.get(TagCatalog, tag.dir_id)
        while current is not None:
            dir_chain.append(current)
            current = session.get(TagCatalog, current.parent_id) if current.parent_id else None
        dir_chain.reverse()
        return TagNodeDetail(
            id=tag.tag_id, name=tag.tag_name, kind="tag", dir_type=None,
            description=tag.description, parent_id=tag.dir_id, status=tag.status,
            has_children=False, aliases=_parse_aliases(tag.aliases),
            path=[d.dir_name for d in dir_chain] + [tag.tag_name],
            path_ids=[d.dir_id for d in dir_chain] + [tag.tag_id],
        )

    raise HTTPException(status_code=404, detail=f"标签或目录不存在：{node_id}")


def _get_define_or_404(tag_id: str, session: Session) -> TagDefine:
    """生命周期目标解析：仅 tag_define；目录 id → 404（文案含"目录"）。"""
    define = session.get(TagDefine, tag_id)
    if define is not None:
        return define
    if session.get(TagCatalog, tag_id) is not None:
        raise HTTPException(status_code=404, detail=f"目录不支持生命周期操作：{tag_id}")
    raise HTTPException(status_code=404, detail=f"标签不存在：{tag_id}")


def _set_tag_status(tag_id: str, status: str, action: str, session: Session) -> TagNodeDetail:
    """改标签定义状态并写变更日志；同状态重复调用早返回不重复记日志。

    状态字面量 v2 统一 deprecated（v1 "deprecate" 退役，doc §3.3）；action
    沿 v1 枚举现值：停用记 deprecate、启用记 update（operator 沿 v1 记
    governance——治理/生命周期为平台自身操作）。
    """
    define = _get_define_or_404(tag_id, session)
    if define.status == status:
        return get_node(tag_id, session)
    before = {"status": define.status}
    define.status = status
    define.updated_at = datetime.now(timezone.utc)
    session.add(TagChangeLog(
        tag_id=tag_id,
        action=action,
        before_json=json.dumps(before, ensure_ascii=False),
        after_json=json.dumps({"status": status}, ensure_ascii=False),
        operator="governance",
    ))
    session.commit()
    return get_node(tag_id, session)


@router.post("/{tag_id}/deactivate", response_model=TagNodeDetail)
def deactivate_tag(tag_id: str, session: Session = Depends(get_session)) -> TagNodeDetail:
    return _set_tag_status(tag_id, "deprecated", "deprecate", session)


@router.post("/{tag_id}/activate", response_model=TagNodeDetail)
def activate_tag(tag_id: str, session: Session = Depends(get_session)) -> TagNodeDetail:
    return _set_tag_status(tag_id, "active", "update", session)


@router.delete("/{tag_id}", response_model=TagDeleteResponse)
def delete_tag(tag_id: str, session: Session = Depends(get_session)) -> TagDeleteResponse:
    """删除标签定义（仅 tag_define；目录 id → 404）。

    被 asset_tag 正式挂载或 pre_asset_tag 待审核候选引用 → 409；删除后自下
    而上剪除空祖先目录（空 = 无子目录且无挂载标签，v1 手法沿用）；响应带
    重定向上级目录。变更日志沿 v1 生命周期行为写 action="delete"（该值
    2026-11-06 已补入 doc §3.8 的 CHECK 枚举——v1 无 CHECK 属历史漏网的
    文档缺口），before_json 留全量快照、after_json 为空。
    """
    define = _get_define_or_404(tag_id, session)
    if session.query(AssetTag.id).filter(AssetTag.tag_id == tag_id).first():
        raise HTTPException(status_code=409, detail="标签已被资产引用，无法删除")
    if session.query(PreAssetTag.id).filter(PreAssetTag.tag_id == tag_id).first():
        raise HTTPException(status_code=409, detail="标签已被待审核候选引用，无法删除")

    before = {
        "tag_id": define.tag_id,
        "tag_name": define.tag_name,
        "tag_type": define.tag_type,
        "dir_id": define.dir_id,
        "status": define.status,
    }
    parent = session.get(TagCatalog, define.dir_id) if define.dir_id else None
    deleted_parent_ids: set[str] = set()
    session.delete(define)
    session.flush()

    # 删除标签后，向上清理既无子目录也无挂载标签的空目录。
    # 不变量：candidate_dir_id 持有行必指向 tag_id 所在目录（review/
    # service.py 合入时保证）——该目录因上方 pre 引用 409 与本处 has_tag
    # 空判定双检查永不可删；将来引入 unmount/rename 须先补
    # PreAssetTag.candidate_dir_id 检查
    while parent is not None:
        has_dir_child = session.query(TagCatalog.dir_id).filter(
            TagCatalog.parent_id == parent.dir_id
        ).first()
        has_tag = session.query(TagDefine.tag_id).filter(
            TagDefine.dir_id == parent.dir_id
        ).first()
        if has_dir_child or has_tag:
            break
        next_parent = session.get(TagCatalog, parent.parent_id) if parent.parent_id else None
        deleted_parent_ids.add(parent.dir_id)
        session.delete(parent)
        session.flush()
        parent = next_parent

    session.add(TagChangeLog(
        tag_id=tag_id,
        action="delete",
        before_json=json.dumps(before, ensure_ascii=False),
        after_json=None,
        operator="governance",
    ))
    redirect_target = parent
    if parent is not None and parent.dir_id in deleted_parent_ids:
        redirect_target = (
            session.get(TagCatalog, parent.parent_id)
            if parent.parent_id
            else None
        )
    session.commit()
    return TagDeleteResponse(
        tag_id=tag_id,
        parent_id=redirect_target.dir_id if redirect_target is not None else None,
        parent_name=redirect_target.dir_name if redirect_target is not None else None,
    )
