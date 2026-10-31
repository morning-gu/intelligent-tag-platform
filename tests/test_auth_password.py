"""认证域（B1）测试：密码哈希/校验、账号服务、启动引导与迁移。

契约咬合面：
- 存储格式 `pbkdf2$200000$salt_hex$hash_hex`（pbkdf2_hmac sha256、16 字节盐、
  200_000 轮）；verify_password 对坏格式返回 False 不抛（坏输入当错密码）。
- create_user 重名抛 ValueError；bootstrap_admin 仅在 user_account 空表时
  创建首管理员 admin（返回是否创建），lifespan 启动时经
  TAGPLT_ADMIN_PASSWORD（默认 admin123）触发。
- 迁移测试以子进程真跑 alembic：0003 后 user_account 表存在且列与 ORM
  模型逐一对齐（而非只存在于 create_all 元数据）。
"""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from tagplatform.auth import (
    bootstrap_admin,
    create_user,
    get_user_by_username,
    hash_password,
    verify_password,
)

# 仓库根（alembic.ini 所在目录）：子进程迁移测试的工作目录
_REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------- 密码哈希与校验 ----------

def test_hash_password_format():
    """哈希串格式钉死：pbkdf2$200000$<32位盐hex>$<64位sha256 hex>。"""
    stored = hash_password("s3cret-密码")
    parts = stored.split("$")
    assert len(parts) == 4
    scheme, rounds, salt_hex, hash_hex = parts
    assert scheme == "pbkdf2"
    assert rounds == "200000"
    assert len(salt_hex) == 32 and int(salt_hex, 16) >= 0  # 16 字节盐的 hex
    assert len(hash_hex) == 64 and int(hash_hex, 16) >= 0  # sha256 摘要的 hex


def test_hash_verify_roundtrip():
    """正确密码经哈希后必须校验通过。"""
    stored = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", stored) is True


def test_hash_is_salted():
    """同一密码两次哈希结果不同（盐随机），但都能通过校验。"""
    a, b = hash_password("same-pwd"), hash_password("same-pwd")
    assert a != b
    assert verify_password("same-pwd", a) and verify_password("same-pwd", b)


def test_verify_wrong_password_false():
    """错密码返回 False（不抛）。"""
    stored = hash_password("right-password")
    assert verify_password("wrong-password", stored) is False
    assert verify_password("", stored) is False


@pytest.mark.parametrize("stored", [
    "",
    "not-a-hash",
    "md5$200000$abcd",  # 未知 scheme 前缀
    "pbkdf2$200000$aa",  # 段数不足
    "pbkdf2$abc$0011$0011",  # 轮数非数字
    "pbkdf2$200000$zzzz$0011",  # 盐非十六进制
    "pbkdf2$200000$0011$éé",  # hash 段非 ASCII（compare_digest 抛 TypeError）
])
def test_verify_bad_format_false_no_raise(stored):
    """坏格式一律返回 False，绝不抛异常（与错密码同路径）。"""
    assert verify_password("anything", stored) is False


# ---------- 账号服务 ----------

def test_create_user_and_get_by_username(session):
    """建号后按用户名可查回：user_id 前缀 user_、哈希可校验、时间戳显式。"""
    user = create_user(session, "alice", "pw-123", "reviewer")
    assert user.user_id.startswith("user_") and len(user.user_id) > len("user_")
    assert user.role == "reviewer"
    assert user.created_at is not None
    assert verify_password("pw-123", user.password_hash)

    fetched = get_user_by_username(session, "alice")
    assert fetched is not None and fetched.user_id == user.user_id
    assert get_user_by_username(session, "nobody") is None


def test_create_user_duplicate_raises_valueerror(session):
    """重名建号抛 ValueError（服务层校验，表无 CHECK 约束）。"""
    create_user(session, "bob", "pw-1", "admin")
    with pytest.raises(ValueError):
        create_user(session, "bob", "pw-2", "reviewer")


# ---------- 启动引导 ----------

def test_bootstrap_admin_empty_table_creates(session):
    """空表引导：创建 admin 账号并返回 True，密码可校验。"""
    assert bootstrap_admin(session, "boot-pw") is True
    admin = get_user_by_username(session, "admin")
    assert admin is not None
    assert admin.role == "admin"
    assert verify_password("boot-pw", admin.password_hash)


def test_bootstrap_admin_nonempty_table_skips(session):
    """表非空（含仅 reviewer 用户）引导跳过：返回 False 且不覆盖既有账号。"""
    create_user(session, "someone", "pw-x", "reviewer")
    assert bootstrap_admin(session, "boot-pw") is False
    # 既有账号未被触碰，也未新建 admin
    assert get_user_by_username(session, "admin") is None
    assert verify_password("pw-x", get_user_by_username(session, "someone").password_hash)

    # 已有 admin 时再次引导同样跳过（幂等：每次启动都会调用）
    assert bootstrap_admin(session, "boot-pw") is False
    assert verify_password("pw-x", get_user_by_username(session, "someone").password_hash)


def test_lifespan_bootstrap_creates_admin(tmp_path, monkeypatch):
    """app 启动 lifespan 触发引导：临时文件库落 admin，密码取环境变量。

    不用 conftest 的 client fixture（其 lifespan 在 monkeypatch 之前启动
    无法保证 fixture 顺序），本测试自建 app：先设 env 再启动 TestClient，
    顺序完全可控；表结构与配置复制 client fixture 的做法（create_all +
    mock 嵌入器/LLM + 同步 executor），conftest 本身零改动。
    """
    from fastapi.testclient import TestClient

    from tagplatform.api import create_app
    from tagplatform.db import create_engine_for
    from tagplatform.db.models import Base
    from tagplatform.settings import Settings

    monkeypatch.setenv("TAGPLT_ADMIN_PASSWORD", "e2e-admin-pw")

    db_path = tmp_path / "tagplatform.db"
    settings = Settings(database_path=str(db_path), use_mock_embedder=True,
                        use_mock_llm=True, generation_workers=0)
    engine = create_engine_for(str(db_path))
    Base.metadata.create_all(engine)
    engine.dispose()  # 建表引擎即弃，app 内部自建引擎

    app = create_app(settings)
    with TestClient(app):  # 进入上下文即触发 lifespan（含启动引导）
        pass
    app.state.engine.dispose()  # 释放 app 级连接（Windows 文件句柄）

    with app.state.session_factory() as s:
        admin = get_user_by_username(s, "admin")
    assert admin is not None and admin.role == "admin"
    assert verify_password("e2e-admin-pw", admin.password_hash)


# ---------- 迁移（子进程真跑 alembic） ----------

def test_migration_creates_user_account(tmp_path):
    """alembic upgrade head → user_account 建表，列与 ORM 模型逐一对齐。"""
    from tagplatform.db.models import UserAccount

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

    model_cols = {c.name for c in UserAccount.__table__.columns}
    conn = sqlite3.connect(db_path)
    try:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "user_account" in tables
        cols = {row[1] for row in conn.execute("PRAGMA table_info(user_account)")}
        assert cols == model_cols
        # username 唯一约束必须落进 DDL（登录与重名校验的依据）
        ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='user_account'"
        ).fetchone()[0]
        assert "UNIQUE" in ddl
    finally:
        conn.close()
