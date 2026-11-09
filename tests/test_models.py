"""核心表 ORM 模型测试（Task 3）。

覆盖契约：
- 标签库两表：`TagCatalog`(目录) + `TagDefine`(标签) 往返读写与默认值；
- `AssetTag` 的 `ux_asset_tag` 唯一键（asset_id,target_type,target_id,tag_id,
  tag_name）真实触发 IntegrityError；同一标签不同 tag_name（泛化值为它预留）
  允许共存；
- `PreAssetTag` 两个部分唯一索引与 target_type CHECK 真实触发 IntegrityError；
- alembic 迁移可升级：子进程 `upgrade head` 后核心表全部存在。

注：IntegrityError 用例先补齐外键父行（media_asset / tag_catalog / tag_define），
确保报错确因目标约束而非外键缺失——create_engine_for 逐连接开启
foreign_keys=ON，缺父行同样会报 IntegrityError，会掩盖约束断言。
"""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

from tagplatform.db.models import (
    AssetTag,
    MediaAsset,
    PreAssetTag,
    TagCatalog,
    TagDefine,
)

# 迁移测试断言用：核心表名（v2 标签库两表 + 四张本地表 + 资产挂载/日志/账号）
EXPECTED_TABLES = {
    "media_asset",
    "asset_build_task",
    "tag_catalog",
    "tag_define",
    "tag_generation_task",
    "pre_asset_tag",
    "asset_tag",
    "tag_import_task",
    "tag_change_log",
    "prompt_template",
    "user_account",
}


def _add_library(session, dir_id="dir_1", tag_id="t1"):
    """补齐标签库父行：目录 → 标签定义。"""
    session.add(TagCatalog(dir_id=dir_id, dir_name="类型", dir_type="dir_static",
                           description="d", applicable_scope="content"))
    session.add(TagDefine(tag_id=tag_id, tag_name="悬疑", tag_type="val_fixed",
                          description="d", dir_id=dir_id))


def test_tag_catalog_define_roundtrip(session):
    _add_library(session)
    session.commit()
    d = session.get(TagCatalog, "dir_1")
    t = session.get(TagDefine, "t1")
    assert d.dir_type == "dir_static" and d.applicable_scope == "content"
    assert t.dir_id == "dir_1" and t.tag_type == "val_fixed"
    assert t.status == "active"


def test_asset_tag_unique(session):
    """ux_asset_tag：同资产同实体同标签同展示值只能一行。"""
    session.add(MediaAsset(asset_id="a", asset_type="movie", title="测试影片"))
    _add_library(session)
    session.flush()
    session.add_all([
        AssetTag(asset_id="a", target_id="a", tag_id="t1", tag_name="悬疑"),
        AssetTag(asset_id="a", target_id="a", tag_id="t1", tag_name="悬疑"),
    ])
    with pytest.raises(IntegrityError):
        session.commit()


def test_asset_tag_generalized_name_allowed(session):
    """唯一键含 tag_name：同一标签挂不同实际值（泛化值预留）允许共存。"""
    session.add(MediaAsset(asset_id="a", asset_type="movie", title="测试影片"))
    _add_library(session)
    session.flush()
    session.add_all([
        AssetTag(asset_id="a", target_id="a", tag_id="t1", tag_name="张三"),
        AssetTag(asset_id="a", target_id="a", tag_id="t1", tag_name="李四"),
    ])
    session.commit()
    assert session.query(AssetTag).count() == 2


def test_pre_asset_tag_partial_unique(session):
    # 同资产同实体不重复挂同一库内标签（ux_pre_tag_catalog）
    session.add(MediaAsset(asset_id="a", asset_type="movie", title="测试影片"))
    _add_library(session)
    session.flush()
    session.add_all([
        PreAssetTag(asset_id="a", target_id="a", tag_id="t1", origin="closed"),
        PreAssetTag(asset_id="a", target_id="a", tag_id="t1", origin="closed"),
    ])
    with pytest.raises(IntegrityError):
        session.commit()


def test_pre_asset_tag_candidate_unique(session):
    # 同资产同实体不重复收录同一候选名（ux_pre_tag_candidate）
    session.add(MediaAsset(asset_id="a", asset_type="movie", title="测试影片"))
    session.flush()
    session.add_all([
        PreAssetTag(asset_id="a", target_id="a", candidate_name="新词",
                    origin="open"),
        PreAssetTag(asset_id="a", target_id="a", candidate_name="新词",
                    origin="open"),
    ])
    with pytest.raises(IntegrityError):
        session.commit()


def test_pre_asset_tag_foreign_keys(session):
    """tag_id 指 tag_define、candidate_dir_id 指 tag_catalog(dir_id)。"""
    session.add(MediaAsset(asset_id="a", asset_type="movie", title="测试影片"))
    _add_library(session)
    session.flush()
    session.add(PreAssetTag(asset_id="a", target_id="a", tag_id="nope",
                            origin="closed"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_pre_asset_tag_target_type_check(session):
    session.add(MediaAsset(asset_id="a", asset_type="movie", title="测试影片"))
    session.flush()
    session.add(PreAssetTag(asset_id="a", target_id="a", target_type="episode",
                            origin="closed"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_alembic_upgrade(tmp_path):
    """子进程执行 alembic upgrade head，升级后 sqlite_master 中核心表齐备。"""
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
