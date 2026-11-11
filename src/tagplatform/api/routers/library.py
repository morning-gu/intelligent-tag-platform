"""`/api/v1/library` 与 `/api/v1/ingest`：标签库（tag_catalog / tag_define）与入库记录。

- GET /library/tree     目录树（含 dir_type / applicable_scope / 标签数）
- GET /library/tags     标签列表（可按 dir_id 过滤，含目录与别名）
- GET /library/stats    库统计（目录数/标签数/可扩展目录数/范围分布）
- GET /ingest/preview   最近入库记录（新标签 + 新别名，读 tag_change_log）

只读，无副作用（入库由审核动作写入 tag_define）。
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from tagplatform.api import get_session
from tagplatform.ingest import recent_ingest
from tagplatform.library import try_load_library

router = APIRouter(prefix="/api/v1")


def _library_or_404(session: Session):
    library = try_load_library(session)
    if library is None:
        raise HTTPException(status_code=503, detail="标签库未就绪（tag_catalog / tag_define 不可读）")
    return library


@router.get("/library/stats")
def library_stats(session: Session = Depends(get_session)) -> dict:
    """库统计：目录 37 / 标签 514 / 可扩展目录 16 这类数字。"""
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


@router.get("/library/tree")
def library_tree(session: Session = Depends(get_session)) -> dict:
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


@router.get("/library/tags")
def library_tags(dir_id: str | None = Query(None),
                 query: str | None = Query(None),
                 limit: int = Query(200, ge=1, le=2000),
                 session: Session = Depends(get_session)) -> dict:
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


@router.get("/ingest/preview")
def ingest_preview(limit: int = Query(50, ge=1, le=500),
                   session: Session = Depends(get_session)) -> dict:
    """最近入库记录：审核写入的新标签与别名（读 tag_change_log）。"""
    definitions, aliases = recent_ingest(session, limit=limit)
    return {"tag_define": definitions, "aliases": aliases,
            "tag_define_count": len(definitions),
            "alias_count": len(aliases)}
