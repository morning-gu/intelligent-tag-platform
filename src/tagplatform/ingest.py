"""入库动作：审核通过的新标签 / 新别名，直接落 `tag_define`。

v2 起标签库就是 `tag_catalog` / `tag_define` 两张正式表（早期 JSON 库已删除），
入库一律真写：

- 新词审核通过 → 插入 `tag_define`（同目录同名幂等），记 `tag_change_log(create)`；
- 合并到既有标签 → 别名追加进 `tag_define.aliases`，记 `tag_change_log(update)`。

`/ingest/preview` 对应改成"最近入库记录"的只读视图（读变更日志），字段与
旧的预览行保持一致，前端与调用方无需改动。

分层红线：本模块不 import fastapi（设计文档 §9.4）。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from tagplatform.db.models import TagChangeLog, TagDefine
from tagplatform.library import TagLibrary

TAG_TYPES = ("val_fixed", "val_generalized")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aliases(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if str(v).strip()]
    text = str(value).strip()
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return []
        return [str(v) for v in parsed if str(v).strip()]
    return [part.strip() for part in text.split(",") if part.strip()]


def _dump(aliases: list[str]) -> str | None:
    return json.dumps(aliases, ensure_ascii=False) if aliases else None


def _next_tag_id(session: Session, dir_id: str, tag_name: str) -> str:
    """新标签 id：`tag_{sha1(dir_id/tag_name)[:8]}`（稳定、可复现）。"""
    digest = hashlib.sha1(f"{dir_id}/{tag_name}".encode("utf-8")).hexdigest()[:8]
    tag_id = f"tag_{digest}"
    if session.get(TagDefine, tag_id) is not None:
        tag_id = f"tag_{digest}{uuid.uuid4().hex[:4]}"
    return tag_id


def _row_dict(row: TagDefine, dir_name: str | None, source: dict | None,
              created: bool) -> dict:
    return {
        "tag_id": row.tag_id,
        "dir_id": row.dir_id,
        "dir_name": dir_name,
        "tag_name": row.tag_name,
        "tag_type": row.tag_type,
        "description": row.description or "",
        "aliases": _aliases(row.aliases),
        "status": row.status,
        "created": created,
        "source": source or {},
    }


def _log(session: Session, tag_id: str, action: str, *,
         before: dict | None = None, after: dict | None = None,
         operator: str | None = None) -> None:
    session.add(TagChangeLog(
        tag_id=tag_id, action=action,
        before_json=json.dumps(before, ensure_ascii=False) if before else None,
        after_json=json.dumps(after, ensure_ascii=False) if after else None,
        operator=operator, created_at=_now(),
    ))


def record_tag_definition(
    session: Session,
    library: TagLibrary | None,
    *,
    tag_name: str,
    dir_id: str,
    description: str = "",
    tag_type: str = "val_fixed",
    aliases: list[str] | None = None,
    source: dict | None = None,
) -> dict:
    """新词入库：插一行 `tag_define`；同目录同名幂等返回既有行。

    调用方负责 commit（审核动作与结果行同事务）。`source` 里的 reviewer
    会记进 `tag_change_log.operator`，便于追溯"谁批进来的"。
    """
    name = tag_name.strip()
    if not name:
        raise ValueError("标签名不能为空")
    directory = library.directory(dir_id) if library is not None else None
    if library is not None and directory is None:
        raise ValueError(f"目录不存在: {dir_id}")

    existing = (session.query(TagDefine)
                .filter(TagDefine.dir_id == dir_id, TagDefine.tag_name == name)
                .one_or_none())
    if existing is not None:
        return _row_dict(existing, directory.dir_name if directory else None,
                         source, created=False)

    alias_list = [a.strip() for a in (aliases or []) if a.strip()]
    row = TagDefine(
        tag_id=_next_tag_id(session, dir_id, name),
        dir_id=dir_id,
        tag_name=name,
        tag_type=tag_type if tag_type in TAG_TYPES else "val_fixed",
        description=(description or "").strip(),
        aliases=_dump(alias_list),
        status="active",
        created_at=_now(),
        updated_at=_now(),
    )
    session.add(row)
    session.flush()
    payload = _row_dict(row, directory.dir_name if directory else None,
                        source, created=True)
    _log(session, row.tag_id, "create", after=payload,
         operator=(source or {}).get("reviewer"))
    return payload


def record_alias(session: Session, *, tag_id: str, alias: str,
                 source: dict | None = None) -> bool:
    """库内标签新增别名：写进 `tag_define.aliases`；已存在返回 False。"""
    alias = alias.strip()
    if not alias:
        return False
    row = session.get(TagDefine, tag_id)
    if row is None:
        raise ValueError(f"标签不存在: {tag_id}")
    current = _aliases(row.aliases)
    if alias in current:
        return False
    updated = [*current, alias]
    before = {"aliases": current}
    row.aliases = _dump(updated)
    row.updated_at = _now()
    _log(session, tag_id, "update", before=before,
         after={"tag_id": row.tag_id, "tag_name": row.tag_name,
                "dir_id": row.dir_id, "aliases": updated},
         operator=(source or {}).get("reviewer"))
    session.flush()
    return True


def recent_ingest(session: Session, limit: int = 50) -> tuple[list[dict], list[dict]]:
    """最近入库记录：新标签（create）与别名（update/merge），读变更日志。"""
    logs = (session.query(TagChangeLog)
            .filter(TagChangeLog.action.in_(("create", "update", "merge")))
            .order_by(TagChangeLog.log_id.desc()).limit(limit).all())
    definitions: list[dict] = []
    aliases: list[dict] = []
    for log in logs:
        try:
            payload = json.loads(log.after_json) if log.after_json else {}
        except json.JSONDecodeError:
            payload = {}
        if log.action == "create":
            definitions.append({
                "tag_id": log.tag_id,
                "dir_id": payload.get("dir_id"),
                "dir_name": payload.get("dir_name"),
                "tag_name": payload.get("tag_name"),
                "tag_type": payload.get("tag_type"),
                "description": payload.get("description") or "",
                "aliases": payload.get("aliases") or [],
                "status": payload.get("status") or "active",
                "source": payload.get("source") or {},
            })
            continue
        for alias in payload.get("aliases") or []:
            aliases.append({
                "tag_id": log.tag_id,
                "tag_name": payload.get("tag_name"),
                "dir_id": payload.get("dir_id"),
                "alias": alias,
                "source": {"operator": log.operator},
            })
    return definitions, aliases
