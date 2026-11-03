"""Prompt 模板层测试：种子激活模板、缺模板报错、占位符渲染、迁移含种子。

种子契约是阶段 2 生成流水线（Task 5 产出器 / Task 8 执行器）的咬合面：
closed/open 两个 mode 各有一条 v1 激活模板，closed 含 {content}/{candidates}
占位符，open 仅含 {content}。迁移测试以子进程真跑 alembic，验证种子确实
随迁移落库（而非只存在于 ORM 元数据）。
"""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from tagplatform.prompts import get_active_prompt, render_prompt

# 仓库根（alembic.ini 所在目录）：子进程迁移测试的工作目录
_REPO_ROOT = Path(__file__).resolve().parents[1]


def test_get_active_prompt_seeded(session):
    for mode in ("closed", "open"):
        t = get_active_prompt(session, mode)
        assert t.version == 1 and t.is_active and "{content}" in t.template_text
    assert "{candidates}" in get_active_prompt(session, "closed").template_text


def test_get_active_prompt_missing_mode(session):
    with pytest.raises(ValueError):
        get_active_prompt(session, "nope")


def test_render_prompt_fills_placeholders(session):
    text = render_prompt(
        get_active_prompt(session, "closed"), content="内容X", candidates="候选Y"
    )
    assert "内容X" in text and "候选Y" in text and "{" not in text.replace("{{", "")


def test_migration_upgrades_with_seed(tmp_path):
    """子进程 alembic upgrade head → prompt_template 建表 → 种子两行、各 mode v1 激活。"""
    db_path = tmp_path / "m.db"
    env = {**os.environ, "TAGPLT_DATABASE_PATH": str(db_path)}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    conn = sqlite3.connect(db_path)
    try:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "prompt_template" in tables
        # 契约钉死 (mode, version) 唯一：建表 DDL 必须含 UNIQUE 约束
        ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='prompt_template'"
        ).fetchone()[0]
        assert "UNIQUE" in ddl
        rows = conn.execute(
            "SELECT mode, version, is_active FROM prompt_template ORDER BY mode"
        ).fetchall()
        assert rows == [("closed", 1, 1), ("open", 1, 1), ("open", 2, 0)]
    finally:
        conn.close()
