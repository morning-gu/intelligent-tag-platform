"""Alembic 迁移全链与带数据升级排练测试。

覆盖：
- 空库 0001 → head 全链走通，核心表齐全、user_account 与 ORM 对齐、FK 无违例；
- v1（0006）带数据升至 head：旧业务表清除，user_account 与父/子 media_asset
  原样保留，v2 约束/部分唯一索引生效；
- 0017：tag_change_log.task_id 回填与回滚；
- 0019：tag_catalog.applicable_scope 历史组合值归一、CHECK 收紧、回滚重入。
"""

import os
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import IntegrityError

from tagplatform.capabilities.storage import create_engine_for, make_session_factory
from tagplatform.capabilities.storage.models import (
    MediaAsset,
    PreAssetTag,
    TagCatalog,
    TagDefine,
    UserAccount,
)
from tagplatform.settings import get_settings

REPO_ROOT = Path(__file__).resolve().parents[1]
TS = "2026-12-01 08:00:00"

GONE_V1_TABLES = {"review_record", "media_context", "import_batch", "generation_task"}
EXPECTED_HEAD_TABLES = {
    "media_asset",
    "asset_build_task",
    "tag_catalog",
    "tag_define",
    "tag_generation_task",
    "pre_asset_tag",
    "asset_tag",
    "tag_import_task",
    "tag_change_log",
    "user_account",
}


@pytest.fixture
def rehearsal(tmp_path):
    """临时文件库 + alembic Config（隔离 TAGPLT_DATABASE_PATH 与设置缓存）。"""
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


def _table_names(db_path: Path) -> set[str]:
    with sqlite3.connect(str(db_path)) as conn:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _table_columns(db_path: Path, table: str) -> list[tuple]:
    with sqlite3.connect(str(db_path)) as conn:
        return [(r[1], r[2], r[3], r[4]) for r in conn.execute(f"PRAGMA table_info({table})")]


def _create_table_sql(db_path: Path, table: str) -> str | None:
    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
    return row[0] if row else None


def _index_names(db_path: Path) -> set[str]:
    with sqlite3.connect(str(db_path)) as conn:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}


def _fk_check(db_path: Path) -> list[tuple]:
    with sqlite3.connect(str(db_path)) as conn:
        return conn.execute("PRAGMA foreign_key_check").fetchall()


def test_fresh_db_full_chain(rehearsal):
    """空库 0001 → head：核心表齐备、prompt_template 已移除、user_account 与模型对齐。"""
    cfg, db_path = rehearsal
    command.upgrade(cfg, "head")

    names = _table_names(db_path)
    assert not (GONE_V1_TABLES & names)
    assert EXPECTED_HEAD_TABLES <= names, f"缺表: {EXPECTED_HEAD_TABLES - names}"
    assert "prompt_template" not in names

    user_cols = {c[0] for c in _table_columns(db_path, "user_account")}
    assert user_cols == {c.name for c in UserAccount.__table__.columns}
    assert "UNIQUE" in (_create_table_sql(db_path, "user_account") or "")
    assert _fk_check(db_path) == []


def test_v1_0006_upgrade_to_head_preserves_assets_and_users(rehearsal):
    """带数据彩排：0006 存量账号与父/子资产升至 head 后保留，v2 约束到位。"""
    cfg, db_path = rehearsal
    command.upgrade(cfg, "0006")

    ts = "2026-11-01 08:00:00"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        "INSERT INTO user_account (user_id, username, password_hash, role, created_at)"
        " VALUES ('user_admin01', 'admin', 'pbkdf2$200000$salt$hash', 'admin', ?)",
        (ts,),
    )
    conn.execute(
        "INSERT INTO media_asset (asset_id, asset_type, parent_id, title, duration_ms,"
        " video_uri, created_at)"
        " VALUES ('asset_movie_001', 'movie', NULL, '彩排影片', 120000, '/media/demo.mp4', ?)",
        (ts,),
    )
    conn.execute(
        "INSERT INTO media_asset (asset_id, asset_type, parent_id, title, duration_ms,"
        " video_uri, created_at)"
        " VALUES ('asset_scene_001', 'scene', 'asset_movie_001', '彩排场景一', NULL, NULL, ?)",
        (ts,),
    )
    conn.commit()
    conn.close()

    command.upgrade(cfg, "head")

    names = _table_names(db_path)
    assert not (GONE_V1_TABLES & names)
    assert EXPECTED_HEAD_TABLES <= names

    with sqlite3.connect(str(db_path)) as conn:
        assert conn.execute(
            "SELECT user_id, username, role FROM user_account"
        ).fetchall() == [("user_admin01", "admin", "admin")]
        assert conn.execute(
            "SELECT asset_id, asset_type, parent_id, title, content_json, source"
            " FROM media_asset ORDER BY asset_id"
        ).fetchall() == [
            ("asset_movie_001", "movie", None, "彩排影片", None, None),
            ("asset_scene_001", "scene", "asset_movie_001", "彩排场景一", None, None),
        ]

    # 部分唯一索引与外键约束在升级后的库上真实生效
    engine = create_engine_for(str(db_path))
    try:
        with make_session_factory(engine)() as s:
            s.add(PreAssetTag(asset_id="asset_movie_001", target_id="probe_cand",
                              candidate_name="新词", origin="open"))
            s.commit()
            s.add(PreAssetTag(asset_id="asset_movie_001", target_id="probe_cand",
                              candidate_name="新词", origin="open"))
            with pytest.raises(IntegrityError):
                s.commit()
            s.rollback()
    finally:
        engine.dispose()

    assert _fk_check(db_path) == []


def test_0017_backfills_task_id_from_after_json_and_downgrades(rehearsal):
    """0017 upgrade 回填存量 import 行的 task_id，downgrade 可回。"""
    cfg, db_path = rehearsal
    command.upgrade(cfg, "0016")

    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "INSERT INTO tag_change_log (tag_id, action, after_json, operator, created_at)"
            " VALUES ('tag_legacy', 'import', '{\"kind\": \"tag\", \"task_id\": \"task_old\"}',"
            " 'governance', '2026-11-01')"
        )
        conn.execute(
            "INSERT INTO tag_change_log (tag_id, action, before_json, after_json)"
            " VALUES ('t_life', 'deprecate', '{\"status\": \"active\"}',"
            " '{\"status\": \"deprecated\"}')"
        )
        conn.execute(
            "INSERT INTO tag_change_log (tag_id, action, after_json)"
            " VALUES ('t_bad', 'import', 'not-json')"
        )
        conn.commit()

    command.upgrade(cfg, "head")
    assert "task_id" in {c[0] for c in _table_columns(db_path, "tag_change_log")}
    assert "idx_change_log_task" in _index_names(db_path)
    with sqlite3.connect(str(db_path)) as conn:
        rows = dict(conn.execute("SELECT tag_id, task_id FROM tag_change_log").fetchall())
    assert rows == {"tag_legacy": "task_old", "t_life": None, "t_bad": None}

    command.downgrade(cfg, "0016")
    assert "task_id" not in {c[0] for c in _table_columns(db_path, "tag_change_log")}
    assert "idx_change_log_task" not in _index_names(db_path)


def test_0019_upgrade_rehearsal(rehearsal):
    """0019 upgrade：历史逗号组合 scope 归一为首合法值，收紧 CHECK 并保留索引与外键。"""
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

    with sqlite3.connect(str(db_path)) as conn:
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
        assert conn.execute(
            "SELECT applicable_scope FROM tag_catalog WHERE dir_id='dir_default'"
        ).fetchone()[0] == "content"
        conn.rollback()

    assert ("applicable_scope", "TEXT", 0, "'content'") in _table_columns(db_path, "tag_catalog")
    sql = _create_table_sql(db_path, "tag_catalog") or ""
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
    """0019 downgrade 回 0018 再升回 head 可重入且外键完整。"""
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
    assert "'content,scene'" in (_create_table_sql(db_path, "tag_catalog") or "")

    command.upgrade(cfg, "head")
    sql_head = _create_table_sql(db_path, "tag_catalog") or ""
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
