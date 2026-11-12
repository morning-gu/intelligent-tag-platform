"""0011 删除 pre_asset_tag.predicted_dir_id 迁移的带数据彩排测试。

主线（doc §8 带数据彩排法，与 test_migration_0010 同款范式，方向相反）：

- 临时文件库升到 0010 → 插资产 / 目录 / 一条带预测快照的待审标签行
  → upgrade head（0011）→ 断言：
  * `predicted_dir_id` 列已删除、`idx_pre_tag_predicted_dir` 索引已删除；
  * 老行其余字段原样保留（`candidate_dir_id` 等业务列不受退役影响）；
- downgrade 回 0010：列与索引重建（照抄 0010 upgrade），快照数据不复活
  （全 NULL——退役即丢快照，与 0010 downgrade 的已知代价同口径）；
- 再升回 head：可重入，列仍然不在。

与 0010 测试的关键差异：0010 断言"加列后老行快照为 NULL"，0011 断言
"删列后业务列不受牵连"——迁移的安全性诉求从"补数据"变成"不伤数据"。
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
    """带数据彩排环境：临时文件库 + alembic Config（0010 测试同款）。"""
    db_path = tmp_path / "rehearsal_0011.db"
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


def _seed_0010_data(db_path: Path) -> None:
    """0010 口径下的资产 / 目录 / 一条带预测快照的待审标签行。"""
    conn = _connect(db_path)
    try:
        conn.execute(
            "INSERT INTO media_asset(asset_id, asset_type, title, created_at)"
            " VALUES('a1', 'movie', '测试影片', ?)", (TS,))
        conn.execute(
            "INSERT INTO tag_catalog (dir_id, dir_name, dir_type, description,"
            " parent_id, applicable_scope, created_at, updated_at)"
            " VALUES ('dir_scene', '场景', 'dir_dynamic', '动态场景目录', NULL,"
            " 'content', ?, ?)",
            (TS, TS),
        )
        conn.execute(
            "INSERT INTO pre_asset_tag(asset_id, target_type, target_id,"
            " candidate_name, candidate_dir_id, predicted_dir_id, origin,"
            " confidence, evidence_json, review_status, created_at, updated_at)"
            " VALUES('a1', 'content', 'a1', '悬疑', 'dir_scene', 'dir_scene',"
            " 'open', 0.8, '[]', 'pending', ?, ?)", (TS, TS))
        conn.commit()
    finally:
        conn.close()


def _column_names(db_path: Path, table: str) -> set[str]:
    conn = _connect(db_path)
    try:
        return {row[1] for row in conn.execute(f"pragma table_info({table})")}
    finally:
        conn.close()


def _index_names(db_path: Path) -> set[str]:
    conn = _connect(db_path)
    try:
        return {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
    finally:
        conn.close()


def _row(db_path: Path, *columns: str) -> tuple:
    conn = _connect(db_path)
    try:
        cols = ", ".join(columns)
        return conn.execute(
            f"SELECT {cols} FROM pre_asset_tag WHERE asset_id = 'a1'"
        ).fetchone()
    finally:
        conn.close()


def test_0011_upgrade_rehearsal(rehearsal):
    """带数据彩排：0010 带数据升 0011——删列 / 删索引 / 业务列不受牵连。"""
    cfg, db_path = rehearsal
    command.upgrade(cfg, "0010")
    _seed_0010_data(db_path)

    command.upgrade(cfg, "head")

    # ---- 列与索引消失 ----
    assert "predicted_dir_id" not in _column_names(db_path, "pre_asset_tag")
    assert "idx_pre_tag_predicted_dir" not in _index_names(db_path)
    # ---- 其余索引仍在（退役不牵连邻居）----
    assert {"idx_pre_tag_asset", "idx_pre_tag_review"} <= _index_names(db_path)
    # ---- 业务列不受牵连 ----
    assert _row(db_path, "candidate_name", "candidate_dir_id",
                "review_status") == ("悬疑", "dir_scene", "pending")


def test_0011_downgrade_and_reupgrade(rehearsal):
    """downgrade 回 0010 重建列/索引（快照不复活）；再升回 head 可重入。"""
    cfg, db_path = rehearsal
    command.upgrade(cfg, "0010")
    _seed_0010_data(db_path)
    command.upgrade(cfg, "head")

    command.downgrade(cfg, "0010")

    assert "predicted_dir_id" in _column_names(db_path, "pre_asset_tag")
    assert "idx_pre_tag_predicted_dir" in _index_names(db_path)
    # 已知代价：退役即丢快照，重建后为 NULL（与 0010 downgrade 同口径）
    assert _row(db_path, "candidate_dir_id", "predicted_dir_id") \
        == ("dir_scene", None)

    command.upgrade(cfg, "head")

    assert "predicted_dir_id" not in _column_names(db_path, "pre_asset_tag")
    assert "idx_pre_tag_predicted_dir" not in _index_names(db_path)
    assert _row(db_path, "candidate_name") == ("悬疑",)
