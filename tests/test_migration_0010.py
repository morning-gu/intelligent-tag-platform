"""0010 pre_asset_tag.predicted_dir_id 迁移的带数据彩排测试。

主线（doc §8 带数据彩排法，与 test_migration_0007/0008 同款范式）：

- 临时文件库升到 0009 → 插资产 / 目录 / 一条"已有人工归属"的待审标签行
  → upgrade head（0010）→ 断言：
  * `predicted_dir_id` 列存在、类型 TEXT、可空（ADD COLUMN 纯元数据，
    老行不需要回填）；
  * 老行全列原样保留，`candidate_dir_id` 不动、`predicted_dir_id` 为 NULL
    （"历史数据没有预测快照"是正确语义，不是漏填）；
  * `idx_pre_tag_predicted_dir` 建在 `predicted_dir_id` 上；
  * 该列**刻意不挂外键**——写入一个目录表里不存在的 dir_id 也必须成功
    （目录被治理删除后仍要留住历史预测）；
- downgrade 回 0009：列与索引消失、行数据保留；
- 再升回 head：可重入，且旧行 `predicted_dir_id` 仍是 NULL（列被删过，
  快照无法恢复——这是 downgrade 的已知代价，测试把这条代价钉死）。
"""

import os
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from tagplatform.settings import get_settings

REPO_ROOT = Path(__file__).resolve().parents[1]

TS = "2026-12-01 08:00:00"


@pytest.fixture
def rehearsal(tmp_path):
    """带数据彩排环境：临时文件库 + alembic Config（0007/0008 测试同款）。"""
    db_path = tmp_path / "rehearsal_0010.db"
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


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _seed_0009_data(db_path: Path) -> None:
    """0009 口径下的资产 / 目录 / 一条待审标签行（candidate_dir_id 已有人工值）。"""
    conn = _connect(db_path)
    try:
        conn.execute(
            "INSERT INTO media_asset (asset_id, asset_type, title, created_at)"
            " VALUES ('a1', 'movie', '测试影片', ?)",
            (TS,),
        )
        conn.execute(
            "INSERT INTO tag_catalog (dir_id, dir_name, dir_type, description,"
            " parent_id, applicable_scope, created_at, updated_at)"
            " VALUES ('dir_scene', '场景', 'dir_dynamic', '动态场景目录', NULL,"
            " 'content', ?, ?)",
            (TS, TS),
        )
        # 老行：生成时预测过归属，但 0009 还没有列可存；审核已改到 dir_scene
        conn.execute(
            "INSERT INTO pre_asset_tag (asset_id, target_type, target_id,"
            " candidate_name, candidate_dir_id, candidate_description, origin,"
            " review_status, created_at, updated_at)"
            " VALUES ('a1', 'content', 'a1', '悬疑', 'dir_scene', '悬念感',"
            " 'open', 'pending', ?, ?)",
            (TS, TS),
        )
        conn.commit()
    finally:
        conn.close()


def _table_columns(db_path: Path, table: str) -> list[tuple]:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    finally:
        conn.close()
    return [(r[1], r[2], r[3], r[4]) for r in rows]


def _column_names(db_path: Path, table: str) -> set[str]:
    return {c[0] for c in _table_columns(db_path, table)}


def _index_names(db_path: Path) -> set[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    finally:
        conn.close()
    return {r[0] for r in rows}


def _index_columns(db_path: Path, index: str) -> list[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(f"PRAGMA index_info({index})").fetchall()
    finally:
        conn.close()
    return [r[2] for r in rows]


def _fk_targets(db_path: Path, table: str) -> dict[str, str]:
    """列名 -> 外键目标表。用来证明 predicted_dir_id 没有外键。"""
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()
    finally:
        conn.close()
    return {r[3]: r[2] for r in rows}


def _row(db_path: Path, *columns: str) -> tuple:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            f"SELECT {', '.join(columns)} FROM pre_asset_tag"
            " WHERE asset_id = 'a1'"
        ).fetchone()
    finally:
        conn.close()
    return row


def test_0010_upgrade_rehearsal(rehearsal):
    """带数据彩排：0009 带数据升 0010——加列 / 老行不动 / 索引到位 / 无外键。"""
    cfg, db_path = rehearsal
    command.upgrade(cfg, "0009")
    _seed_0009_data(db_path)

    command.upgrade(cfg, "head")

    # ---- 列：存在、TEXT、可空、无默认值（老行不回填，语义就是"没预测过"）----
    columns = dict(
        (name, (decl_type, notnull, dflt))
        for name, decl_type, notnull, dflt in _table_columns(db_path, "pre_asset_tag")
        if name == "predicted_dir_id"
    )
    assert columns, "predicted_dir_id 未创建"
    assert columns["predicted_dir_id"][0] == "TEXT"
    assert columns["predicted_dir_id"][1] == 0  # notnull = 0
    assert columns["predicted_dir_id"][2] is None

    # ---- 数据：老行原样保留，candidate_dir_id 不动，快照为空 ----
    assert _row(
        db_path,
        "candidate_name",
        "candidate_dir_id",
        "candidate_description",
        "origin",
        "review_status",
        "predicted_dir_id",
    ) == ("悬疑", "dir_scene", "悬念感", "open", "pending", None)

    # ---- 索引：按列名建在 predicted_dir_id 上（与模型 __table_args__ 同名）----
    assert "idx_pre_tag_predicted_dir" in _index_names(db_path)
    assert _index_columns(db_path, "idx_pre_tag_predicted_dir") == ["predicted_dir_id"]

    # ---- 外键：candidate_dir_id 有、predicted_dir_id 刻意没有 ----
    fks = _fk_targets(db_path, "pre_asset_tag")
    assert fks.get("candidate_dir_id") == "tag_catalog"
    assert "predicted_dir_id" not in fks

    # ---- 无外键的实证：写入目录表里不存在的 dir_id 也必须成功 ----
    conn = _connect(db_path)
    try:
        conn.execute(
            "UPDATE pre_asset_tag SET predicted_dir_id = 'dir_ghost'"
            " WHERE asset_id = 'a1'"
        )
        conn.commit()
        assert conn.execute(
            "SELECT predicted_dir_id FROM pre_asset_tag WHERE asset_id = 'a1'"
        ).fetchone()[0] == "dir_ghost"
    finally:
        conn.close()


def test_0010_downgrade_and_reupgrade(rehearsal):
    """downgrade 回 0009 删列删索引、行保留；再升回 head 可重入。"""
    cfg, db_path = rehearsal
    command.upgrade(cfg, "0009")
    _seed_0009_data(db_path)
    command.upgrade(cfg, "head")

    command.downgrade(cfg, "0009")

    assert "predicted_dir_id" not in _column_names(db_path, "pre_asset_tag")
    assert "idx_pre_tag_predicted_dir" not in _index_names(db_path)
    # 行数据保留（downgrade 只删列，不删行）
    assert _row(db_path, "candidate_name", "candidate_dir_id") == ("悬疑", "dir_scene")

    command.upgrade(cfg, "head")

    assert "predicted_dir_id" in _column_names(db_path, "pre_asset_tag")
    assert "idx_pre_tag_predicted_dir" in _index_names(db_path)
    # 已知代价：列被删过一次，之前的快照无法恢复
    assert _row(db_path, "candidate_dir_id", "predicted_dir_id") == ("dir_scene", None)