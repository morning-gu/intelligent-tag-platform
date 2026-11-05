"""0007 v2 表结构重建迁移的带数据彩排测试（doc §8 带数据彩排法）。

三条主线：
1. 彩排升级：临时文件库程序化升到 0006 → 手工插外键链完整的 v1 数据
   （账号、目录父/子、资产+上下文+生成任务、挂载两行、change_log）→
   upgrade head → 断言旧业务表消失、user_account / prompt 3 行种子 /
   media_asset 行原样保留、v2 新表结构与 db.models 逐字对拍（列/NOT NULL/
   默认值/CHECK/索引名）、合法行可插且非法枚举 / FK 违例 / 幂等唯一索引
   拒绝、PRAGMA foreign_key_check 为空。
2. 全新空库 0001→head：种子迁移链（0002/0005 import 种子常量）不断，
   prompt_template 3 行种子齐备——恢复 v1 test_models.py 退役的迁移链
   种子覆盖（deferred minor 闭环点）。
3. downgrade 0006：v2 新表移除、v1 空表重建（数据不回迁）、media_asset
   摘除合并列，再 round-trip 升回 0007 验证迁移可重入。

测试自建临时 sqlite 文件库（tmp_path），经 TAGPLT_DATABASE_PATH 驱动
alembic env.py（get_settings 有 lru_cache，前后各清一次缓存），不碰任何
现有库。
"""

import os
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError
from alembic import command
from alembic.config import Config

from tagplatform.db import create_engine_for, make_session_factory
from tagplatform.db.models import (
    AssetTag,
    MediaAsset,
    PreAssetTag,
    TagCatalog,
    TagChangeLog,
    TagDefine,
    TagGenerationTask,
    TagImportTask,
)
from tagplatform.settings import get_settings

REPO_ROOT = Path(__file__).resolve().parents[1]

# 0007 直接 drop 且不再重建的 v1 业务表（tag_catalog/asset_tag/tag_change_log
# 是 drop 后按 v2 结构重建，名字仍在，不在此列）
GONE_V1_TABLES = {"review_record", "media_context", "import_batch", "generation_task"}
# 0007 新建/重建的 v2 业务表
V2_NEW_TABLES = {
    "tag_catalog",
    "tag_define",
    "pre_asset_tag",
    "asset_tag",
    "tag_generation_task",
    "tag_import_task",
    "tag_change_log",
}
# 其中全新建（v1 无同名表）的 4 张——downgrade 后必须彻底消失
V2_NEW_ONLY = V2_NEW_TABLES - {"tag_catalog", "asset_tag", "tag_change_log"}
# 本期物理不动的表 + 只补两列的 media_asset
UNTOUCHED_TABLES = {"user_account", "prompt_template", "asset_build_task", "media_asset"}

# prompt_template 3 行种子（closed v1 激活 / open v1 激活 / open v2 过时标注）
PROMPT_SEEDS = [("closed", 1, 1), ("open", 1, 1), ("open", 2, 0)]

# 列规格对拍基准（PRAGMA table_info：name, type, notnull, dflt_value），
# 与 db/models.py v2 模型逐字对应；media_asset 为 v1 七列 + 追加的两列
# （迁移按计划只 add_column，不重建——v1 形状的 CHECK/索引留待 PG 迁移对齐）
EXPECTED_COLUMNS = {
    "media_asset": [
        ("asset_id", "TEXT", 1, None),
        ("asset_type", "TEXT", 1, None),
        ("parent_id", "TEXT", 0, None),
        ("title", "TEXT", 1, None),
        ("duration_ms", "INTEGER", 0, None),
        ("video_uri", "TEXT", 0, None),
        ("created_at", "DATETIME", 0, None),
        ("content_json", "TEXT", 0, None),
        ("source", "TEXT", 0, None),
    ],
    "tag_catalog": [
        ("dir_id", "TEXT", 1, None),
        ("dir_name", "TEXT", 1, None),
        ("dir_type", "TEXT", 1, None),
        ("description", "TEXT", 1, None),
        ("parent_id", "TEXT", 0, None),
        ("aliases", "TEXT", 0, None),
        ("applicable_scope", "TEXT", 0, "'content'"),
        ("embedding", "BLOB", 0, None),
        ("created_at", "DATETIME", 0, None),
        ("updated_at", "DATETIME", 0, None),
    ],
    "tag_define": [
        ("tag_id", "TEXT", 1, None),
        ("tag_name", "TEXT", 1, None),
        ("tag_type", "TEXT", 1, None),
        ("description", "TEXT", 1, None),
        ("dir_id", "TEXT", 1, None),
        ("aliases", "TEXT", 0, None),
        ("status", "TEXT", 0, "'active'"),
        ("embedding", "BLOB", 0, None),
        ("created_at", "DATETIME", 0, None),
        ("updated_at", "DATETIME", 0, None),
    ],
    "tag_generation_task": [
        ("task_id", "TEXT", 1, None),
        ("asset_id", "TEXT", 1, None),
        ("mode", "TEXT", 1, None),
        ("model_version", "TEXT", 0, None),
        ("prompt_version", "TEXT", 0, None),
        ("policy_json", "TEXT", 0, None),
        ("status", "TEXT", 1, None),
        ("error", "TEXT", 0, None),
        ("created_at", "DATETIME", 0, None),
        ("finished_at", "DATETIME", 0, None),
    ],
    "pre_asset_tag": [
        ("id", "INTEGER", 1, None),
        ("asset_id", "TEXT", 1, None),
        ("target_type", "TEXT", 1, "'content'"),
        ("target_id", "TEXT", 1, None),
        ("tag_id", "TEXT", 0, None),
        ("candidate_name", "TEXT", 0, None),
        ("candidate_dir_id", "TEXT", 0, None),
        ("candidate_description", "TEXT", 0, None),
        ("origin", "TEXT", 1, None),
        ("relevance_score", "REAL", 0, None),
        ("confidence", "REAL", 0, None),
        ("evidence_json", "TEXT", 0, None),
        ("task_id", "TEXT", 0, None),
        ("review_status", "TEXT", 1, "'pending'"),
        ("reviewer_id", "TEXT", 0, None),
        ("review_comment", "TEXT", 0, None),
        ("created_at", "DATETIME", 0, None),
        ("updated_at", "DATETIME", 0, None),
    ],
    "asset_tag": [
        ("id", "INTEGER", 1, None),
        ("asset_id", "TEXT", 1, None),
        ("target_type", "TEXT", 1, "'content'"),
        ("target_id", "TEXT", 1, None),
        ("tag_id", "TEXT", 1, None),
        ("tag_name", "TEXT", 1, None),
        ("created_at", "DATETIME", 0, None),
        ("updated_at", "DATETIME", 0, None),
    ],
    "tag_import_task": [
        ("task_id", "TEXT", 1, None),
        ("file_name", "TEXT", 1, None),
        ("status", "TEXT", 1, None),
        ("result_summary", "TEXT", 0, None),
        ("created_at", "DATETIME", 0, None),
    ],
    "tag_change_log": [
        ("log_id", "INTEGER", 1, None),
        ("tag_id", "TEXT", 1, None),
        ("action", "TEXT", 1, None),
        ("before_json", "TEXT", 0, None),
        ("after_json", "TEXT", 0, None),
        ("operator", "TEXT", 0, None),
        ("created_at", "DATETIME", 0, None),
    ],
}

# CHECK / UNIQUE 约束片段（doc §3 内联 DDL 逐字；出现在 CREATE TABLE 正文中）
EXPECTED_CHECK_FRAGMENTS = {
    "tag_catalog": [
        "CHECK (dir_type IN ('dir_static', 'dir_dynamic'))",
        "CHECK (applicable_scope IN ('content', 'scene', 'character'))",
        "CONSTRAINT ux_tag_catalog_parent_name UNIQUE (parent_id, dir_name)",
    ],
    "tag_define": [
        "CHECK (tag_type IN ('val_fixed', 'val_generalized'))",
        "CHECK (status IN ('active', 'deprecated'))",
        "CONSTRAINT ux_tag_define_dir_name UNIQUE (dir_id, tag_name)",
    ],
    "tag_generation_task": [
        "CHECK (mode IN ('closed', 'open'))",
        "CHECK (status IN ('queued', 'running', 'succeeded', 'failed'))",
    ],
    "pre_asset_tag": [
        "CHECK (target_type IN ('content', 'scene', 'character'))",
        "CHECK (origin IN ('closed', 'open'))",
        "CHECK (review_status IN ('pending', 'approved', 'rejected'))",
    ],
    "asset_tag": [
        "CHECK (target_type IN ('content', 'scene', 'character'))",
        "CONSTRAINT ux_asset_tag UNIQUE (asset_id, target_type, target_id, tag_id, tag_name)",
    ],
    "tag_import_task": [
        "CHECK (status IN ('processing', 'completed', 'failed', 'revoked'))",
    ],
    "tag_change_log": [
        "CHECK (action IN ('create', 'update', 'merge', 'deprecate', 'import'))",
    ],
}

# CREATE INDEX 级索引名（表内 UNIQUE 约束在 SQLite 中是 sqlite_autoindex_*，
# 不经 CREATE INDEX 建出，故不在本集合）
EXPECTED_INDEXES = {
    "idx_tag_catalog_type",
    "idx_tag_catalog_scope",
    "idx_tag_define_status",
    "idx_tag_define_dir",
    "idx_gen_task_asset",
    "idx_gen_task_status",
    "ux_pre_tag_catalog",
    "ux_pre_tag_candidate",
    "idx_pre_tag_asset",
    "idx_pre_tag_task",
    "idx_pre_tag_review",
    "idx_asset_tag_asset",
    "idx_asset_tag_tag",
    "idx_import_task_status",
    "idx_change_log_tag",
}


@pytest.fixture
def rehearsal(tmp_path):
    """带数据彩排环境：临时文件库 + alembic Config。

    env.py 运行时经 get_settings() 读库路径（TAGPLT_DATABASE_PATH 覆盖），
    该函数 lru_cache 缓存——前置与收尾各清一次，收尾在环境变量还原后执行，
    不向同进程其他测试泄漏缓存。
    """
    db_path = tmp_path / "rehearsal.db"
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


def _table_names(db_path: Path) -> set[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    finally:
        conn.close()
    return {r[0] for r in rows}


def _create_table_sql(db_path: Path, table: str) -> str | None:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def _index_sql(db_path: Path, name: str) -> str | None:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
            (name,),
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def _index_names(db_path: Path) -> set[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    finally:
        conn.close()
    return {r[0] for r in rows}


def _table_columns(db_path: Path, table: str) -> list[tuple]:
    """PRAGMA table_info → [(name, type, notnull, dflt_value), ...]（列序）。"""
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    finally:
        conn.close()
    return [(r[1], r[2], r[3], r[4]) for r in rows]


def _fk_check(db_path: Path) -> list[tuple]:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        conn.close()
    return rows


def _seed_v1_data(db_path: Path) -> None:
    """彩排数据：0006 时刻表形状下、外键父/子链完整的 v1 业务行。

    目录父/子（v1 目录/标签混表）、资产 + 上下文 + 生成任务 + 挂载两行
    （closed 带 tag_id / open 带候选名）、账号一行、change_log 一行。
    种子连接自身开 PRAGMA foreign_keys=ON，外键链断了当场报错。
    """
    ts = "2026-11-01 08:00:00"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        "INSERT INTO user_account (user_id, username, password_hash, role, created_at)"
        " VALUES ('user_admin01', 'admin', 'pbkdf2$200000$salt$hash', 'admin', ?)",
        (ts,),
    )
    # v1 tag_catalog：目录节点与叶子标签混一张表（父/子两行）
    conn.execute(
        "INSERT INTO tag_catalog (tag_id, tag_name, tag_type, description, parent_id,"
        " applicable_scope, status, created_at, updated_at)"
        " VALUES ('dir_genre', '类型', 'dir', '类型目录', NULL, 'content', 'active', ?, ?)",
        (ts, ts),
    )
    conn.execute(
        "INSERT INTO tag_catalog (tag_id, tag_name, tag_type, description, parent_id,"
        " applicable_scope, status, created_at, updated_at)"
        " VALUES ('tag_comedy_v1', '喜剧', 'genre', '喜剧标签', 'dir_genre',"
        " 'content', 'active', ?, ?)",
        (ts, ts),
    )
    conn.execute(
        "INSERT INTO media_asset (asset_id, asset_type, parent_id, title, duration_ms,"
        " video_uri, created_at)"
        " VALUES ('asset_movie_001', 'movie', NULL, '彩排影片', 120000, '/media/demo.mp4', ?)",
        (ts,),
    )
    conn.execute(
        "INSERT INTO media_context (context_id, asset_id, content_json, source,"
        " model_version, recognition_status, created_at)"
        " VALUES ('ctx_001', 'asset_movie_001', '{\"content\": {\"summary\": \"彩排\"}}',"
        " '外部项目', 'test-model-v1', 'completed', ?)",
        (ts,),
    )
    conn.execute(
        "INSERT INTO generation_task (task_id, context_id, mode, model_version,"
        " prompt_version, status, created_at, finished_at)"
        " VALUES ('task_001', 'ctx_001', 'closed', 'test-model-v1', 'closed-v1',"
        " 'succeeded', ?, ?)",
        (ts, ts),
    )
    # 挂载两行：closed 行带 tag_id（approved 终态）、open 行带候选名（pending）
    conn.execute(
        "INSERT INTO asset_tag (id, asset_id, tag_id, origin, relevance_score,"
        " confidence, evidence_json, review_status, task_id, created_at, updated_at)"
        " VALUES (1, 'asset_movie_001', 'tag_comedy_v1', 'closed', 0.9, 0.8, '[]',"
        " 'approved', 'task_001', ?, ?)",
        (ts, ts),
    )
    conn.execute(
        "INSERT INTO asset_tag (id, asset_id, candidate_name, candidate_type,"
        " candidate_description, origin, relevance_score, confidence, evidence_json,"
        " review_status, task_id, created_at, updated_at)"
        " VALUES (2, 'asset_movie_001', '新词', 'mood', '开放生成的情绪候选', 'open',"
        " 0.7, 0.6, '[]', 'pending', 'task_001', ?, ?)",
        (ts, ts),
    )
    conn.execute(
        "INSERT INTO tag_change_log (tag_id, action, before_json, after_json,"
        " operator, created_at)"
        " VALUES ('tag_comedy_v1', 'create', NULL, '{\"tag_name\": \"喜剧\"}', 'admin', ?)",
        (ts,),
    )
    conn.commit()
    conn.close()


def _invalid_enum_rows() -> list:
    """每张 v2 新表一条枚举列非法值行（验证 CHECK 库层兜底）。

    各行取互不冲突的主键 / (asset_id, target_id, tag_id, candidate_name)
    组合，保证失败只可能由 CHECK 触发，不与唯一索引/FK 混淆。
    """
    return [
        TagCatalog(dir_id="probe_dir", dir_name="坏目录", dir_type="bogus", description="x"),
        TagDefine(tag_id="probe_tag1", tag_name="坏标签1", tag_type="bogus",
                  description="x", dir_id="dir_comedy"),
        TagDefine(tag_id="probe_tag2", tag_name="坏标签2", tag_type="val_fixed",
                  description="x", dir_id="dir_comedy", status="bogus"),
        TagGenerationTask(task_id="probe_task1", asset_id="asset_001", mode="bogus",
                          status="queued"),
        TagGenerationTask(task_id="probe_task2", asset_id="asset_001", mode="closed",
                          status="bogus"),
        PreAssetTag(asset_id="asset_001", target_id="probe_tt", origin="closed",
                    target_type="bogus"),
        PreAssetTag(asset_id="asset_001", target_id="probe_origin", tag_id="tag_comedy",
                    origin="bogus"),
        PreAssetTag(asset_id="asset_001", target_id="probe_review", tag_id="tag_comedy",
                    origin="closed", review_status="bogus"),
        AssetTag(asset_id="asset_001", target_id="probe_at", tag_id="tag_comedy",
                 tag_name="喜剧", target_type="bogus"),
        TagImportTask(task_id="probe_imp", file_name="x.xlsx", status="bogus"),
        TagChangeLog(tag_id="tag_comedy", action="bogus"),
    ]


def test_0007_upgrade_rehearsal(rehearsal):
    """带数据彩排：0006 带数据升 0007——旧表消失 / 种子保留 / 结构对拍 / 约束兜底。"""
    cfg, db_path = rehearsal
    command.upgrade(cfg, "0006")
    _seed_v1_data(db_path)

    command.upgrade(cfg, "head")

    # ---- 表集合：旧业务表消失、v2 新表齐备、保留表不动 ----
    names = _table_names(db_path)
    assert not (GONE_V1_TABLES & names), f"旧业务表未清除: {GONE_V1_TABLES & names}"
    assert V2_NEW_TABLES <= names, f"v2 新表缺失: {V2_NEW_TABLES - names}"
    assert UNTOUCHED_TABLES <= names, f"保留表缺失: {UNTOUCHED_TABLES - names}"

    # ---- 数据：账号 / prompt 种子 / media_asset 行原样保留（新列落 NULL）----
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT user_id, username, role FROM user_account"
        ).fetchall()
        assert rows == [("user_admin01", "admin", "admin")]

        rows = conn.execute(
            "SELECT mode, version, is_active FROM prompt_template ORDER BY id"
        ).fetchall()
        assert rows == PROMPT_SEEDS

        rows = conn.execute(
            "SELECT asset_id, title, content_json, source FROM media_asset"
        ).fetchall()
        assert rows == [("asset_movie_001", "彩排影片", None, None)]
    finally:
        conn.close()

    # ---- 结构对拍：列规格（含顺序）/ CHECK 文本 / 索引名逐字对应 models.py ----
    for table, expected in EXPECTED_COLUMNS.items():
        assert _table_columns(db_path, table) == expected, f"{table} 列规格不符"
    for table, fragments in EXPECTED_CHECK_FRAGMENTS.items():
        sql = _create_table_sql(db_path, table)
        assert sql is not None, f"{table} 不存在"
        for fragment in fragments:
            assert fragment in sql, f"{table} 缺约束片段: {fragment}"
    assert EXPECTED_INDEXES <= _index_names(db_path), (
        f"缺索引: {EXPECTED_INDEXES - _index_names(db_path)}"
    )
    # pre 表两个部分唯一索引的 WHERE 谓词（幂等边界，doc §6.1）
    assert "WHERE tag_id IS NOT NULL" in _index_sql(db_path, "ux_pre_tag_catalog")
    assert ("WHERE tag_id IS NULL AND candidate_name IS NOT NULL"
            in _index_sql(db_path, "ux_pre_tag_candidate"))

    # ---- 行为：Task 1 模型插合法行成功，非法枚举 / FK 违例 / 幂等唯一被拒 ----
    engine = create_engine_for(str(db_path))
    try:
        with make_session_factory(engine)() as s:
            # v2 全链路最小图：目录树 → 标签 → 资产 → 生成任务 → 待审行 → 正式
            # 挂载。分步 flush 钉住父表先行——模型层无 relationship() 时 UOW
            # 不保证跨表插入顺序（v1 test_models "先 flush 父行"同款约定）。
            s.add(MediaAsset(asset_id="asset_001", asset_type="movie", title="测试影片"))
            s.add_all([
                TagCatalog(dir_id="dir_root", dir_name="类型", dir_type="dir_static",
                           description="固定类型目录"),
                TagCatalog(dir_id="dir_comedy", dir_name="喜剧", dir_type="dir_static",
                           description="喜剧行", parent_id="dir_root"),
            ])
            s.flush()
            s.add(TagDefine(tag_id="tag_comedy", tag_name="喜剧", tag_type="val_fixed",
                            description="搞笑", dir_id="dir_comedy"))
            s.add(TagGenerationTask(task_id="task_001", asset_id="asset_001",
                                    mode="closed", status="succeeded"))
            s.flush()
            s.add_all([
                PreAssetTag(asset_id="asset_001", target_id="asset_001",
                            tag_id="tag_comedy", origin="closed",
                            relevance_score=0.9, confidence=0.8, task_id="task_001"),
                AssetTag(asset_id="asset_001", target_id="asset_001",
                         tag_id="tag_comedy", tag_name="喜剧"),
                TagImportTask(task_id="task_imp_001", file_name="tags.xlsx",
                              status="completed"),
                TagChangeLog(tag_id="tag_comedy", action="create", after_json="{}",
                             operator="admin"),
            ])
            s.commit()

            # 非法枚举：CHECK 约束兜底（doc §3 内联 CHECK 逐字落地）
            for row in _invalid_enum_rows():
                s.add(row)
                with pytest.raises(IntegrityError):
                    s.commit()
                s.rollback()

            # 外键违例：pre 行指向不存在的资产
            s.add(PreAssetTag(asset_id="asset_missing", target_id="probe_fk",
                              tag_id="tag_comedy", origin="closed"))
            with pytest.raises(IntegrityError):
                s.commit()
            s.rollback()

            # 幂等唯一索引 ux_pre_tag_catalog：同资产同实体重复挂同一标签定义
            s.add(PreAssetTag(asset_id="asset_001", target_id="asset_001",
                              tag_id="tag_comedy", origin="closed"))
            with pytest.raises(IntegrityError):
                s.commit()
            s.rollback()

            # 幂等唯一索引 ux_pre_tag_candidate：同实体重复收录同一候选名
            s.add(PreAssetTag(asset_id="asset_001", target_id="probe_cand",
                              candidate_name="新词", origin="open"))
            s.commit()
            s.add(PreAssetTag(asset_id="asset_001", target_id="probe_cand",
                              candidate_name="新词", origin="open"))
            with pytest.raises(IntegrityError):
                s.commit()
            s.rollback()
    finally:
        engine.dispose()

    assert _fk_check(db_path) == []


def test_fresh_db_full_chain(rehearsal):
    """全新空库 0001→head：种子链不断，prompt_template 3 行种子齐备。"""
    cfg, db_path = rehearsal
    command.upgrade(cfg, "head")

    names = _table_names(db_path)
    assert not (GONE_V1_TABLES & names)
    assert V2_NEW_TABLES <= names
    assert UNTOUCHED_TABLES <= names

    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT mode, version, is_active FROM prompt_template ORDER BY id"
        ).fetchall()
        assert rows == PROMPT_SEEDS
    finally:
        conn.close()

    assert _fk_check(db_path) == []


def test_downgrade_to_0006(rehearsal):
    """downgrade 0006：v1 空表重建（数据不回迁）、media_asset 摘列、可重入。"""
    cfg, db_path = rehearsal
    command.upgrade(cfg, "0006")
    _seed_v1_data(db_path)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0006")

    names = _table_names(db_path)
    assert GONE_V1_TABLES <= names, f"v1 表未重建: {GONE_V1_TABLES - names}"
    # 仅 4 张全新表必须消失；tag_catalog/asset_tag/tag_change_log 回到 v1 形状
    assert not (V2_NEW_ONLY & names), f"v2 全新表未移除: {V2_NEW_ONLY & names}"

    # 重建的是 v1 形状（对拍鉴别列）：tag_catalog 回 tag_id 混表主键、
    # asset_tag 回 candidate_type（v2 的 target_type 消失）
    catalog_cols = [c[0] for c in _table_columns(db_path, "tag_catalog")]
    assert "tag_id" in catalog_cols and "dir_id" not in catalog_cols
    asset_tag_cols = [c[0] for c in _table_columns(db_path, "asset_tag")]
    assert "candidate_type" in asset_tag_cols and "target_type" not in asset_tag_cols

    # media_asset 摘除合并列，既有行保留
    cols = [c[0] for c in _table_columns(db_path, "media_asset")]
    assert "content_json" not in cols and "source" not in cols

    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("SELECT asset_id, title FROM media_asset").fetchall()
        assert rows == [("asset_movie_001", "彩排影片")]
        # v1 空表：0007 期间被 drop 的业务数据不回迁
        for table in GONE_V1_TABLES:
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            assert count == 0, table
        # 账号与模板种子不受降级影响
        assert conn.execute("SELECT COUNT(*) FROM user_account").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM prompt_template").fetchone()[0] == 3
    finally:
        conn.close()

    # round-trip：0006 → 0007 可重入（batch 摘列后的 media_asset 仍可再补列）
    command.upgrade(cfg, "head")
    assert V2_NEW_TABLES <= _table_names(db_path)
    assert _fk_check(db_path) == []
