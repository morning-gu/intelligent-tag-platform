"""0017 导入批键列迁移排练（Task 12）。

契约：
- upgrade：tag_change_log 加 `task_id` 可空列 + `idx_change_log_task` 索引，
  并回填存量行（从 action='import' 行的 after_json 解析 task_id 写列——
  坏 JSON/非 import 行留 NULL）；
- downgrade：drop 索引与列（可回）；
- fresh 链：空库 0001→0018 全链走通，0018 与 0017 各可 downgrade 一级。

批次反查的消费方（orchestration/imports.py）语义由 test_api_imports
既有 revoke/export 用例锁定；本文件只验迁移本身。
"""

import os
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from tagplatform.settings import get_settings

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def rehearsal(tmp_path):
    """临时文件库 + alembic 配置（TAGPLT_DATABASE_PATH 指向临时库）。"""
    db_path = tmp_path / "rehearsal_0017.db"
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


def _columns(db_path: Path) -> set[str]:
    with sqlite3.connect(str(db_path)) as conn:
        return {row[1] for row in conn.execute("PRAGMA table_info(tag_change_log)")}


def _indexes(db_path: Path) -> set[str]:
    with sqlite3.connect(str(db_path)) as conn:
        return {row[1] for row in conn.execute("PRAGMA index_list(tag_change_log)")}


def test_0017_backfills_task_id_from_after_json_and_downgrades(rehearsal):
    """upgrade 回填存量行（after_json.task_id → 列），downgrade 可回。"""
    cfg, db_path = rehearsal
    command.upgrade(cfg, "0016")

    # 模拟 0016 生产库存量：import 审计行只有 after_json（无 task_id 列）
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "INSERT INTO tag_change_log (tag_id, action, after_json, operator, created_at)"
            " VALUES ('tag_legacy', 'import', '{\"kind\": \"tag\", \"task_id\": \"task_old\"}',"
            " 'governance', '2026-11-01')")
        conn.execute(
            "INSERT INTO tag_change_log (tag_id, action, before_json, after_json)"
            " VALUES ('t_life', 'deprecate', '{\"status\": \"active\"}',"
            " '{\"status\": \"deprecated\"}')")  # 生命周期行：不回填
        conn.execute(
            "INSERT INTO tag_change_log (tag_id, action, after_json)"
            " VALUES ('t_bad', 'import', 'not-json')")  # 坏 JSON：不回填
        conn.commit()

    command.upgrade(cfg, "head")
    assert "task_id" in _columns(db_path)
    assert "idx_change_log_task" in _indexes(db_path)
    with sqlite3.connect(str(db_path)) as conn:
        rows = dict(conn.execute(
            "SELECT tag_id, task_id FROM tag_change_log").fetchall())
    assert rows["tag_legacy"] == "task_old"
    assert rows["t_life"] is None
    assert rows["t_bad"] is None

    command.downgrade(cfg, "0016")
    assert "task_id" not in _columns(db_path)
    assert "idx_change_log_task" not in _indexes(db_path)


def test_fresh_chain_upgrade_head_then_two_single_downgrades(rehearsal):
    """fresh 链：空库 0001→head（0019）全链走通；0019→0018→0017→0016 各可回一级。"""
    cfg, db_path = rehearsal
    command.upgrade(cfg, "head")

    def version() -> str:
        with sqlite3.connect(str(db_path)) as conn:
            return conn.execute(
                "SELECT version_num FROM alembic_version").fetchone()[0]

    assert version() == "0019"
    command.downgrade(cfg, "-1")
    assert version() == "0018"
    command.downgrade(cfg, "-1")
    assert version() == "0017"
    command.downgrade(cfg, "-1")
    assert version() == "0016"
