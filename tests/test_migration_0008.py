"""0008 tag_catalog.applicable_scope 组合值迁移的带数据彩排测试。

主线（doc §8 带数据彩排法，test_migration_0007 同款范式）：
- 临时文件库升到 0007 → 手工插目录父/子 + 标签行（含单值 scope 'content'，
  0007 口径下合法）→ upgrade head（0008）→ 断言：
  * 目录行全列原样保留（INSERT SELECT 数据搬迁）；
  * applicable_scope 列默认值变 'content,scene,character'（新插入不填
    scope 的行走全选默认）；
  * CHECK 扩为 7 组合——'content,scene' 等组合行可插、'bogus' 仍被拒；
  * 自引用外键经 RENAME 后仍成立（PRAGMA foreign_key_check 为空）；
  * 索引 idx_tag_catalog_type / idx_tag_catalog_scope 重建到位。
- downgrade 回 0007：CHECK 回单值口径（组合行不能再插）、行数据保留、
  再 round-trip 升回 head 验证可重入。
"""

import os
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError
from alembic import command
from alembic.config import Config

from tagplatform.db import create_engine_for, make_session_factory
from tagplatform.db.models import TagCatalog, TagDefine
from tagplatform.settings import get_settings

REPO_ROOT = Path(__file__).resolve().parents[1]

TS = "2026-12-01 08:00:00"


@pytest.fixture
def rehearsal(tmp_path):
    """带数据彩排环境：临时文件库 + alembic Config（0007 测试同款）。"""
    db_path = tmp_path / "rehearsal.db"
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


def _seed_0007_data(db_path: Path) -> None:
    """0007 口径下的目录父/子 + 标签行（scope 单值 'content' 合法）。"""
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        "INSERT INTO tag_catalog (dir_id, dir_name, dir_type, description,"
        " parent_id, applicable_scope, created_at, updated_at)"
        " VALUES ('dir_root', '类型', 'dir_static', '根目录', NULL, 'content', ?, ?)",
        (TS, TS),
    )
    conn.execute(
        "INSERT INTO tag_catalog (dir_id, dir_name, dir_type, description,"
        " parent_id, applicable_scope, created_at, updated_at)"
        " VALUES ('dir_comedy', '喜剧', 'dir_dynamic', '二级目录', 'dir_root',"
        " 'content', ?, ?)",
        (TS, TS),
    )
    conn.execute(
        "INSERT INTO tag_define (tag_id, tag_name, tag_type, description, dir_id,"
        " status, created_at, updated_at)"
        " VALUES ('tag_comedy', '喜剧', 'val_fixed', '搞笑', 'dir_comedy',"
        " 'active', ?, ?)",
        (TS, TS),
    )
    conn.commit()
    conn.close()


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
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
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


def test_0008_upgrade_rehearsal(rehearsal):
    """带数据彩排：0007 带数据升 0008——数据保留 / 默认值全选 / CHECK 扩组合。"""
    cfg, db_path = rehearsal
    command.upgrade(cfg, "0007")
    _seed_0007_data(db_path)

    command.upgrade(cfg, "head")

    # ---- 数据：目录父/子两行全列原样保留（INSERT SELECT 搬迁）----
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT dir_id, dir_name, dir_type, description, parent_id,"
            " applicable_scope, created_at, updated_at FROM tag_catalog"
            " ORDER BY dir_id"
        ).fetchall()
        assert rows == [
            ("dir_comedy", "喜剧", "dir_dynamic", "二级目录", "dir_root",
             "content", TS, TS),
            ("dir_root", "类型", "dir_static", "根目录", None,
             "content", TS, TS),
        ]
        # 标签行不受本迁移影响
        count = conn.execute("SELECT COUNT(*) FROM tag_define").fetchone()[0]
        assert count == 1

        # ---- 默认值：新插入不填 scope 的行走全选默认 ----
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            "INSERT INTO tag_catalog (dir_id, dir_name, dir_type, description,"
            " parent_id) VALUES ('dir_default', '默认', 'dir_static', '', NULL)"
        )
        scope = conn.execute(
            "SELECT applicable_scope FROM tag_catalog WHERE dir_id='dir_default'"
        ).fetchone()[0]
        assert scope == "content,scene,character"
        conn.rollback()
    finally:
        conn.close()

    # ---- 结构：列规格不变、CHECK 扩 7 组合、索引重建 ----
    cols = _table_columns(db_path, "tag_catalog")
    assert ("applicable_scope", "TEXT", 0,
            "'content,scene,character'") in cols
    sql = _create_table_sql(db_path, "tag_catalog")
    assert sql is not None
    assert (
        "applicable_scope IN ('content', 'scene', 'character', "
        "'content,scene', 'content,character', 'scene,character', "
        "'content,scene,character')"
    ) in sql
    assert "CONSTRAINT ux_tag_catalog_parent_name UNIQUE (parent_id, dir_name)" in sql
    assert {"idx_tag_catalog_type", "idx_tag_catalog_scope"} <= _index_names(db_path)

    # ---- 行为：组合行可插、非法值被拒、自引用外键仍成立 ----
    engine = create_engine_for(str(db_path))
    try:
        with make_session_factory(engine)() as s:
            s.add(TagCatalog(dir_id="dir_combo", dir_name="组合目录",
                             dir_type="dir_dynamic", description="",
                             parent_id="dir_root",
                             applicable_scope="content,scene"))
            s.commit()

            s.add(TagCatalog(dir_id="dir_bad", dir_name="坏目录",
                             dir_type="dir_static", description="",
                             applicable_scope="bogus"))
            with pytest.raises(IntegrityError):
                s.commit()
            s.rollback()
    finally:
        engine.dispose()

    assert _fk_check(db_path) == []


def test_0008_downgrade_and_roundtrip(rehearsal):
    """downgrade 回 0007：CHECK 回单值口径、行保留、再升回 head 可重入。"""
    cfg, db_path = rehearsal
    command.upgrade(cfg, "head")

    # head 口径下插组合行 + 单值行
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        "INSERT INTO tag_catalog (dir_id, dir_name, dir_type, description,"
        " parent_id, applicable_scope) VALUES"
        " ('dir_root', '类型', 'dir_static', '', NULL, 'content'),"
        " ('dir_combo', '组合', 'dir_dynamic', '', 'dir_root', 'content,scene')"
    )
    conn.commit()
    conn.close()

    command.downgrade(cfg, "0007")

    # 行数据保留（降级时组合值归一为首 token：'content,scene' → 'content'）
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT dir_id, applicable_scope FROM tag_catalog"
            " ORDER BY dir_id"
        ).fetchall()
        assert rows == [("dir_combo", "content"), ("dir_root", "content")]
        # CHECK 回单值口径：新插组合行被拒
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO tag_catalog (dir_id, dir_name, dir_type, description,"
                " parent_id, applicable_scope) VALUES"
                " ('dir_bad', '坏', 'dir_static', '', NULL, 'scene,character')"
            )
            conn.commit()
        conn.rollback()
    finally:
        conn.close()

    # round-trip：再升回 head，CHECK/默认值/索引全部回到位，组合行仍可插
    command.upgrade(cfg, "head")
    sql = _create_table_sql(db_path, "tag_catalog")
    assert sql is not None
    assert "'content,scene'" in sql
    assert ("applicable_scope", "TEXT", 0,
            "'content,scene,character'") in _table_columns(db_path, "tag_catalog")
    assert {"idx_tag_catalog_type", "idx_tag_catalog_scope"} <= _index_names(db_path)

    engine = create_engine_for(str(db_path))
    try:
        with make_session_factory(engine)() as s:
            s.add(TagDefine(tag_id="tag_x", tag_name="标签", tag_type="val_fixed",
                            description="", dir_id="dir_combo"))
            s.commit()
    finally:
        engine.dispose()

    assert _fk_check(db_path) == []
