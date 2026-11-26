"""Prompt 本地化语义测试 + 0018 drop 迁移验证（四层重构 Task 3）。

prompt 不再外置 DB（prompt_template 表由 0018 drop，git 即版本）：
get_prompt 返回本地常量（closed/open 均只含 {content}——closed 的候选
标签由 ClosedTagProducer 按目录分组注入，不经占位符），render_prompt 是
纯文本渲染。迁移测试以子进程真跑 alembic：head 处表已 drop；一级
downgrade 重建并从 templates.py 常量重新播种（回滚路径可用性）。
"""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from tagplatform.capabilities.prompts import (
    get_prompt,
    render_prompt,
)

# 仓库根（alembic.ini 所在目录）：子进程迁移测试的工作目录
_REPO_ROOT = Path(__file__).resolve().parents[1]


def test_get_prompt_seeded():
    t = get_prompt("closed")
    assert "{content}" in t
    assert "{candidates}" not in t
    with pytest.raises(ValueError):
        get_prompt("open")


def test_get_prompt_missing_mode():
    with pytest.raises(ValueError):
        get_prompt("nope")


def test_render_prompt_fills_placeholders():
    text = render_prompt(get_prompt("closed"), content="内容X")
    assert "内容X" in text and "{" not in text.replace("{{", "")


def _alembic(args: list[str], db_path: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "TAGPLT_DATABASE_PATH": str(db_path)}
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _table_names(db_path: Path) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        return {
            row[0] for row in
            conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()


def test_migration_head_drops_prompt_template(tmp_path):
    """子进程 alembic upgrade head → prompt_template 被 0018 drop（本地化后无表）。"""
    db_path = tmp_path / "m.db"
    result = _alembic(["upgrade", "head"], db_path)
    assert result.returncode == 0, result.stderr
    assert "prompt_template" not in _table_names(db_path)


def test_migration_downgrade_reseeds_from_local_constants(tmp_path):
    """0018 downgrade：重建表并回填 closed/open v1 激活行。

    回滚路径（先 downgrade 再回滚旧镜像）可用性：行数 = closed/open v1
    激活各一条，其中 closed 来自 CLOSED_TEMPLATE_TEXT，open 来自 0018 内联冻结字面量。
    """
    from tagplatform.capabilities.prompts import CLOSED_TEMPLATE_TEXT

    db_path = tmp_path / "m.db"
    assert _alembic(["upgrade", "0018"], db_path).returncode == 0
    result = _alembic(["downgrade", "0017"], db_path)
    assert result.returncode == 0, result.stderr

    conn = sqlite3.connect(db_path)
    try:
        # 建表 DDL 契约钉死 (mode, version) 唯一：UNIQUE 约束随表重建
        ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='prompt_template'"
        ).fetchone()[0]
        assert "UNIQUE" in ddl
        rows = conn.execute(
            "SELECT mode, version, is_active, template_text"
            " FROM prompt_template ORDER BY mode"
        ).fetchall()
        assert rows[0] == ("closed", 1, 1, CLOSED_TEMPLATE_TEXT)
        assert rows[1][:3] == ("open", 1, 1)
        assert "{content}" in rows[1][3]
    finally:
        conn.close()
