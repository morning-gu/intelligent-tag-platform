"""标签域编排：标签库查询/导入/生命周期/导出 + 标签库统计视图。

用例清单（原 api/routers/tags.py 与 api/routers/library.py 的读路径 SQL 与
装配逻辑，Task 11 原样下沉；SQL 与装配零语义变化，Task 12 再做 list
分页/导出分批改造）：

- `import_tag_file`  上传标签 Excel/XML：解析 → 治理双表入库 → 任务号与摘要
- `list_tags`        混合树列表（目录/标签合并、scope 递归、q 过滤、分页）
- `export_tags`      目录作用域/名称筛选导出 zip（xlsx+xml，共享归档构建器）
- `get_node`         节点详情（目录或标签，含别名与根→节点路径）
- `set_tag_status`   标签定义生命周期状态切换（activate/deactivate 共用，
  写 tag_change_log）
- `delete_tag`       删除标签定义 + 自下而上剪除空祖先目录（引用 409）
- `library_stats`    标签库统计（目录/标签/可扩展目录/范围与类型分布）
- `library_tree`     目录树（dir_type/applicable_scope/标签数）
- `library_tags`     标签列表（目录/关键字过滤）
- `ingest_preview`   最近入库记录（tag_change_log）

别名解析裁定（Task 9 遗留顺带）：展示层原私有 `_parse_aliases`（仅 JSON、
坏 JSON 返 []）退役，统一改调 `services.tags.library.parse_aliases`——坏
JSON 逗号回落（与 ingest 落库侧同一份解析器，行为变化经裁定与 Task 9 的
统一同向）。
"""

import json
from datetime import datetime, timezone

from fastapi import HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, aliased

from tagplatform.capabilities.storage.models import (
    AssetTag,
    PreAssetTag,
    TagCatalog,
    TagChangeLog,
    TagDefine,
    TagImportTask,
)
from tagplatform.orchestration._shared import ImportSummary, _build_export_archive
from tagplatform.services.content.ingest import recent_ingest
from tagplatform.services.tags.governance import run_governance
from tagplatform.services.tags.library import parse_aliases, try_load_library
from tagplatform.tasks.tag_import import TagImportFileError, parse_upload


# ---------- 响应模型 ----------

class TagNodeOut(BaseModel):
    """混合树节点（目录与标签统一形状，前端消费契约）。

    id 对目录是 dir_id、对标签是 tag_id；kind 区分节点种类（dir|tag）；
    dir_type 仅目录有值（dir_static|dir_dynamic|dir_asset，标签为 null）；
    目录无 status 列恒为 null（标签 active|deprecated）；has_children 对
    目录 = 存在子目录或目录下挂有标签（与列表过滤无关、按全库判定），
    标签恒 False；aliases 列表视图即展开（v1 列表不展开别名的约定随统一
    节点契约退役）。
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


class ImportResponse(BaseModel):
    """导入响应：本批导入任务号 + 结果摘要（v1 batch_id 字段退役）。"""

    task_id: str
    summary: ImportSummary


# ---------- 内部辅助 ----------

def _like_pattern(q: str) -> str:
    """q → LIKE 子串模式：通配符按字面转义，保证纯子串语义。"""
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _get_define_or_404(tag_id: str, session: Session) -> TagDefine:
    """生命周期目标解析：仅 tag_define；目录 id → 404（文案含"目录"）。"""
    define = session.get(TagDefine, tag_id)
    if define is not None:
        return define
    if session.get(TagCatalog, tag_id) is not None:
        raise HTTPException(status_code=404, detail=f"目录不支持生命周期操作：{tag_id}")
    raise HTTPException(status_code=404, detail=f"标签不存在：{tag_id}")


def _library_or_404(session: Session):
    library = try_load_library(session)
    if library is None:
        raise HTTPException(status_code=503, detail="标签库未就绪（tag_catalog / tag_define 不可读）")
    return library


# ---------- 用例：标签导入/查询/生命周期/导出 ----------

def import_tag_file(
    session: Session, embedder, data: bytes, file_name: str
) -> ImportResponse:
    """上传标签 Excel/XML：解析 → 治理双表入库 → 返回导入任务号与结果摘要。

    解析失败（TagImportFileError）→ 422；治理流水线（embed + DB）为阻塞
    操作，接入层以同步 def 走线程池不占事件循环。
    """
    try:
        parsed = parse_upload(data, file_name)
    except TagImportFileError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    result = run_governance(parsed.rows, session, embedder, file_name=file_name,
                            parse_errors=parsed.errors)
    task = session.get(TagImportTask, result.task_id)
    return ImportResponse(
        task_id=result.task_id,
        summary=ImportSummary.model_validate_json(task.result_summary),
    )


def list_tags(
    session: Session,
    q: str | None,
    parent_id: str | None,
    root: bool,
    scope_id: str | None,
    kind: str | None,
    limit: int,
    offset: int,
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
            has_children=d.dir_id in occupied, aliases=parse_aliases(d.aliases),
        )
        for d in dir_rows
    ]
    tag_nodes = [
        TagNodeOut(
            id=t.tag_id, name=t.tag_name, kind="tag", dir_type=None,
            description=t.description, parent_id=t.dir_id, status=t.status,
            has_children=False, aliases=parse_aliases(t.aliases),
        )
        for t in tag_rows
    ]
    nodes = (sorted(dir_nodes, key=lambda n: n.name)
             + sorted(tag_nodes, key=lambda n: n.name))
    return TagListResponse(items=nodes[offset:offset + limit])


def export_tags(
    session: Session, q: str | None, scope_id: str | None
) -> Response:
    """导出当前目录作用域或名称筛选命中的目录及其全部标签（zip）。

    叶子 = tag_define 行（v2 目录不再是节点，全部标签即叶子），祖先路径 =
    tag_catalog 链；scope_id 为目录时导出该目录及其子树下全部标签（含直挂
    标签，v1「该目录及其全部标签」语义）；q 命中目录导出其子树叶子、命中
    标签导出该标签本身；未知目录 → 404。
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


def get_node(session: Session, node_id: str) -> TagNodeDetail:
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
            has_children=has_children, aliases=parse_aliases(d.aliases),
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
            has_children=False, aliases=parse_aliases(tag.aliases),
            path=[d.dir_name for d in dir_chain] + [tag.tag_name],
            path_ids=[d.dir_id for d in dir_chain] + [tag.tag_id],
        )

    raise HTTPException(status_code=404, detail=f"标签或目录不存在：{node_id}")


def set_tag_status(
    session: Session, tag_id: str, status: str, action: str
) -> TagNodeDetail:
    """改标签定义状态并写变更日志；同状态重复调用早返回不重复记日志。

    状态字面量 v2 统一 deprecated（v1 "deprecate" 退役，doc §3.3）；action
    沿 v1 枚举现值：停用记 deprecate、启用记 update（operator 沿 v1 记
    governance——治理/生命周期为平台自身操作）。
    """
    define = _get_define_or_404(tag_id, session)
    if define.status == status:
        return get_node(session, tag_id)
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
    return get_node(session, tag_id)


def delete_tag(session: Session, tag_id: str) -> TagDeleteResponse:
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


# ---------- 用例：标签库统计视图（原 api/routers/library.py）----------

def library_stats(session: Session) -> dict:
    """库统计：目录 37 / 标签 514 / 可扩展目录 16 这类数字。

    dynamic_dirs 统计可扩充目录（dir_dynamic + dir_asset）。
    """
    library = _library_or_404(session)
    dirs = library.dirs()
    tags = library.tags()
    dynamic = [d for d in dirs if d.allows_new_tags]
    scopes: dict[str, int] = {}
    for directory in dirs:
        scopes[directory.applicable_scope] = scopes.get(directory.applicable_scope, 0) + 1
    tag_types: dict[str, int] = {}
    for tag in tags:
        tag_types[tag.tag_type] = tag_types.get(tag.tag_type, 0) + 1
    return {
        "version": library.version,
        "source": library.source,
        "dirs": len(dirs),
        "tags": len(tags),
        "dynamic_dirs": len(dynamic),
        "scopes": scopes,
        "tag_types": tag_types,
    }


def library_tree(session: Session) -> dict:
    """目录树：每个节点带 dir_type / applicable_scope / 直接标签数 / 子节点。"""
    library = _library_or_404(session)
    counts: dict[str, int] = {}
    for tag in library.tags():
        counts[tag.dir_id] = counts.get(tag.dir_id, 0) + 1

    def node(directory) -> dict:
        children = library.children(directory.dir_id)
        return {
            "dir_id": directory.dir_id,
            "dir_name": directory.dir_name,
            "level": directory.level,
            "dir_type": directory.dir_type,
            "applicable_scope": directory.applicable_scope,
            "allows_new_tags": directory.allows_new_tags,
            "description": directory.description,
            "path": "/".join(library.path_of(directory.dir_id)),
            "tag_count": counts.get(directory.dir_id, 0),
            "children": [node(child) for child in children],
        }

    return {"version": library.version,
            "dirs": [node(root) for root in library.root_dirs()]}


def library_tags(
    session: Session, dir_id: str | None, query: str | None, limit: int
) -> dict:
    """标签列表：可按目录（含子目录）与名称关键字过滤。"""
    library = _library_or_404(session)
    tags = library.tags_in_dirs([dir_id]) if dir_id else library.tags()
    if query:
        needle = query.strip()
        tags = [t for t in tags if needle in t.tag_name
                or any(needle in alias for alias in t.aliases)]
    rows = []
    for tag in tags[:limit]:
        directory = library.directory(tag.dir_id)
        rows.append({
            "tag_id": tag.tag_id,
            "tag_name": tag.tag_name,
            "tag_type": tag.tag_type,
            "dir_id": tag.dir_id,
            "dir_name": directory.dir_name if directory else None,
            "path": "/".join(library.path_of(tag.dir_id)),
            "description": tag.description,
            "aliases": list(tag.aliases),
            "status": tag.status,
        })
    return {"total": len(tags), "items": rows}


def ingest_preview(session: Session, limit: int) -> dict:
    """最近入库记录：审核写入的新标签与别名（读 tag_change_log）。"""
    definitions, aliases = recent_ingest(session, limit=limit)
    return {"tag_define": definitions, "aliases": aliases,
            "tag_define_count": len(definitions),
            "alias_count": len(aliases)}
