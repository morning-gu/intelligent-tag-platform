"""8 张核心表 ORM 模型测试（Task 3）。

覆盖契约：
- TagCatalog 往返读写与 status 默认值 'active'；
- AssetTag 两个部分唯一索引真实触发 IntegrityError（目录标签：同一资产
  不重复挂同一标签；候选标签：同一资产不重复收录同一候选名）；
- alembic 迁移可升级：子进程 `upgrade head` 后 8 张表全部存在。

注：两个 IntegrityError 用例先补齐外键父行（media_asset / tag_catalog），
确保报错确因部分唯一索引而非外键缺失——create_engine_for 逐连接开启
foreign_keys=ON，缺父行同样会报 IntegrityError，会掩盖索引断言。
"""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

from tagplatform.db.models import AssetTag, MediaAsset, TagCatalog

# 迁移测试断言用：8 张核心表名
EXPECTED_TABLES = {
    "media_asset",
    "media_context",
    "tag_catalog",
    "generation_task",
    "asset_tag",
    "review_record",
    "import_batch",
    "tag_change_log",
}


def test_tag_catalog_roundtrip(session):
    t = TagCatalog(tag_id="tag_x", tag_name="悬疑", tag_type="genre",
                   description="以悬念和谜题为核心", aliases='["悬念"]')
    session.add(t)
    session.commit()
    got = session.get(TagCatalog, "tag_x")
    assert got.aliases == '["悬念"]' and got.status == "active"


def test_asset_tag_partial_unique(session):
    # 先补齐外键父行并单独 flush，让 IntegrityError 确由 ux_asset_tag_catalog
    # 触发。必须分两次 flush：ORM 无 relationship() 时按类名字母序决定跨表
    # 插入顺序（AssetTag 排在 MediaAsset/TagCatalog 之前），同批 flush 会
    # 因父行未落库而先报外键错误，掩盖索引断言。
    session.add(MediaAsset(asset_id="a", asset_type="movie", title="测试影片"))
    session.add(TagCatalog(tag_id="t1", tag_name="悬疑", tag_type="genre",
                           description="d"))
    session.flush()
    session.add_all([AssetTag(asset_id="a", tag_id="t1", origin="catalog"),
                     AssetTag(asset_id="a", tag_id="t1", origin="catalog")])
    with pytest.raises(IntegrityError):
        session.commit()


def test_asset_tag_candidate_unique(session):
    # 同上：先单独 flush 父行，确保 IntegrityError 确由 ux_asset_tag_candidate 触发
    session.add(MediaAsset(asset_id="a", asset_type="movie", title="测试影片"))
    session.flush()
    session.add_all([AssetTag(asset_id="a", origin="generated", candidate_name="新词"),
                     AssetTag(asset_id="a", origin="generated", candidate_name="新词")])
    with pytest.raises(IntegrityError):
        session.commit()


def test_alembic_upgrade(tmp_path):
    """子进程执行 alembic upgrade head，升级后 sqlite_master 中 8 张表齐备。"""
    db_path = tmp_path / "m.db"
    env = {**os.environ, "TAGPLT_DATABASE_PATH": str(db_path)}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 0, (
        f"alembic upgrade 失败:\n{result.stdout}\n{result.stderr}"
    )
    conn = sqlite3.connect(str(db_path))
    try:
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        conn.close()
    assert EXPECTED_TABLES <= names, f"缺表: {EXPECTED_TABLES - names}"
