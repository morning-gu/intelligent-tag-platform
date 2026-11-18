"""0013 closed prompt standardized scoring migration rehearsal."""

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

    command.upgrade(cfg, "head")
    upgraded = _closed_prompt(db_path)
    assert "applicability" in upgraded
    assert "evidence_strength" in upgraded
    assert "evidences" in upgraded
    assert "legacy closed prompt" not in upgraded

    command.downgrade(cfg, "0012")
    downgraded = _closed_prompt(db_path)
    assert "relevance：标签与内容的相关程度，0 到 1" in downgraded
    assert "applicability" not in downgraded
