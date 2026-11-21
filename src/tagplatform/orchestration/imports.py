"""标签导入任务域编排（接收/列表/详情/导出/撤销）——orchestration/imports 域。

用例清单（原 api/routers/imports.py 的读路径 SQL 与装配逻辑，Task 11 原样
下沉；批次反查仍是全表扫描 + JSON 文本 Python 侧过滤，Task 12 SQL 化）：

- `create_import_task`  接收上传建任务（解析入库由后台线程执行）
- `list_import_tasks`   全部导入任务（created_at 降序）
- `get_import_task`     任务详情（result_summary JSON 解析为结构化摘要）
- `export_import_task`  批次标签导出 zip（共享 _shared._build_export_archive）
- `revoke_import_task`  撤销导入任务（删批内新建、复活行还原、引用 409）

错误映射随逻辑下沉本层原样保留：404（未知任务）、409（已撤销/非 completed/
被 asset_tag 或 pre_asset_tag 引用/目录被他批子节点引用）。
"""

import json

from fastapi import HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from tagplatform.orchestration._shared import (
    ImportSummary,
    UtcDatetime,
    _build_export_archive,
)
from tagplatform.capabilities.storage.models import (
    AssetTag,
    PreAssetTag,
    TagCatalog,
    TagChangeLog,
    TagDefine,
    TagImportTask,
)
from tagplatform.tasks.tag_import import TagImportExecutor


# ---------- 响应模型 ----------

class ImportTaskOut(BaseModel):
    """导入任务列表项（v1 BatchOut 的 task_id 形状）。"""

    task_id: str
    file_name: str
    status: str
    created_at: UtcDatetime | None = None


class ImportTaskDetailOut(ImportTaskOut):
    """导入任务详情：含 result_summary 解析后的结果摘要（无/损坏时为 None）。"""

    summary: ImportSummary | None = None
    error: str | None = None


# ---------- 内部辅助（批次反查）----------

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


# ---------- 用例 ----------

def create_import_task(
    executor: TagImportExecutor, fileobj, filename: str
) -> ImportTaskOut:
    """接收上传并建立任务，解析和入库由后台线程执行。"""
    task = executor.submit(filename, fileobj.read())
    return ImportTaskOut(task_id=task.task_id, file_name=task.file_name,
                         status=task.status, created_at=task.created_at)


def list_import_tasks(session: Session) -> list[ImportTaskOut]:
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


def get_import_task(session: Session, task_id: str) -> ImportTaskDetailOut:
    """导入任务详情：result_summary JSON 解析为结果摘要；未知 task_id → 404。"""
    task = session.get(TagImportTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"导入任务不存在：{task_id}")
    summary: ImportSummary | None = None
    error: str | None = None
    if task.result_summary:
        try:
            data = json.loads(task.result_summary)
            if task.status == "failed":
                if isinstance(data, dict) and isinstance(data.get("error"), str):
                    error = data["error"]
            else:
                summary = ImportSummary.model_validate(data)
        except (json.JSONDecodeError, ValidationError):
            # 摘要 JSON 损坏时降级为 None，不影响任务元数据返回
            summary = None
    return ImportTaskDetailOut(
        task_id=task.task_id,
        file_name=task.file_name,
        status=task.status,
        created_at=task.created_at,
        summary=summary,
        error=error,
    )


def export_import_task(session: Session, task_id: str) -> Response:
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


def revoke_import_task(session: Session, task_id: str) -> ImportTaskOut:
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
