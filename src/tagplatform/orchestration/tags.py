"""标签域编排：标签库查询/导入/生命周期/导出 + 标签库统计视图。

用例清单（原 api/routers/tags.py 与 api/routers/library.py 的读路径 SQL 与
装配逻辑，Task 11 原样下沉；list 分页已 SQL LIMIT/OFFSET 下推 + total 计
数、导出已分批/按需装配（产物字节不变），Task 12）：

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
from tagplatform.orchestration._shared import (
    ClearCountsOut,
    ClearPreviewOut,
    ClearResultOut,
    ImportSummary,
    _build_export_archive,
)
from tagplatform.services.content.ingest import recent_ingest
from tagplatform.services.content.reset import active_task_counts
from tagplatform.services.tags.governance import run_governance
from tagplatform.services.tags.library import parse_aliases, try_load_library
from tagplatform.services.tags.reset import clear_library
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
    """混合树列表响应（分页经 limit/offset，默认 100/0）。

    total（Task 12 白名单内唯一响应结构变化）：过滤后节点总数——分页切口
    前的候选集大小（kind/q/parent_id/scope_id 过滤口径内），SQL COUNT
    计算；前端同步在 Task 14。
    """

    items: list[TagNodeOut]
    total: int


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
    """混合树列表：目录与标签合并（目录先、名字序），SQL LIMIT/OFFSET 分页。

    - 无过滤：全部目录 + 全部标签；
    - root：仅顶层目录（标签必挂目录，不存在根标签）；
    - parent_id：该目录的子目录 + 直挂标签；
    - scope_id：递归子树（CTE 沿 v1 手法跨双表）——后代目录 + 后代目录下
      的标签；标签的后代以其目录的后代目录界定，直挂 scope 本体的标签
      不在结果内；
    - q：目录与标签的 name+aliases 子串（LIKE 转义沿 v1）；
    - kind：dir|tag 二值过滤；v1 的自由 type 分类随 tag_type 枚举化退役。

    分页（Task 12 白名单改造）：合并列表 = [目录段（名字序）] + [标签段
    （名字序）]，两段各自 SQL ORDER BY + OFFSET/LIMIT 切窗口（原实现两表
    全量 ORM 物化 + Python 合并排序后切片）；total = 两段过滤后计数之和
    （COUNT 下推 SQL），分页切口前口径。
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

    include_dirs = kind in (None, "dir")
    include_tags = kind in (None, "tag") and tag_query is not None

    # 分页切口前口径的总数：两段过滤后计数（COUNT 下推 SQL）
    dir_total = dir_query.count() if include_dirs else 0
    tag_total = tag_query.count() if include_tags else 0
    total = dir_total + tag_total

    # 窗口 [offset, offset+limit) 在两段上的落位：目录段在前，标签段接续
    dir_nodes: list[TagNodeOut] = []
    tag_nodes: list[TagNodeOut] = []
    if include_dirs and offset < dir_total:
        dir_rows: list[TagCatalog] = (
            dir_query.order_by(TagCatalog.dir_name)
            .offset(offset).limit(min(limit, dir_total - offset)).all()
        )
        # 目录 has_children 按全库判定（与本层过滤无关）：有子目录或挂有标签
        occupied: set[str] = {
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
    if include_tags:
        tag_window_start = max(0, offset - dir_total)
        tag_window_end = offset + limit - dir_total
        if tag_window_end > tag_window_start:
            tag_rows: list[TagDefine] = (
                tag_query.order_by(TagDefine.tag_name)
                .offset(tag_window_start).limit(tag_window_end - tag_window_start).all()
            )
            tag_nodes = [
                TagNodeOut(
                    id=t.tag_id, name=t.tag_name, kind="tag", dir_type=None,
                    description=t.description, parent_id=t.dir_id, status=t.status,
                    has_children=False, aliases=parse_aliases(t.aliases),
                )
                for t in tag_rows
            ]
    return TagListResponse(items=[*dir_nodes, *tag_nodes], total=total)


def _descendant_dir_ids(session: Session, frontier: set[str]) -> set[str]:
    """frontier 目录集的全部后代 dir_id（分层 SQL 扩散，目录树按需加载）。

    原 export_tags 的 Python BFS（全目录 ORM 物化 + children 森林 + 逐点
    下钻）改按层批量查 parent_id IN（Task 12 分批改造；与 list_tags 的
    递归 CTE 同语义，选代际扩散是为 q 命中目录集合并入同一遍查询）。
    """
    result: set[str] = set()
    pending = {d for d in frontier if d}
    while pending:
        rows = session.query(TagCatalog.dir_id).filter(
            TagCatalog.parent_id.in_(pending)).all()
        pending = {d for (d,) in rows if d not in result}
        result |= pending
    return result


def export_tags(
    session: Session, q: str | None, scope_id: str | None
) -> Response:
    """导出当前目录作用域或名称筛选命中的目录及其全部标签（zip）。

    叶子 = tag_define 行（v2 目录不再是节点，全部标签即叶子），祖先路径 =
    tag_catalog 链；scope_id 为目录时导出该目录及其子树下全部标签（含直挂
    标签，v1「该目录及其全部标签」语义）；q 命中目录导出其子树叶子、命中
    标签导出该标签本身；未知目录 → 404。

    分批/按需改造（Task 12，行为不变——产物由共享 _build_export_archive
    构造，选型说明见函数尾注）：两表全量 ORM 物化 + Python BFS 退役——
    - scope 子树：分层 SQL 扩散（_descendant_dir_ids）；
    - q 目录命中：仅投影 (dir_id, dir_name, aliases) 三列流式扫描 + Python
      casefold 判定（SQLite LIKE 无法等价 unicode casefold，为保选择语义
      逐字不变保留 Python 侧判定——轻量元组，非 ORM 实体）；
    - 叶子：按 tag_id 序分批（yield_per）仅物化选中行；
    - 祖先链：逐层按 PK 批量加载（只取叶子所需目录，非全树）。
    产物字节不变：xlsx/xml 归档仍由 _build_export_archive 全量内存构造
    （openpyxl/zipfile 无真流式出口，数据装配侧消除全量物化）。
    """
    if scope_id is not None:
        if session.get(TagCatalog, scope_id) is None:
            raise HTTPException(status_code=404, detail=f"目录不存在：{scope_id}")
        selected_dirs = {scope_id} | _descendant_dir_ids(session, {scope_id})

    keep_dirs: set[str] = set()
    if q:
        keyword = q.casefold()
        # q 目录命中：三列投影流式扫描（原实现全表 ORM 物化 + casefold）
        for dir_id, dir_name, aliases in (
            session.query(TagCatalog.dir_id, TagCatalog.dir_name,
                          TagCatalog.aliases).yield_per(500)
        ):
            if (keyword in dir_name.casefold()
                    or (aliases is not None and keyword in aliases.casefold())):
                keep_dirs.add(dir_id)
        keep_dirs |= _descendant_dir_ids(session, keep_dirs)
        if scope_id is not None:
            keep_dirs &= selected_dirs

    # 叶子选取：scope 过滤（SQL）∩（q 标签命中 ∪ q 目录命中子树）
    leaves_query = session.query(TagDefine).order_by(TagDefine.tag_id)
    if scope_id is not None:
        leaves_query = leaves_query.filter(TagDefine.dir_id.in_(selected_dirs))
    if q:
        keyword = q.casefold()
        # q 标签命中：三列投影流式扫描（scope 给定时只在候选目录内扫——
        # 命中目录域外的标签最终也会被 scope 交集排除，语义不变）
        tag_scan = session.query(TagDefine.tag_id, TagDefine.tag_name,
                                 TagDefine.aliases)
        if scope_id is not None:
            tag_scan = tag_scan.filter(TagDefine.dir_id.in_(selected_dirs))
        keep_tag_ids: set[str] = set()
        for tag_id, tag_name, aliases in tag_scan.yield_per(500):
            if (keyword in tag_name.casefold()
                    or (aliases is not None and keyword in aliases.casefold())):
                keep_tag_ids.add(tag_id)
        leaves_query = leaves_query.filter(or_(
            TagDefine.tag_id.in_(keep_tag_ids),
            TagDefine.dir_id.in_(keep_dirs),
        ))
    export_leaves: list[TagDefine] = list(leaves_query.yield_per(500))

    # 祖先链按需加载：逐层按 PK 批量取（只取叶子所需，非全树物化）
    dirs_by_id: dict[str, TagCatalog] = {}
    pending = {t.dir_id for t in export_leaves if t.dir_id}
    while pending:
        rows = session.query(TagCatalog).filter(
            TagCatalog.dir_id.in_(pending)).all()
        dirs_by_id.update((d.dir_id, d) for d in rows)
        pending = {d.parent_id for d in rows
                   if d.parent_id and d.parent_id not in dirs_by_id}

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


# ---------- clear-reset（2026-11-24 spec §3.2：③ 清空标签库 + 预览） ----------

def clear_tag_library(session: Session) -> ClearResultOut:
    """③ 清空标签库：级联全部标签数据与任务历史；批次/日志幸存。

    守卫（删除前）：running 生成/构建 + processing 导入 → 409
    （spec §3.3——只有正在执行的写方挡路，queued 在删除半径内）。
    """
    active = active_task_counts(session, None)
    running = active["running_generation"] + active["running_build"]
    if running > 0:
        raise HTTPException(
            status_code=409,
            detail=f"有 {running} 个生成/构建任务正在执行，请先等待其完成")
    if active["processing_import"] > 0:
        raise HTTPException(
            status_code=409,
            detail=f"有 {active['processing_import']} 个标签导入任务正在执行，"
                   "请先等待其完成")
    counts = clear_library(session)
    return ClearResultOut(ok=True, counts=ClearCountsOut(**counts))


def library_clear_preview(session: Session) -> ClearPreviewOut:
    """③ 计数预览（只读，dry_run 与实删同源计数）。"""
    counts = clear_library(session, dry_run=True)
    return ClearPreviewOut(scope="library", counts=ClearCountsOut(**counts))


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
