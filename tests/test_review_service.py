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


def test_approve_new_word_without_dir_raises(session):
    row = _seed_row(session, candidate_name="无目录新词")
    row.candidate_dir_id = None
    session.commit()
    with pytest.raises(ValueError, match="没有归属目录"):
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


def test_approve_new_word_without_dir_raises(session):
    row = _seed_row(session, candidate_name="无目录新词")
    row.candidate_dir_id = None
    session.commit()
    with pytest.raises(ValueError, match="没有归属目录"):
        apply_review(session, row.id, "approve", "alice", None)
