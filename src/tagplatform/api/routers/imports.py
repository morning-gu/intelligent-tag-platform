"""`/api/v1/import/tasks` 薄路由：导入任务列表、详情、导出与撤销。

接入层只做协议转换：multipart/query 解析 → 调编排 → 返回。任务查询、
result_summary JSON 解析、批次反查与撤销事务全部在
tagplatform.orchestration.imports（用例清单见该模块 docstring）。

v1 的 /api/v1/import/batches 前缀与 batch_id 字段随表结构 v2 对照表退役
（import_batch → tag_import_task，doc §2.1/§3.7）。导出 zip 与 /tags/export
共用 orchestration._shared._build_export_archive（格式逐字沿 v1）。HTTP
错误映射（404 未知任务、409 已撤销/非 completed/引用冲突）随逻辑在编排
层原样保留。
"""

from fastapi import APIRouter, Depends, File, UploadFile
from sqlalchemy.orm import Session

from tagplatform.api import get_session, get_tag_import_executor
from tagplatform.orchestration import imports as imports_orch
from tagplatform.orchestration.imports import ImportTaskDetailOut, ImportTaskOut
from tagplatform.tasks.tag_import import TagImportExecutor

router = APIRouter(prefix="/api/v1/import/tasks")


@router.post("", status_code=202, response_model=ImportTaskOut)
def create_import_task(
    file: UploadFile = File(...),
    executor: TagImportExecutor = Depends(get_tag_import_executor),
) -> ImportTaskOut:
    """接收上传并建立任务，解析和入库由后台线程执行。"""
    return imports_orch.create_import_task(
        executor, file.file, file.filename or "upload")


@router.get("", response_model=list[ImportTaskOut])
def list_import_tasks(session: Session = Depends(get_session)) -> list[ImportTaskOut]:
    """全部导入任务，按 created_at 降序（最近导入在前）。"""
    return imports_orch.list_import_tasks(session)


@router.get("/{task_id}", response_model=ImportTaskDetailOut)
def get_import_task(task_id: str, session: Session = Depends(get_session)) -> ImportTaskDetailOut:
    """导入任务详情：result_summary JSON 解析为结果摘要；未知 task_id → 404。"""
    return imports_orch.get_import_task(session, task_id)


@router.get("/{task_id}/export")
def export_import_task(task_id: str, session: Session = Depends(get_session)):
    """导出导入批次的标签为 zip（批内标签行 = 叶子，祖先路径 = 目录链）。"""
    return imports_orch.export_import_task(session, task_id)


@router.post("/{task_id}/revoke", response_model=ImportTaskOut)
def revoke_import_task(task_id: str, session: Session = Depends(get_session)) -> ImportTaskOut:
    """撤销导入任务（删批内新建、复活行还原；引用冲突 409）。"""
    return imports_orch.revoke_import_task(session, task_id)
