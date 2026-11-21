"""Rehearse the 0016 multimodal-evidence closed prompt upgrade."""

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
    db_path = tmp_path / "rehearsal_0016.db"
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


def test_0016_updates_and_downgrades_closed_prompt(rehearsal):
    cfg, db_path = rehearsal
    command.upgrade(cfg, "0015")

    # 模拟 0015 生产库的真实状态：v1 文案仍是 0013 的扁平四键契约
    # （新装库在 0013 步就会写入现行常量、已含新文案，0016 的 UPDATE
    # 幂等覆盖同一行，无需区分两种来源）。
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "UPDATE prompt_template SET template_text=?"
            " WHERE mode='closed' AND version=1",
            ("legacy closed prompt {content}",),
        )
        conn.commit()

    # 升级止于 0016（head 已是 0018——prompt_template 被 drop）
    command.upgrade(cfg, "0016")
    upgraded = _closed_prompt(db_path)
    # 三模态嵌套契约进文案：模态节与各模态独立评分的说明
    assert "content、scene、frame" in upgraded
    assert "evidence_strength" in upgraded
    assert "evidences" in upgraded
    assert "剧情整体描述" in upgraded
    assert "关键帧图片" in upgraded
    # 占位符保留（runner 仍渲染 {content}）
    assert "{content}" in upgraded
    assert "legacy closed prompt" not in upgraded

    command.downgrade(cfg, "0015")
    downgraded = _closed_prompt(db_path)
    # 回滚恢复 0015 存量的扁平四键文案（含独立维度措辞）
    assert "两个独立维度" in downgraded
    assert "evidences：证据摘要数组" in downgraded
    assert "content、scene、frame" not in downgraded


def test_0016_upgrade_from_older_revision_is_idempotent(rehearsal):
    """从 0012 直升 0016：0013 与 0016 两次 UPDATE 叠加，最终为 0016 文案。"""
    cfg, db_path = rehearsal
    command.upgrade(cfg, "0012")
    command.upgrade(cfg, "0016")
    with sqlite3.connect(str(db_path)) as conn:
        assert conn.execute(
            "SELECT version_num FROM alembic_version").fetchone() == ("0016",)
    upgraded = _closed_prompt(db_path)
    assert "content、scene、frame" in upgraded
    assert "{content}" in upgraded
