"""11 张 v2 表 ORM 模型测试（v2 全平台切换 Task 1）。

覆盖契约：
- 枚举 CHECK 约束（doc §3 内联 CHECK 逐字落地）：各枚举列合法值入库、
  非法值 IntegrityError（非法值刻意混入 v1 已退役字面量，如 origin=catalog、
  review_status=merge——v2 口径下必须被库层拒绝）；
- 唯一约束：ux_tag_define_dir_name（同 dir 同名冲突、跨 dir 允许）、
  ux_tag_catalog_parent_name（同 parent 同名冲突、跨 parent/根目录允许）、
  ux_asset_tag 全键唯一（同 tag_id 不同 tag_name 为泛化值预留，允许）；
- pre_asset_tag 两个部分唯一索引（沿用 v1 的 Index + sqlite_where 写法）：
  目录标签/候选名幂等 + NULL 语义（tag_id NULL 且 candidate_name NULL 的行
  不受任何唯一索引约束）；
- 外键：PreAssetTag → media_asset / tag_define / tag_generation_task /
  tag_catalog（candidate_dir_id）；
- v2 合并语义：content_json/source 落 media_asset 列，media_context 表消失；
- prompt 种子钩子：create_all 后 prompt_template 恰 3 行且 closed v1 active。

注：涉及外键的用例先补齐父行并单独 flush（父行先落库），确保报错确因目标
约束而非外键缺失——create_engine_for 逐连接开启 foreign_keys=ON，缺父行
同样报 IntegrityError，会掩盖断言。
"""

import pytest
from sqlalchemy.exc import IntegrityError

from tagplatform.db.models import (
    AssetTag,
    Base,
    MediaAsset,
    PreAssetTag,
    PromptTemplate,
    TagCatalog,
    TagChangeLog,
    TagDefine,
    TagGenerationTask,
    TagImportTask,
)


# ---------- 最小合法行构造器（字段覆盖即改写对应列） ----------

def make_asset(**kw):
    defaults = dict(asset_id="asset_1", asset_type="movie", title="测试影片")
    defaults.update(kw)
    return MediaAsset(**defaults)


def make_dir(**kw):
    defaults = dict(dir_id="dir_1", dir_name="类型",
                    dir_type="dir_static", description="固定枚举目录")
    defaults.update(kw)
    return TagCatalog(**defaults)


def make_tag(**kw):
    defaults = dict(tag_id="tag_1", tag_name="喜剧", tag_type="val_fixed",
                    description="以笑点为主", dir_id="dir_1")
    defaults.update(kw)
    return TagDefine(**defaults)


def make_gen_task(**kw):
    defaults = dict(task_id="task_1", asset_id="asset_1", mode="closed",
                    status="queued")
    defaults.update(kw)
    return TagGenerationTask(**defaults)


def make_pre(**kw):
    defaults = dict(asset_id="asset_1", target_type="content",
                    target_id="asset_1", origin="closed",
                    review_status="pending")
    defaults.update(kw)
    return PreAssetTag(**defaults)


def make_mount(**kw):
    defaults = dict(asset_id="asset_1", target_type="content",
                    target_id="asset_1", tag_id="tag_1", tag_name="喜剧")
    defaults.update(kw)
    return AssetTag(**defaults)


def make_import_task(**kw):
    defaults = dict(task_id="imp_1", file_name="tags.xlsx",
                    status="processing")
    defaults.update(kw)
    return TagImportTask(**defaults)


def make_log(**kw):
    defaults = dict(tag_id="tag_1", action="create")
    defaults.update(kw)
    return TagChangeLog(**defaults)


# ---------- 枚举 CHECK 约束 ----------

# (构造器, 枚举列, 合法值, 非法值)；非法值混入 v1 退役字面量验证口径切换
CHECK_CASES = [
    (make_asset, "asset_type", ("movie", "series", "scene"), "episode"),
    (make_dir, "dir_type", ("dir_static", "dir_dynamic"), "static"),
    (make_dir, "applicable_scope", ("content", "scene", "character"), "frame"),
    (make_tag, "tag_type", ("val_fixed", "val_generalized"), "genre"),
    (make_tag, "status", ("active", "deprecated"), "deprecate"),
    (make_gen_task, "mode", ("closed", "open"), "hybrid"),
    (make_gen_task, "status",
     ("queued", "running", "succeeded", "failed"), "canceled"),
    (make_pre, "target_type", ("content", "scene", "character"), "shot"),
    (make_pre, "origin", ("closed", "open"), "catalog"),
    (make_pre, "review_status", ("pending", "approved", "rejected"), "merge"),
    (make_mount, "target_type", ("content", "scene", "character"), "shot"),
    (make_import_task, "status",
     ("processing", "completed", "failed", "revoked"), "done"),
    (make_log, "action",
     ("create", "update", "merge", "deprecate", "import"), "delete"),
]

# 合法值批量入库/非法值单行入库时按序号改写主键与唯一键列，避免自撞
UNIQUE_OVERRIDES = {
    make_asset: lambda i: {"asset_id": f"asset_x{i}"},
    make_dir: lambda i: {"dir_id": f"dir_x{i}", "dir_name": f"目录{i}"},
    make_tag: lambda i: {"tag_id": f"tag_x{i}", "tag_name": f"标签{i}"},
    make_gen_task: lambda i: {"task_id": f"task_x{i}"},
    make_pre: lambda i: {},
    make_mount: lambda i: {},
    make_import_task: lambda i: {"task_id": f"imp_x{i}"},
    make_log: lambda i: {},
}


@pytest.fixture
def seeded(session):
    """补齐外键父行：asset_1 / dir_1 / tag_1（tag_1 挂 dir_1），父行先落库。"""
    session.add_all([make_asset(), make_dir(), make_tag()])
    session.flush()
    return session


@pytest.mark.parametrize("builder,field,legal,_illegal", CHECK_CASES)
def test_enum_check_accepts_legal(seeded, builder, field, legal, _illegal):
    for i, value in enumerate(legal):
        kw = {field: value, **UNIQUE_OVERRIDES[builder](i)}
        seeded.add(builder(**kw))
    seeded.commit()  # 全部合法值应能入库


@pytest.mark.parametrize("builder,field,_legal,illegal", CHECK_CASES)
def test_enum_check_rejects_illegal(seeded, builder, field, illegal, _legal):
    kw = {field: illegal, **UNIQUE_OVERRIDES[builder](99)}
    seeded.add(builder(**kw))
    with pytest.raises(IntegrityError):
        seeded.commit()  # 父行齐备、键值全新 → 报错只能来自 CHECK


# ---------- 唯一约束 ----------

def test_ux_tag_define_dir_name(session):
    """同 dir 同名冲突；跨 dir 允许（doc §3.3）。"""
    session.add_all([
        make_dir(dir_id="dir_a", dir_name="风格"),
        make_dir(dir_id="dir_b", dir_name="题材"),
    ])
    session.commit()  # 父目录先持久化：冲突批 rollback 会连同未提交行一并回滚
    session.add_all([
        make_tag(tag_id="tag_1", tag_name="喜剧", dir_id="dir_a"),
        make_tag(tag_id="tag_2", tag_name="喜剧", dir_id="dir_a"),
    ])
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()
    session.add(make_tag(tag_id="tag_3", tag_name="喜剧", dir_id="dir_b"))
    session.commit()
    assert session.get(TagDefine, "tag_3") is not None


def test_ux_tag_catalog_parent_name(session):
    """parent 作用域唯一：同 parent 下同名冲突；跨 parent 与根目录（NULL）
    同名允许——SQL UNIQUE 对 NULL 互不区分，由服务层校验（doc §3.2）。"""
    session.add_all([
        make_dir(dir_id="dir_root", dir_name="一级目录"),
        make_dir(dir_id="dir_c1", dir_name="喜剧", parent_id="dir_root"),
    ])
    session.commit()
    session.add(make_dir(dir_id="dir_c2", dir_name="喜剧", parent_id="dir_root"))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()
    session.add_all([
        make_dir(dir_id="dir_other", dir_name="其他一级"),
        make_dir(dir_id="dir_c3", dir_name="喜剧", parent_id="dir_other"),
        make_dir(dir_id="dir_root2", dir_name="一级目录"),
    ])
    session.commit()


def test_ux_asset_tag_full_key(session):
    """ux_asset_tag 全键唯一：(asset_id, target_type, target_id, tag_id,
    tag_name)。同 tag_id 不同 tag_name 允许（泛化值预留：同一定义可挂多个
    实际值，doc §3.6）。"""
    session.add_all([make_asset(), make_dir(), make_tag()])
    session.commit()  # 父行先持久化（同上）
    session.add_all([make_mount(), make_mount()])
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()
    session.add_all([make_mount(tag_name="张三"), make_mount(tag_name="李四")])
    session.commit()


# ---------- pre_asset_tag 部分唯一索引（幂等 + NULL 语义） ----------

def test_ux_pre_tag_catalog_partial_unique(session):
    """ux_pre_tag_catalog：同一资产同一实体不重复挂同一标签定义；
    不同实体（scene 粒度）允许。"""
    session.add_all([make_asset(), make_dir(), make_tag()])
    session.commit()  # 父行先持久化（同上）
    session.add_all([make_pre(tag_id="tag_1"), make_pre(tag_id="tag_1")])
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()
    session.add(make_pre(tag_id="tag_1", target_type="scene",
                         target_id="scene_001"))
    session.commit()


def test_ux_pre_tag_candidate_partial_unique(session):
    """ux_pre_tag_candidate：同一资产同一实体不重复收录同一候选名
    （open 候选，tag_id 为空）。"""
    session.add(make_asset())
    session.commit()
    session.add_all([
        make_pre(origin="open", candidate_name="新词"),
        make_pre(origin="open", candidate_name="新词"),
    ])
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()
    session.add(make_pre(origin="open", candidate_name="另一词"))
    session.commit()


def test_pre_partial_index_null_semantics(session):
    """NULL 语义：tag_id IS NOT NULL 谓词之外的行不受任何唯一索引约束——
    同资产同实体的多行 tag_id NULL 且 candidate_name NULL 可共存。"""
    session.add(make_asset())
    session.flush()
    session.add_all([make_pre(), make_pre()])
    session.commit()
    assert session.query(PreAssetTag).count() == 2


# ---------- 外键 ----------

@pytest.mark.parametrize("overrides", [
    {"asset_id": "asset_missing"},
    {"tag_id": "tag_missing"},
    {"task_id": "task_missing"},
    {"candidate_dir_id": "dir_missing"},
], ids=["media_asset", "tag_define", "tag_generation_task", "tag_catalog"])
def test_pre_asset_tag_foreign_keys(seeded, overrides):
    seeded.add(make_pre(**overrides))
    with pytest.raises(IntegrityError):
        seeded.commit()  # foreign_keys=ON：缺父行必须被拒


def test_pre_asset_tag_full_fk_chain(seeded):
    """全父链就位时 closed/open 两形态行均可入库（各走一个部分唯一索引）。"""
    seeded.add(make_gen_task(status="succeeded"))
    seeded.flush()
    seeded.add_all([
        make_pre(tag_id="tag_1", task_id="task_1",
                 relevance_score=0.9, confidence=0.8),
        make_pre(origin="open", candidate_name="浪漫甜蜜",
                 candidate_dir_id="dir_1", task_id="task_1",
                 review_status="approved", reviewer_id="user_1",
                 review_comment="合入"),
    ])
    seeded.commit()
    assert seeded.query(PreAssetTag).count() == 2


# ---------- v2 合并语义与默认值 ----------

def test_media_asset_merged_columns(session):
    """v2 合并：content_json/source 落 media_asset 列；media_context 表
    不再存在于 metadata（识别状态 = content_json IS NULL，doc §3.1）。"""
    assert "media_context" not in Base.metadata.tables
    session.add(make_asset(content_json='{"schema_version": "1.0"}',
                           source="upload"))
    session.commit()
    got = session.get(MediaAsset, "asset_1")
    assert got.content_json == '{"schema_version": "1.0"}'
    assert got.source == "upload"


def test_server_default_columns(session):
    """DDL DEFAULT 子句经 server_default 落库：目录适用范围、标签状态、
    粒度与审核状态默认值（doc §3）。"""
    d = make_dir(dir_id="dir_mood", dir_name="整体情绪基调",
                 dir_type="dir_dynamic")
    t = make_tag(tag_id="tag_mood", tag_name="浪漫甜蜜", dir_id="dir_mood")
    session.add_all([make_asset(), d, t])
    session.flush()
    p = make_pre()
    session.add(p)
    session.commit()
    assert session.get(TagCatalog, "dir_mood").applicable_scope == "content"
    assert session.get(TagDefine, "tag_mood").status == "active"
    assert session.get(PreAssetTag, p.id).target_type == "content"
    assert session.get(PreAssetTag, p.id).review_status == "pending"


# ---------- prompt 种子钩子 ----------

def test_prompt_seed_after_create_all(session):
    """create_all 路径的 after_create 钩子：prompt_template 恰 3 行
    （closed v1 active、open v1 active、open v2 过时标记 inactive）。"""
    rows = session.query(PromptTemplate).all()
    assert len(rows) == 3
    closed_v1 = next(r for r in rows
                     if r.mode == "closed" and r.version == 1)
    assert closed_v1.is_active is True
    open_v1 = next(r for r in rows if r.mode == "open" and r.version == 1)
    assert open_v1.is_active is True
    open_v2 = next(r for r in rows if r.mode == "open" and r.version == 2)
    assert open_v2.is_active is False
