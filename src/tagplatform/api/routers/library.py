"""`/api/v1/library` 与 `/api/v1/ingest` 薄路由：标签库视图与入库记录。

接入层只做协议转换：query 参数解析 → 调编排 → 返回。库统计/目录树/标签
列表/入库记录的装配逻辑全部在 tagplatform.orchestration.tags（标签域，
用例清单见该模块 docstring）：

- GET /library/tree     目录树（含 dir_type / applicable_scope / 标签数）
- GET /library/tags     标签列表（可按 dir_id 过滤，含目录与别名）
- GET /library/stats    库统计（目录数/标签数/可扩展目录数/范围分布）
- GET /ingest/preview   最近入库记录（新标签 + 新别名，读 tag_change_log）

只读，无副作用（入库由审核动作写入 tag_define）；库未就绪 → 503
（映射在编排层 _library_or_404）。
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from tagplatform.api import get_session
from tagplatform.orchestration import tags as tags_orch

router = APIRouter(prefix="/api/v1")


@router.get("/library/stats")
def library_stats(session: Session = Depends(get_session)) -> dict:
    """库统计：目录 37 / 标签 514 / 可扩展目录 16 这类数字。"""
    return tags_orch.library_stats(session)


@router.get("/library/tree")
def library_tree(session: Session = Depends(get_session)) -> dict:
    """目录树：每个节点带 dir_type / applicable_scope / 直接标签数 / 子节点。"""
    return tags_orch.library_tree(session)


@router.get("/library/tags")
def library_tags(dir_id: str | None = Query(None),
                 query: str | None = Query(None),
                 limit: int = Query(200, ge=1, le=2000),
                 session: Session = Depends(get_session)) -> dict:
    """标签列表：可按目录（含子目录）与名称关键字过滤。"""
    return tags_orch.library_tags(session, dir_id, query, limit)


@router.get("/ingest/preview")
def ingest_preview(limit: int = Query(50, ge=1, le=500),
                   session: Session = Depends(get_session)) -> dict:
    """最近入库记录：审核写入的新标签与别名（读 tag_change_log）。"""
    return tags_orch.ingest_preview(session, limit)
