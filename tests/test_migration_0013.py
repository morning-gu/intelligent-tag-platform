"""Rehearse closed prompt and tag quality upgrades around revision 0013."""

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
    db_path = tmp_path / "rehearsal_0013.db"
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


def _closed_prompt(db_path: Path) -> str:
    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute(
            "SELECT template_text FROM prompt_template"
            " WHERE mode='closed' AND version=1"
        ).fetchone()
    assert row is not None
    return row[0]


def test_0013_updates_and_downgrades_closed_prompt(rehearsal):
    cfg, db_path = rehearsal
    command.upgrade(cfg, "0012")

    # Simulate the existing v1 prompt stored by a database at revision 0012.
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "UPDATE prompt_template SET template_text=?"
            " WHERE mode='closed' AND version=1",
            ("legacy closed prompt {content}",),
        )
        conn.commit()

    # 升级止于 0016（本文件彩排 0013/0014/0016 的 prompt 演进；
    # head 已是 0018——prompt_template 被 drop，查询对象不存在）
    command.upgrade(cfg, "0016")
    upgraded = _closed_prompt(db_path)
    assert "applicability" in upgraded
    assert "evidence_strength" in upgraded
    assert "evidences" in upgraded
    assert "legacy closed prompt" not in upgraded

    command.downgrade(cfg, "0012")
    downgraded = _closed_prompt(db_path)
    assert "relevance：标签与内容的相关程度，0 到 1" in downgraded
    assert "applicability" not in downgraded


def test_0014_upgrades_main_0013(rehearsal):
    cfg, db_path = rehearsal
    command.upgrade(cfg, "0013")
    closed_prompt = _closed_prompt(db_path)
    command.upgrade(cfg, "0016")
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == ("0016",)
        assert conn.execute("SELECT COUNT(*) FROM tag_quality_evaluation").fetchone() == (0,)
    assert _closed_prompt(db_path) == closed_prompt


def test_0014_adopts_pilot_0013_without_losing_evaluations(rehearsal):
    cfg, db_path = rehearsal
    command.upgrade(cfg, "0016")  # 止于 0016：head(0018) 已 drop prompt_template
    with sqlite3.connect(db_path) as conn:
        # Pilot 0013 used the same quality schema as main 0014, before the closed prompt upgrade.
        conn.execute("UPDATE alembic_version SET version_num='0013'")
        conn.execute("UPDATE prompt_template SET template_text='pilot closed prompt {content}'"
                     " WHERE mode='closed' AND version=1")
        conn.execute("INSERT INTO media_asset (asset_id, asset_type, title) VALUES ('a1', 'movie', 'Film')")
        conn.execute("INSERT INTO tag_generation_task"
                     " (task_id, asset_id, mode, status, batch_id)"
                     " VALUES ('g1', 'a1', 'open', 'succeeded', 'b1')")
        conn.execute("INSERT INTO tag_quality_evaluation"
                     " (task_id, status, model, prompt_version, labels, scores, frame_paths)"
                     ' VALUES (?, ?, ?, ?, ?, ?, ?)',
                     ("g1", "completed", "test", "tag-quality-v3", '[]',
                      '{"1":{"value":{"score":2,"reason":"evidence"}}}', '{}'))

    command.upgrade(cfg, "0016")

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == ("0016",)
        assert conn.execute("SELECT batch_id FROM tag_generation_task WHERE task_id='g1'").fetchone() == ("b1",)
        assert conn.execute("SELECT prompt_version, scores FROM tag_quality_evaluation"
                            " WHERE task_id='g1'").fetchone() == (
                                "tag-quality-v3", '{"1":{"value":{"score":2,"reason":"evidence"}}}',
                            )
    assert "applicability" in _closed_prompt(db_path)
