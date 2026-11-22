"""`/api/v1/tags` 薄路由：混合树查询、详情、生命周期与导出 + 标签导入。

接入层只做协议转换：请求参数解析（query/form）→ 调编排 → 返回。读路径
SQL、树合并/分页、导出归档与生命周期写库全部在
tagplatform.orchestration.tags（用例清单见该模块 docstring）。

v2 双表口径（doc §3.2/3.3/3.6）：目录树在 tag_catalog、叶子标签在
tag_define（经 dir_id 挂目录）、正式挂载在 asset_tag。导出 zip（xlsx+xml）
格式逐字沿 v1，归档构建器在 orchestration._shared（导入批次导出共用）。
HTTP 错误映射（404 未知 tag_id/目录、409 引用冲突、422 解析失败/坏表头）
随逻辑在编排层原样保留。
"""

from fastapi import APIRouter, Depends, File, Query, UploadFile
from sqlalchemy.orm import Session

from tagplatform.api import get_embedder, get_session
from tagplatform.orchestration import tags as tags_orch
from tagplatform.orchestration.tags import (
    ImportResponse,
    TagDeleteResponse,
    TagListResponse,
    TagNodeDetail,
)

router = APIRouter(prefix="/api/v1/tags")


@router.post("/import", response_model=ImportResponse)
def import_tags(
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
    embedder=Depends(get_embedder),
) -> ImportResponse:
    """上传标签 Excel/XML：解析 → 治理双表入库 → 任务号与结果摘要。

    同步 def：治理流水线（embed + DB）为阻塞操作，走线程池不占事件循环。
    """
    return tags_orch.import_tag_file(
        session, embedder, file.file.read(), file.filename or "upload")


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
    """混合树列表：目录与标签合并（目录先、名字序），分页在合并列表上切。"""
    return tags_orch.list_tags(
        session, q, parent_id, root, scope_id, kind, limit, offset)


@router.get("/export")
def export_tags(
    q: str | None = Query(None),
    scope_id: str | None = Query(None),
    session: Session = Depends(get_session),
):
    """导出当前目录作用域或名称筛选命中的目录及其全部标签（zip）。"""
    return tags_orch.export_tags(session, q, scope_id)


@router.get("/{node_id}", response_model=TagNodeDetail)
def get_node(node_id: str, session: Session = Depends(get_session)) -> TagNodeDetail:
    """节点详情：id 可为 dir_id（目录）或 tag_id（标签），含别名与根→节点路径。"""
    return tags_orch.get_node(session, node_id)


@router.post("/{tag_id}/deactivate", response_model=TagNodeDetail)
def deactivate_tag(tag_id: str, session: Session = Depends(get_session)) -> TagNodeDetail:
    return tags_orch.set_tag_status(session, tag_id, "deprecated", "deprecate")


@router.post("/{tag_id}/activate", response_model=TagNodeDetail)
def activate_tag(tag_id: str, session: Session = Depends(get_session)) -> TagNodeDetail:
    return tags_orch.set_tag_status(session, tag_id, "active", "update")


@router.delete("/{tag_id}", response_model=TagDeleteResponse)
def delete_tag(tag_id: str, session: Session = Depends(get_session)) -> TagDeleteResponse:
    """删除标签定义（引用 409；删除后自下而上剪除空祖先目录）。"""
    return tags_orch.delete_tag(session, tag_id)
