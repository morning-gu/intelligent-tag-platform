"""0019 tag_catalog.applicable_scope 迁移的带数据彩排测试。

- 临时文件库升到 0018 → 手工插目录（含历史逗号组合行与合法单值行）与标签行
  → upgrade head（0019）→ 断言：
  * 历史组合值归一为首个合法值，合法值与 NULL 原样保留；
  * applicable_scope 列默认值恢复为 'content'；
  * CHECK 收紧为 content / scene / character，拒绝组合值与非法值；
  * 自引用外键与两个索引完整到位。
- downgrade 回 0018 再升回 head 验证可重入。
"""

import os
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import IntegrityError

from tagplatform.capabilities.storage import create_engine_for, make_session_factory
from tagplatform.capabilities.storage.models import TagCatalog, TagDefine
from tagplatform.settings import get_settings

REPO_ROOT = Path(__file__).resolve().parents[1]
TS = "2026-12-01 08:00:00"


@pytest.fixture
def rehearsal(tmp_path):
    db_path = tmp_path / "rehearsal_0019.db"
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    old = os.environ.get("TAGPLT_DATABASE_PATH")
    os.environ["TAGPLT_DATABASE_PATH"] = str(db_path)
    get_settings.cache_clear()
    try:
        yield cfg, db_path
    finally:
        if old is None:
            os.environ.pop("TAGPLT_DATABASE_PATH", None)
        else:
            os.environ["TAGPLT_DATABASE_PATH"] = old
        get_settings.cache_clear()


def _table_columns(db_path: Path, table: str) -> list[tuple]:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    finally:
        conn.close()
    return [(r[1], r[2], r[3], r[4]) for r in rows]


def _create_table_sql(db_path: Path, table: str) -> str | None:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def _index_names(db_path: Path) -> set[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
    finally:
        conn.close()
    return {r[0] for r in rows}


def _fk_check(db_path: Path) -> list[tuple]:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        conn.close()
    return rows


def test_0019_upgrade_rehearsal(rehearsal):
    cfg, db_path = rehearsal
    command.upgrade(cfg, "0018")

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        "INSERT INTO tag_catalog (dir_id, dir_name, dir_type, description,"
        " parent_id, applicable_scope, created_at, updated_at) VALUES"
        " ('dir_root', '根', 'dir_static', '根', NULL, 'content', ?, ?),"
        " ('dir_scene', '场景', 'dir_dynamic', '场', 'dir_root', 'scene', ?, ?),"
        " ('dir_combo', '历史组合', 'dir_asset', '旧', 'dir_root', 'scene,character', ?, ?),"
        " ('dir_all', '历史全选', 'dir_dynamic', '旧', 'dir_root', 'content,scene,character', ?, ?)",
        (TS, TS, TS, TS, TS, TS, TS, TS),
    )
    conn.execute(
        "INSERT INTO tag_define (tag_id, tag_name, tag_type, description, dir_id,"
        " status, created_at, updated_at) VALUES"
        " ('tag_1', '标签', 'val_fixed', '释义', 'dir_scene', 'active', ?, ?)",
        (TS, TS),
    )
    conn.commit()
    conn.close()

    command.upgrade(cfg, "head")

    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT dir_id, dir_type, applicable_scope FROM tag_catalog ORDER BY dir_id"
        ).fetchall()
        assert rows == [
            ("dir_all", "dir_dynamic", "content"),
            ("dir_combo", "dir_asset", "scene"),
            ("dir_root", "dir_static", "content"),
            ("dir_scene", "dir_dynamic", "scene"),
        ]
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            "INSERT INTO tag_catalog (dir_id, dir_name, dir_type, description, parent_id)"
            " VALUES ('dir_default', '默认', 'dir_static', '', NULL)"
        )
        scope = conn.execute(
            "SELECT applicable_scope FROM tag_catalog WHERE dir_id='dir_default'"
        ).fetchone()[0]
        assert scope == "content"
        conn.rollback()
    finally:
        conn.close()

    cols = _table_columns(db_path, "tag_catalog")
    assert ("applicable_scope", "TEXT", 0, "'content'") in cols
    sql = _create_table_sql(db_path, "tag_catalog")
    assert sql is not None
    assert "applicable_scope IN ('content', 'scene', 'character')" in sql
    assert "'content,scene'" not in sql
    assert {"idx_tag_catalog_type", "idx_tag_catalog_scope"} <= _index_names(db_path)

    engine = create_engine_for(str(db_path))
    try:
        with make_session_factory(engine)() as s:
            s.add(TagCatalog(dir_id="dir_char", dir_name="人物", dir_type="dir_dynamic",
                             description="", parent_id="dir_root", applicable_scope="character"))
            s.commit()

            for bad in ("content,scene", "bogus"):
                s.add(TagCatalog(dir_id=f"dir_{bad}", dir_name=f"坏_{bad}",
                                 dir_type="dir_static", description="", applicable_scope=bad))
                with pytest.raises(IntegrityError):
                    s.commit()
                s.rollback()
    finally:
        engine.dispose()

    assert _fk_check(db_path) == []


def test_0019_downgrade_and_roundtrip(rehearsal):
    cfg, db_path = rehearsal
    command.upgrade(cfg, "head")

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        "INSERT INTO tag_catalog (dir_id, dir_name, dir_type, description,"
        " parent_id, applicable_scope) VALUES"
        " ('dir_root', '类型', 'dir_static', '', NULL, 'content')"
    )
    conn.commit()
    conn.close()

    command.downgrade(cfg, "0018")
    sql_0018 = _create_table_sql(db_path, "tag_catalog")
    assert sql_0018 is not None and "'content,scene'" in sql_0018

    command.upgrade(cfg, "head")
    sql_head = _create_table_sql(db_path, "tag_catalog")
    assert sql_head is not None
    assert "applicable_scope IN ('content', 'scene', 'character')" in sql_head
    assert ("applicable_scope", "TEXT", 0, "'content'") in _table_columns(db_path, "tag_catalog")

    engine = create_engine_for(str(db_path))
    try:
        with make_session_factory(engine)() as s:
            s.add(TagDefine(tag_id="tag_x", tag_name="标签", tag_type="val_fixed",
                            description="", dir_id="dir_root"))
            s.commit()
    finally:
        engine.dispose()

    assert _fk_check(db_path) == []
