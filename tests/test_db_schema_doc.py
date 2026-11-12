"""设计文档 §3.1 / §3.10 的建表约定守卫。

迁移只跑一次；这里按文档逐条断言"库里真的
是这么建的"，防止后续改动（改模型、加迁移）悄悄把约束/索引丢掉：

- `media_asset.asset_type` CHECK(movie|series|scene)、parent/type 两个索引；
- `asset_build_task.status` CHECK(queued|running|succeeded|failed)、
  asset/status 两个索引、total_scenes/done_scenes 默认 0；
- 两张表的 created_at 都有 DEFAULT（文档的 DEFAULT now()，SQLite 即
  CURRENT_TIMESTAMP）。
"""

from __future__ import annotations

import sqlite3

import pytest

from tagplatform.db import create_engine_for
from tagplatform.db.models import Base


@pytest.fixture()
def sqlite_file(tmp_path):
    """按模型建表后的真实 SQLite 文件库（要读 sqlite_master，不能用内存库）。"""
    path = tmp_path / "schema.db"
    engine = create_engine_for(str(path))
    Base.metadata.create_all(engine)
    yield path
    engine.dispose()


def _ddl(conn: sqlite3.Connection, table: str) -> str:
    row = conn.execute(
        "select sql from sqlite_master where type='table' and name=?", (table,)
    ).fetchone()
    assert row is not None, f"{table} 表不存在"
    return row[0]


def _indexes(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"pragma index_list({table})")}


def test_media_asset_matches_doc(sqlite_file):
    conn = sqlite3.connect(sqlite_file)
    ddl = _ddl(conn, "media_asset")
    assert "ck_media_asset_asset_type" in ddl
    assert "CHECK (asset_type IN ('movie', 'series', 'scene'))" in ddl
    assert "created_at DATETIME DEFAULT CURRENT_TIMESTAMP" in ddl
    indexes = _indexes(conn, "media_asset")
    assert {"idx_media_asset_parent", "idx_media_asset_type"} <= indexes
    conn.close()


def test_asset_build_task_matches_doc(sqlite_file):
    conn = sqlite3.connect(sqlite_file)
    ddl = _ddl(conn, "asset_build_task")
    assert "ck_asset_build_task_status" in ddl
    assert (
        "CHECK (status IN ('queued', 'running', 'succeeded', 'failed'))" in ddl
    )
    assert "total_scenes INTEGER DEFAULT 0 NOT NULL" in ddl
    assert "done_scenes INTEGER DEFAULT 0 NOT NULL" in ddl
    assert "created_at DATETIME DEFAULT CURRENT_TIMESTAMP" in ddl
    indexes = _indexes(conn, "asset_build_task")
    assert {"idx_build_task_asset", "idx_build_task_status"} <= indexes
    conn.close()


def test_checks_actually_reject(sqlite_file):
    """约束不是摆设：非法枚举值必须被 SQLite 拒绝。"""
    conn = sqlite3.connect(sqlite_file)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "insert into media_asset(asset_id, asset_type, title) "
            "values('a_bad', 'film', 'x')"
        )
    conn.execute(
        "insert into media_asset(asset_id, asset_type, title) "
        "values('a_ok', 'movie', 'x')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "insert into asset_build_task(task_id, asset_id, movie_id, status) "
            "values('t_bad', 'a_ok', 'm', 'done')"
        )
    conn.execute(
        "insert into asset_build_task(task_id, asset_id, movie_id, status) "
        "values('t_ok', 'a_ok', 'm', 'queued')"
    )
    conn.close()
