"""审核操作服务测试：pre_asset_tag 的 approve / reject / modify / merge。

覆盖契约（文档 §6.2）：
- approve/reject：状态直达终态，审核人/备注落在行内（reviewer_id /
  review_comment），不再写审核流水表；
- modify：payload 字段写回行内（名称/描述/目录），**改完直接 approved**
  （v2 语义：修改即“改后通过”，不重排）；
- merge：并入既有标签——tag_id 赋目标、候选字段清空、终态 approved；
- 前置：行不存在 ValueError("标签行不存在")；非 pending → ConflictError；
- 载荷：非法 action / modify 无可改字段 / merge 缺或坏目标 → ValueError；
  空串/纯空白 → ValueError("修改字段不能为空")；超 200 字符 → ValueError；
  strip 后落库；
- 落库撞部分唯一索引（merge 到同资产已挂标签 / modify 改名撞已有候选）
  → ConflictError("该资产已挂此标签")，回滚后行不变；
- 服务内 commit：审核后 rollback，行变更仍在。
"""

import pytest

from tagplatform.db.models import MediaAsset, PreAssetTag
from tagplatform.review import ConflictError, apply_review


@pytest.fixture(autouse=True)
def _seed_parents(session):
    """库内标签来自 tests/conftest 的 JSON 标签库（tag_action / tag_drama…）。"""
    session.add(MediaAsset(asset_id="a1", asset_type="movie", title="测试影片"))
    session.commit()


def _seed_row(session, origin="open", status="pending",
              tag_id=None, candidate_name="多线叙事") -> PreAssetTag:
    """播种一行 pre_asset_tag（tag_id 为空即库外新词）。"""
    row = PreAssetTag(
        asset_id="a1",
        target_type="content",
        target_id="a1",
        tag_id=tag_id,
        candidate_name=candidate_name if tag_id is None else None,
        candidate_description="候选描述" if tag_id is None else None,
        candidate_dir_id="dir_theme" if tag_id is None else None,
        origin=origin,
        relevance_score=None if tag_id is None else 0.9,
        confidence=0.8,
        evidence_json="[]",
        review_status=status,
    )
    session.add(row)
    session.commit()
    return row


# ---------- approve / reject ----------


def test_approve_sets_status_and_reviewer(session):
    row = _seed_row(session)
    returned = apply_review(session, row.id, "approve", "alice",
                            {"comment": "证据充分"})
    assert returned.review_status == "approved"
    assert returned.reviewer_id == "alice"
    assert returned.review_comment == "证据充分"
    assert returned.updated_at is not None


def test_reject_sets_status_and_comment(session):
    row = _seed_row(session)
    returned = apply_review(session, row.id, "reject", "bob",
                            {"comment": "证据不足"})
    assert returned.review_status == "rejected"
    assert returned.reviewer_id == "bob"
    assert returned.review_comment == "证据不足"


def test_review_without_comment_keeps_column_empty(session):
    row = _seed_row(session)
    returned = apply_review(session, row.id, "approve", "alice", None)
    assert returned.review_comment is None


# ---------- modify ----------


def test_modify_fields_and_approves(session):
    """v2：修改即通过——字段写回、状态 approved、备注留痕。"""
    row = _seed_row(session)
    returned = apply_review(session, row.id, "modify", "alice", {
        "candidate_name": "多线叙事改", "candidate_description": "新描述",
        "comment": "措辞修正"})
    assert returned.candidate_name == "多线叙事改"
    assert returned.candidate_description == "新描述"
    assert returned.review_status == "approved"
    assert returned.review_comment == "措辞修正"


def test_modify_catalog_row_maps_display_keys(session):
    """库内行按展示别名改行内字段，不触碰共享 tag_catalog 字典。"""
    row = _seed_row(session, origin="closed", tag_id="tag_action")
    returned = apply_review(session, row.id, "modify", "alice", {
        "tag_name": "动作改", "description": "新定义", "dir_id": "dir_theme"})
    assert returned.candidate_name == "动作改"
    assert returned.candidate_description == "新定义"
    assert returned.candidate_dir_id == "dir_theme"
    assert returned.tag_id == "tag_action"  # 挂载关系不动
    assert returned.review_status == "approved"


def test_modify_without_mappable_field_raises(session):
    row = _seed_row(session)
    for payload in (None, {}, {"comment": "仅备注"}, {"irrelevant": "x"}):
        with pytest.raises(ValueError):
            apply_review(session, row.id, "modify", "alice", payload)
    assert session.get(PreAssetTag, row.id).review_status == "pending"


def test_modify_blank_string_field_raises(session):
    row = _seed_row(session)
    for payload in ({"candidate_name": ""},
                    {"tag_name": "   "},
                    {"description": ""}):
        with pytest.raises(ValueError, match="修改字段不能为空"):
            apply_review(session, row.id, "modify", "alice", payload)
    assert session.get(PreAssetTag, row.id).candidate_name == "多线叙事"


def test_modify_overlong_string_field_raises(session):
    row = _seed_row(session)
    with pytest.raises(ValueError, match="candidate_name"):
        apply_review(session, row.id, "modify", "alice",
                     {"candidate_name": "长" * 201})
    returned = apply_review(session, row.id, "modify", "alice",
                            {"candidate_name": "名" * 200})
    assert returned.candidate_name == "名" * 200


def test_modify_strips_string_values(session):
    row = _seed_row(session)
    returned = apply_review(session, row.id, "modify", "alice",
                            {"tag_name": "  紧张感  "})
    assert returned.candidate_name == "紧张感"


# ---------- merge ----------


def test_merge_into_existing_tag(session):
    row = _seed_row(session)
    returned = apply_review(session, row.id, "merge", "alice",
                            {"merge_to_tag_id": "tag_drama", "comment": "并入目录"})
    assert returned.tag_id == "tag_drama"
    assert returned.candidate_name is None
    assert returned.candidate_description is None
    assert returned.review_status == "approved"
    assert returned.review_comment == "并入目录"


def test_merge_without_target_raises(session):
    row = _seed_row(session)
    for payload in (None, {"comment": "缺目标"}):
        with pytest.raises(ValueError):
            apply_review(session, row.id, "merge", "alice", payload)
    assert session.get(PreAssetTag, row.id).review_status == "pending"


def test_merge_unknown_target_raises(session):
    row = _seed_row(session)
    with pytest.raises(ValueError):
        apply_review(session, row.id, "merge", "alice",
                     {"merge_to_tag_id": "tag_none"})


def test_merge_onto_mounted_tag_conflict(session):
    """同资产已挂该标签 → 撞 ux_pre_tag_catalog → ConflictError（API 409）。"""
    _seed_row(session, origin="closed", tag_id="tag_action")  # a1 已挂 tag_action
    row = _seed_row(session)  # 库外新词
    with pytest.raises(ConflictError, match="该资产已挂此标签"):
        apply_review(session, row.id, "merge", "alice",
                     {"merge_to_tag_id": "tag_action"})
    fresh = session.get(PreAssetTag, row.id)
    assert fresh.review_status == "pending" and fresh.tag_id is None
    assert fresh.candidate_name == "多线叙事"


def test_modify_onto_existing_candidate_name_conflict(session):
    """改名撞同资产已有候选 → 撞 ux_pre_tag_candidate → ConflictError。"""
    _seed_row(session, candidate_name="已有候选")
    row = _seed_row(session, candidate_name="待改行")
    with pytest.raises(ConflictError, match="该资产已挂此标签"):
        apply_review(session, row.id, "modify", "alice",
                     {"candidate_name": "已有候选"})
    fresh = session.get(PreAssetTag, row.id)
    assert fresh.candidate_name == "待改行" and fresh.review_status == "pending"


# ---------- 前置与冲突 ----------


def test_missing_row_raises_value_error(session):
    with pytest.raises(ValueError, match="标签行不存在"):
        apply_review(session, 999, "approve", "alice", None)


def test_conflict_when_not_pending(session):
    row = _seed_row(session)
    row.review_status = "approved"
    session.commit()
    with pytest.raises(ConflictError, match="该标签已被处理"):
        apply_review(session, row.id, "approve", "alice", None)


def test_unknown_action_raises(session):
    row = _seed_row(session)
    with pytest.raises(ValueError):
        apply_review(session, row.id, "alien", "alice", None)


def test_service_commits_immediately(session):
    row = _seed_row(session)
    apply_review(session, row.id, "approve", "alice", {"comment": "ok"})
    session.rollback()  # 撤销一切未提交变更
    fresh = session.get(PreAssetTag, row.id)
    assert fresh.review_status == "approved" and fresh.review_comment == "ok"


# ---------- 入库（写 tag_define） ----------


def test_approve_new_word_writes_tag_define(session):
    """通过一条新词 → 真的插进 tag_define（含 create 日志与来源追溯）。"""
    from tagplatform.db.models import TagChangeLog, TagDefine

    row = _seed_row(session, candidate_name="时间循环")
    apply_review(session, row.id, "approve", "alice", {"comment": "新增"})
    created = (session.query(TagDefine)
               .filter_by(dir_id="dir_theme", tag_name="时间循环").one())
    assert created.status == "active"
    log = session.query(TagChangeLog).filter_by(
        tag_id=created.tag_id, action="create").one()
    assert log.operator == "alice"
    assert '"reviewer": "alice"' in (log.after_json or "")
    assert f'"pre_asset_tag_id": {row.id}' in (log.after_json or "")


def test_approve_new_word_in_static_dir_raises(session):
    """静态目录的取值是固定的，审核通过也不能往里塞新标签。"""
    row = _seed_row(session, candidate_name="静态目录新词")
    row.candidate_dir_id = "dir_genre"          # dir_static
    session.commit()
    with pytest.raises(ValueError, match="静态目录"):
        apply_review(session, row.id, "approve", "alice", None)


def test_approve_new_word_scope_mismatch_raises(session):
    """挂载粒度必须落在目录的 applicable_scope 里（dir_scene 只能挂 scene）。"""
    row = _seed_row(session, candidate_name="粒度不符新词")
    row.candidate_dir_id = "dir_scene"          # dir_dynamic，scope=scene
    row.target_type = "content"
    session.commit()
    with pytest.raises(ValueError, match="挂载粒度"):
        apply_review(session, row.id, "approve", "alice", None)


def test_merge_appends_alias_to_tag_define(session):
    """相似并入 → 原候选名写进目标标签的 aliases。"""
    from tagplatform.db.models import TagDefine

    row = _seed_row(session, candidate_name="报仇")
    apply_review(session, row.id, "merge", "alice",
                 {"merge_to_tag_id": "tag_revenge"})
    target = session.get(TagDefine, "tag_revenge")
    assert "报仇" in (target.aliases or "")
    # 并入是幂等的：同一别名不会重复追加
    assert target.aliases.count("报仇") == 1


# ---------- 正式挂载（写 asset_tag） ----------
#
# 事故背景：`asset_tag` 是"审核通过后的正式挂载表"，资产详情/标签库的挂载
# 都读它。这段写在合并 main 时整块丢了，于是审核一直在通过、`asset_tag`
# 却永远是 0 行——界面上看着正常（详情页读的是 pre_asset_tag），但正式挂载
# 关系一条都没有。下面三条钉住写入、幂等与冗余名刷新。


def test_approve_new_word_mounts_asset_tag(session):
    """新词通过 → 除 tag_define 外还要写一行 asset_tag 挂载。"""
    from tagplatform.db.models import AssetTag, TagDefine

    row = _seed_row(session, candidate_name="时间循环")
    apply_review(session, row.id, "approve", "alice", None)
    created = (session.query(TagDefine)
               .filter_by(dir_id="dir_theme", tag_name="时间循环").one())
    mount = session.query(AssetTag).one()
    assert (mount.asset_id, mount.target_type, mount.target_id) == ("a1", "content", "a1")
    assert mount.tag_id == created.tag_id
    assert mount.tag_name == "时间循环"          # 冗余拷贝取 define 现值
    assert mount.created_at is not None


def test_approve_catalog_row_mounts_and_records_alias(session):
    """命中库内标签的候选（judge 判重替换/人工改成库内名）：
    原候选名落成别名，同时写正式挂载行。"""
    from tagplatform.db.models import AssetTag, TagDefine

    row = _seed_row(session, origin="closed", tag_id="tag_revenge")
    row.candidate_alias = "报仇雪恨"
    session.commit()
    apply_review(session, row.id, "approve", "alice", None)
    target = session.get(TagDefine, "tag_revenge")
    assert "报仇雪恨" in (target.aliases or "")
    mount = session.query(AssetTag).one()
    assert mount.tag_id == "tag_revenge"
    assert mount.tag_name == target.tag_name


def test_reject_writes_no_mount(session):
    """驳回不挂载：asset_tag 保持为空。"""
    from tagplatform.db.models import AssetTag

    row = _seed_row(session)
    apply_review(session, row.id, "reject", "bob", {"comment": "证据不足"})
    assert session.query(AssetTag).count() == 0


def test_mount_is_idempotent(session):
    """同一资产同一标签重复挂载不会多插行（重复审核/同片多候选）。"""
    from tagplatform.review.service import _mount_asset_tag
    from tagplatform.db.models import AssetTag

    row = _seed_row(session)
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    assert _mount_asset_tag(session, row, tag_id="tag_revenge",
                            tag_name="复仇", now=now) is True
    session.commit()
    assert _mount_asset_tag(session, row, tag_id="tag_revenge",
                            tag_name="复仇", now=now) is False
    assert session.query(AssetTag).count() == 1


def test_refresh_mounted_tag_names_syncs_redundant_copy(session):
    """定义侧改名后，挂载行的 tag_name 冗余拷贝要被刷新（doc §6.3）。"""
    from tagplatform.db.models import AssetTag, TagDefine
    from tagplatform.review import refresh_mounted_tag_names

    row = _seed_row(session)
    apply_review(session, row.id, "approve", "alice", None)
    define = session.get(TagDefine, session.query(AssetTag).one().tag_id)
    define.tag_name = "时间循环（正式名）"
    session.commit()

    assert refresh_mounted_tag_names(session, define.tag_id) == 1
    assert session.query(AssetTag).one().tag_name == "时间循环（正式名）"


def test_refresh_unknown_tag_raises(session):
    from tagplatform.review import refresh_mounted_tag_names

    with pytest.raises(ValueError, match="标签定义不存在"):
        refresh_mounted_tag_names(session, "tag_ghost")
