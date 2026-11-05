"""审核操作服务测试（v2 重写，Task 8——行即日志，doc §6.2 映射表逐字执行）。

覆盖契约（brief + doc §6.2 + 裁定口径）：
- approve：pre 行 approved + reviewer/comment → 复制挂载 asset_tag
  （tag_name 取 tag_define.tag_name 现值；candidate_name 不参与挂载值；
  target_type/target_id 沿 pre 行）；
- reject：pre 行 rejected + reviewer/comment，无挂载；
- modify：candidate_name/candidate_description 更新（payload 键同名，
  comment 记录修改内容）→ 直接走 approve 流程（一步到位，不回 pending
  ——v1"重置后再审"语义退役）；字段 strip/非空/≤200/类型校验沿用 v1
  （Review Focus 5 风格）；
- merge 两支：并入既有（tag_id 赋值、candidate_name 原文保留可溯、不建
  新 define——含同名候选 Review Focus 3）/ 合入新建（建 TagDefine
  val_fixed + tag_change_log action='merge' + candidate_dir_id 赋值；
  同目录同名 → 422 提示改走并入既有）；
- 冲突两类分开测：终态行再审 → ConflictError("该标签已被处理")；approve/
  merge 复制挂载撞 ux_asset_tag → ConflictError("该资产已挂此标签")
  （Ruling 3）；并入既有撞 ux_pre_tag_catalog（同资产已有同 tag_id 行）
  同映射；
- open 自由候选行（tag_id 空）直接 approve/modify → ValueError（422——
  asset_tag.tag_id 非空约束的前置化：候选名无法直接挂载）；
- 服务内 commit（rollback 后行状态与挂载行仍存活）；载荷校验全部前置
  （零副作用：失败后行字段/状态/挂载/变更日志均原样）。

实现注意：跨表插入分步 flush；pytest.raises 冲突用例先 commit 父行。
"""

from datetime import datetime, timezone

import pytest

from tagplatform.db.models import (
    AssetTag,
    MediaAsset,
    PreAssetTag,
    TagCatalog,
    TagChangeLog,
    TagDefine,
)
from tagplatform.review import ConflictError, apply_review

_NOW = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _seed_parents(session):
    """补齐外键父行（pre_asset_tag.asset_id → media_asset、tag_id →
    tag_define、candidate_dir_id → tag_catalog；目录行先 flush 再插定义）。
    refresh_mounted_tag_names 用例（Task 5 保留）依赖同一批父行。"""
    session.add(MediaAsset(asset_id="a1", asset_type="movie", title="测试影片",
                           created_at=_NOW))
    session.add(TagCatalog(dir_id="dir_1", dir_name="剧情", dir_type="dir_static",
                           description="剧情类目录", parent_id=None, aliases=None,
                           applicable_scope="content", embedding=None,
                           created_at=_NOW, updated_at=_NOW))
    session.flush()
    session.add(TagDefine(tag_id="tag_1", tag_name="悬疑", tag_type="val_fixed",
                          description="以悬念为核心", dir_id="dir_1", aliases=None,
                          status="active", embedding=None,
                          created_at=_NOW, updated_at=_NOW))
    session.add(TagDefine(tag_id="tag_2", tag_name="喜剧", tag_type="val_fixed",
                          description="以笑点为核心", dir_id="dir_1", aliases=None,
                          status="active", embedding=None,
                          created_at=_NOW, updated_at=_NOW))
    session.commit()


def _seed_row(session, origin="open", status="pending", tag_id=None,
              candidate_name="多线叙事", candidate_dir_id=None,
              candidate_description="候选描述", confidence=0.8) -> PreAssetTag:
    """播种一行 pre_asset_tag 并提交（拿到行 id；时间戳显式赋值）。

    默认 open 自由候选行（candidate_name）；closed 行传 origin="closed" +
    tag_id + candidate_name=None；open 归一命中行传 tag_id（candidate 并存）。"""
    row = PreAssetTag(
        asset_id="a1", target_type="content", target_id="a1",
        tag_id=tag_id, candidate_name=candidate_name,
        candidate_dir_id=candidate_dir_id,
        candidate_description=candidate_description,
        origin=origin, relevance_score=0.9 if tag_id else None,
        confidence=confidence, evidence_json="[]", task_id=None,
        review_status=status, created_at=_NOW, updated_at=_NOW,
    )
    session.add(row)
    session.commit()
    return row


# ---------- approve ----------


def test_approve_marks_row_and_copies_mount(session):
    """approve：行 approved + reviewer 落行 → 复制挂载 asset_tag（tag_name
    取 define 现值、target 沿 pre 行）；无 payload → comment 为 None（空
    comment 合法）。"""
    row = _seed_row(session, origin="closed", tag_id="tag_1", candidate_name=None)
    returned = apply_review(session, row.id, "approve", "alice", None)
    assert returned.review_status == "approved"
    assert returned.reviewer_id == "alice"
    assert returned.review_comment is None
    assert returned.updated_at is not None

    mounts = session.query(AssetTag).all()
    assert len(mounts) == 1
    mount = mounts[0]
    assert mount.asset_id == "a1"
    assert mount.target_type == "content" and mount.target_id == "a1"
    assert mount.tag_id == "tag_1"
    assert mount.tag_name == "悬疑"  # tag_define.tag_name 现值（doc §3.6）


def test_approve_mount_uses_current_define_name(session):
    """挂载 tag_name 是 define 名的现值拷贝：审核前改定义名，挂载取新值
    （val_fixed 冗余拷贝契约，doc §6.3 同源）。"""
    define = session.get(TagDefine, "tag_1")
    define.tag_name = "悬疑改"
    session.commit()
    row = _seed_row(session, origin="closed", tag_id="tag_1", candidate_name=None)
    apply_review(session, row.id, "approve", "alice", None)
    mount = session.query(AssetTag).one()
    assert mount.tag_name == "悬疑改"


def test_approve_open_hit_row_ignores_candidate_name(session):
    """open 归一命中行（tag_id 与 candidate_name 并存）：挂载 tag_name 仍取
    define 名，不用候选名（candidate_* 只留在 pre 行可溯）。"""
    row = _seed_row(session, origin="open", tag_id="tag_2",
                    candidate_name="喜剧变体")
    apply_review(session, row.id, "approve", "alice", {"comment": "ok"})
    mount = session.query(AssetTag).one()
    assert mount.tag_id == "tag_2" and mount.tag_name == "喜剧"


def test_approve_empty_comment_payload_ok(session):
    """空 comment 合法：payload 只带空串 comment → review_comment 落空串。"""
    row = _seed_row(session, origin="closed", tag_id="tag_1", candidate_name=None)
    returned = apply_review(session, row.id, "approve", "alice", {"comment": ""})
    assert returned.review_status == "approved"
    assert returned.review_comment == ""


def test_approve_free_candidate_raises(session):
    """open 自由候选行（tag_id 空）直接 approve → ValueError（asset_tag.tag_id
    非空约束的前置化）；零副作用：行原样、无挂载。"""
    row = _seed_row(session)  # 默认自由候选"多线叙事"
    with pytest.raises(ValueError, match="合并"):
        apply_review(session, row.id, "approve", "alice", None)
    fresh = session.get(PreAssetTag, row.id)
    assert fresh.review_status == "pending" and fresh.tag_id is None
    assert session.query(AssetTag).count() == 0


# ---------- reject ----------


def test_reject_marks_row_without_mount(session):
    """reject：行 rejected + reviewer/comment 落行，不复制挂载；自由候选行
    同样可直接驳回（驳回即终审，doc §6.1）。"""
    row = _seed_row(session)
    returned = apply_review(session, row.id, "reject", "bob",
                            {"comment": "证据不足"})
    assert returned.review_status == "rejected"
    assert returned.reviewer_id == "bob"
    assert returned.review_comment == "证据不足"
    assert session.query(AssetTag).count() == 0


# ---------- modify（改候选后通过，一步到位） ----------


def test_modify_updates_candidate_and_approves(session):
    """modify：candidate_name/candidate_description 更新（payload 键同名）+
    直接走 approve 流程——行 approved（不回 pending）、comment 记录修改
    内容、复制挂载 tag_name 取 define 名。"""
    row = _seed_row(session, origin="open", tag_id="tag_2",
                    candidate_name="喜剧变体")
    returned = apply_review(session, row.id, "modify", "alice", {
        "candidate_name": "多线叙事改", "candidate_description": "新描述",
        "comment": "措辞修正"})
    assert returned.candidate_name == "多线叙事改"
    assert returned.candidate_description == "新描述"
    assert returned.review_status == "approved"  # 一步到位，不再回 pending
    assert returned.reviewer_id == "alice"
    assert returned.review_comment == "措辞修正"
    mount = session.query(AssetTag).one()
    assert mount.tag_id == "tag_2" and mount.tag_name == "喜剧"


def test_modify_without_mappable_field_raises(session):
    """空 modify 值（无任何可改字段）→ ValueError（API 层 422）。"""
    row = _seed_row(session, origin="open", tag_id="tag_2")
    with pytest.raises(ValueError):
        apply_review(session, row.id, "modify", "alice", None)
    with pytest.raises(ValueError):
        apply_review(session, row.id, "modify", "alice", {})
    with pytest.raises(ValueError):
        apply_review(session, row.id, "modify", "alice", {"comment": "仅备注"})
    # 零副作用：行未动
    assert session.get(PreAssetTag, row.id).review_status == "pending"


def test_modify_unknown_keys_only_raises(session):
    """v1 词表的别名/未知键（tag_name 等）不再是合法修改键 → ValueError。"""
    row = _seed_row(session, origin="open", tag_id="tag_2")
    with pytest.raises(ValueError):
        apply_review(session, row.id, "modify", "alice",
                     {"irrelevant": "x", "comment": "c"})
    with pytest.raises(ValueError):
        apply_review(session, row.id, "modify", "alice", {"tag_name": "旧词表键"})


def test_modify_blank_string_field_raises(session):
    """Review Focus 5 风格：modify 字符串字段空串/纯空白 → ValueError
    "修改字段不能为空"（API 层 422），零副作用。"""
    row = _seed_row(session, origin="open", tag_id="tag_2")
    for payload in ({"candidate_name": ""},            # 空串
                    {"candidate_description": " \t "}):  # 纯空白
        with pytest.raises(ValueError, match="修改字段不能为空"):
            apply_review(session, row.id, "modify", "alice", payload)
    # 零副作用：行未动
    fresh = session.get(PreAssetTag, row.id)
    assert fresh.candidate_name == "多线叙事" and fresh.review_status == "pending"


def test_modify_overlong_string_field_raises(session):
    """modify 字符串字段 strip 后超 200 字符 → ValueError 带字段名（API 层
    422）；恰好 200 字符可过。"""
    row = _seed_row(session, origin="open", tag_id="tag_2")
    with pytest.raises(ValueError, match="candidate_name"):
        apply_review(session, row.id, "modify", "alice",
                     {"candidate_name": "长" * 201})
    assert session.query(AssetTag).count() == 0  # 零副作用
    # 上限边界：恰好 200 字符合法
    returned = apply_review(session, row.id, "modify", "alice",
                            {"candidate_name": "名" * 200})
    assert returned.candidate_name == "名" * 200
    assert returned.review_status == "approved"


def test_modify_non_string_field_raises(session):
    """modify 字段类型错（非字符串）→ ValueError（API 层 422）。"""
    row = _seed_row(session, origin="open", tag_id="tag_2")
    with pytest.raises(ValueError, match="修改字段必须为字符串"):
        apply_review(session, row.id, "modify", "alice", {"candidate_name": 123})


def test_modify_strips_string_values(session):
    """字符串字段 strip 后落库——前后空白消灭，纯空白名不会再进入库。"""
    row = _seed_row(session, origin="open", tag_id="tag_2")
    returned = apply_review(session, row.id, "modify", "alice",
                            {"candidate_name": "  紧张感  "})
    assert returned.candidate_name == "紧张感"
    assert session.get(PreAssetTag, row.id).candidate_name == "紧张感"


def test_modify_free_candidate_raises(session):
    """open 自由候选行（tag_id 空）modify → ValueError（同 approve：无定义
    可挂载，须先合并入库）；载荷校验仍先于该前置（空字段报空字段错）。"""
    row = _seed_row(session)
    with pytest.raises(ValueError, match="修改字段不能为空"):
        apply_review(session, row.id, "modify", "alice", {"candidate_name": ""})
    with pytest.raises(ValueError, match="合并"):
        apply_review(session, row.id, "modify", "alice", {"candidate_name": "新名"})
    fresh = session.get(PreAssetTag, row.id)
    assert fresh.candidate_name == "多线叙事" and fresh.review_status == "pending"


# ---------- merge·并入既有 ----------


def test_merge_into_existing(session):
    """merge 并入既有：pre 行赋 tag_id、candidate_name 原文保留（可溯）→
    走 approve 流程（approved + 复制挂载）；不建新标签。"""
    row = _seed_row(session)
    returned = apply_review(session, row.id, "merge", "alice",
                            {"merge_to_tag_id": "tag_2", "comment": "并入目录"})
    assert returned.tag_id == "tag_2"
    assert returned.candidate_name == "多线叙事"  # 原文保留可溯（doc §6.2）
    assert returned.review_status == "approved"
    assert returned.reviewer_id == "alice"
    assert returned.review_comment == "并入目录"
    # 不建新标签
    assert session.query(TagDefine).count() == 2
    # 复制挂载 tag_name 取 define 现值
    mount = session.query(AssetTag).one()
    assert mount.tag_id == "tag_2" and mount.tag_name == "喜剧"


def test_merge_into_existing_same_name(session):
    """Review Focus 3：候选名与库内标签同名时并入既有——赋 tag_id 而非新建
    define，不触发 ux_tag_define_dir_name 唯一冲突，挂载正常落行。"""
    row = _seed_row(session, candidate_name="悬疑")  # 与 tag_1（悬疑）同名
    returned = apply_review(session, row.id, "merge", "alice",
                            {"merge_to_tag_id": "tag_1"})
    assert returned.tag_id == "tag_1"
    assert returned.candidate_name == "悬疑"
    assert returned.review_status == "approved"
    assert session.query(TagDefine).count() == 2  # 未新建 define
    mount = session.query(AssetTag).one()
    assert (mount.tag_id, mount.tag_name) == ("tag_1", "悬疑")


def test_merge_unknown_target_raises(session):
    row = _seed_row(session)
    with pytest.raises(ValueError, match="目标标签不存在"):
        apply_review(session, row.id, "merge", "alice",
                     {"merge_to_tag_id": "tag_none"})
    # 零副作用
    fresh = session.get(PreAssetTag, row.id)
    assert fresh.review_status == "pending" and fresh.tag_id is None


def test_merge_missing_payload_raises(session):
    row = _seed_row(session)
    for payload in (None, {}, {"comment": "缺目标"}):
        with pytest.raises(ValueError, match="merge"):
            apply_review(session, row.id, "merge", "alice", payload)
    assert session.get(PreAssetTag, row.id).review_status == "pending"


def test_merge_both_branches_raises(session):
    """两支载荷互斥：同时给 merge_to_tag_id 与 new_tag_name → ValueError。"""
    row = _seed_row(session)
    with pytest.raises(ValueError, match="只能提供"):
        apply_review(session, row.id, "merge", "alice", {
            "merge_to_tag_id": "tag_1",
            "new_tag_name": "新名", "new_dir_id": "dir_1"})
    assert session.get(PreAssetTag, row.id).review_status == "pending"


# ---------- merge·合入新建 ----------


def test_merge_into_new_tag(session):
    """merge 合入新建：建 TagDefine（val_fixed）+ tag_change_log
    (action='merge') → pre 行赋 tag_id/candidate_dir_id → approve 流程
    （candidate_name 原文保留、挂载取新定义名）。"""
    row = _seed_row(session, candidate_name="光污染")
    returned = apply_review(session, row.id, "merge", "alice", {
        "new_tag_name": "视觉冲击", "new_dir_id": "dir_1",
        "new_description": "视效冲击力", "comment": "库内确无对应"})
    # 新建定义：val_fixed + 挂 dir_1
    define = session.get(TagDefine, returned.tag_id)
    assert define is not None
    assert define.tag_name == "视觉冲击"
    assert define.tag_type == "val_fixed"
    assert define.dir_id == "dir_1"
    assert define.description == "视效冲击力"
    assert define.status == "active"
    # 变更日志：action='merge'（metrics 的 merge 计数源，doc §6.2）
    log = session.query(TagChangeLog).one()
    assert log.tag_id == define.tag_id
    assert log.action == "merge"
    assert log.operator == "alice"
    assert log.created_at is not None
    # pre 行：tag_id/candidate_dir_id 赋值，candidate_name 原文保留
    assert returned.tag_id == define.tag_id
    assert returned.candidate_dir_id == "dir_1"
    assert returned.candidate_name == "光污染"
    assert returned.review_status == "approved"
    assert returned.review_comment == "库内确无对应"
    # 复制挂载取新定义名
    mount = session.query(AssetTag).one()
    assert (mount.tag_id, mount.tag_name) == (define.tag_id, "视觉冲击")


def test_merge_into_new_tag_without_optional_description(session):
    """new_description 可选：缺省时新定义 description 落空串（NOT NULL 列）。"""
    row = _seed_row(session)
    returned = apply_review(session, row.id, "merge", "alice", {
        "new_tag_name": "视觉冲击", "new_dir_id": "dir_1"})
    define = session.get(TagDefine, returned.tag_id)
    assert define.description == ""
    assert session.query(TagChangeLog).filter_by(action="merge").count() == 1


def test_merge_into_new_same_name_same_dir_raises(session):
    """Review Focus 3：合入新建撞 ux_tag_define_dir_name（同目录同名）→
    ValueError 提示改走并入既有（API 层 422），零副作用（不建定义）。"""
    row = _seed_row(session, candidate_name="悬疑")
    with pytest.raises(ValueError, match="并入既有"):
        apply_review(session, row.id, "merge", "alice", {
            "new_tag_name": "悬疑", "new_dir_id": "dir_1"})
    assert session.query(TagDefine).count() == 2  # 未新建
    fresh = session.get(PreAssetTag, row.id)
    assert fresh.review_status == "pending" and fresh.tag_id is None
    assert session.query(TagChangeLog).count() == 0


def test_merge_into_new_missing_or_unknown_dir_raises(session):
    row = _seed_row(session)
    with pytest.raises(ValueError, match="new_dir_id"):
        apply_review(session, row.id, "merge", "alice",
                     {"new_tag_name": "视觉冲击"})
    with pytest.raises(ValueError, match="目标目录不存在"):
        apply_review(session, row.id, "merge", "alice",
                     {"new_tag_name": "视觉冲击", "new_dir_id": "dir_none"})
    assert session.query(TagDefine).count() == 2  # 零副作用


def test_merge_into_new_blank_or_overlong_name_raises(session):
    """新标签名沿用 modify 的字符串校验（空/纯空白/超 200）。"""
    row = _seed_row(session)
    with pytest.raises(ValueError, match="新标签名不能为空"):
        apply_review(session, row.id, "merge", "alice",
                     {"new_tag_name": "   ", "new_dir_id": "dir_1"})
    with pytest.raises(ValueError, match="新标签名超长"):
        apply_review(session, row.id, "merge", "alice",
                     {"new_tag_name": "长" * 201, "new_dir_id": "dir_1"})
    with pytest.raises(ValueError, match="新标签描述"):
        apply_review(session, row.id, "merge", "alice", {
            "new_tag_name": "视觉冲击", "new_dir_id": "dir_1",
            "new_description": "  "})
    assert session.get(PreAssetTag, row.id).review_status == "pending"


# ---------- 冲突（两类分开测） ----------


def test_conflict_when_row_terminal(session):
    """终态行再审任何动作 → ConflictError("该标签已被处理")：approved 与
    rejected 两态分别触发（终态不被覆盖，doc §6.1）。"""
    approved = _seed_row(session, origin="closed", tag_id="tag_1",
                         candidate_name=None)
    approved.review_status = "approved"
    session.commit()
    rejected = _seed_row(session, origin="closed", tag_id="tag_2",
                         candidate_name=None)
    rejected.review_status = "rejected"
    session.commit()
    for row in (approved, rejected):
        with pytest.raises(ConflictError, match="该标签已被处理"):
            apply_review(session, row.id, "approve", "alice", None)
        with pytest.raises(ConflictError, match="该标签已被处理"):
            apply_review(session, row.id, "merge", "alice",
                         {"merge_to_tag_id": "tag_1"})


def test_conflict_when_mount_exists(session):
    """挂载唯一冲突（Ruling 3）：approve 复制挂载撞 ux_asset_tag（同资产同
    标签已挂）→ ConflictError("该资产已挂此标签")，回滚后行保持 pending、
    审核字段未落、无重复挂载。"""
    # 父行先 commit，再播挂载行（跨表插入分步 flush）
    session.add(AssetTag(asset_id="a1", target_type="content", target_id="a1",
                         tag_id="tag_1", tag_name="悬疑",
                         created_at=_NOW, updated_at=_NOW))
    session.commit()
    row = _seed_row(session, origin="closed", tag_id="tag_1", candidate_name=None)
    with pytest.raises(ConflictError, match="该资产已挂此标签"):
        apply_review(session, row.id, "approve", "alice", None)
    fresh = session.get(PreAssetTag, row.id)
    assert fresh.review_status == "pending"
    assert fresh.reviewer_id is None and fresh.review_comment is None
    assert session.query(AssetTag).count() == 1  # 无重复挂载


def test_conflict_when_merge_onto_same_tag_row(session):
    """并入既有撞 ux_pre_tag_catalog：同资产已有同 tag_id 行（前一条已并入
    通过）→ ConflictError，回滚后本行保持自由候选原样。"""
    first = _seed_row(session, candidate_name="候选甲")
    apply_review(session, first.id, "merge", "alice",
                 {"merge_to_tag_id": "tag_1"})
    second = _seed_row(session, candidate_name="候选乙")
    with pytest.raises(ConflictError, match="该资产已挂此标签"):
        apply_review(session, second.id, "merge", "alice",
                     {"merge_to_tag_id": "tag_1"})
    fresh = session.get(PreAssetTag, second.id)
    assert fresh.review_status == "pending" and fresh.tag_id is None
    assert fresh.candidate_name == "候选乙"
    # 首条成果不被波及
    assert session.query(AssetTag).count() == 1
    assert session.query(TagDefine).count() == 2


# ---------- 前置与服务内提交 ----------


def test_missing_row_raises_value_error(session):
    with pytest.raises(ValueError, match="标签行不存在"):
        apply_review(session, 999, "approve", "alice", None)


def test_unknown_action_raises(session):
    row = _seed_row(session)
    with pytest.raises(ValueError, match="未知 action"):
        apply_review(session, row.id, "alien", "alice", None)
    assert session.get(PreAssetTag, row.id).review_status == "pending"


def test_service_commits_immediately(session):
    """服务内提交：审核后 rollback，行状态与挂载行仍存活（已落库）。"""
    row = _seed_row(session, origin="closed", tag_id="tag_1", candidate_name=None)
    apply_review(session, row.id, "approve", "alice", {"comment": "ok"})
    session.rollback()  # 撤销一切未提交变更
    assert session.get(PreAssetTag, row.id).review_status == "approved"
    assert session.query(AssetTag).count() == 1


# ---------- tag_name 冗余拷贝刷新（Task 5，doc §6.3） ----------


def test_refresh_mounted_tag_names_syncs_all_mounts(session):
    """改 tag_define.tag_name 后刷新助手：该 tag_id 全部 asset_tag 挂载行
    tag_name 同步为新名，返回更新行数；无挂载 → 0；定义缺失 → ValueError。

    助手契约（本期无 rename 端点，Task 8 rename 流程消费）：调用方先改
    TagDefine.tag_name 并自行写变更日志、提交事务；助手只做挂载侧批量
    UPDATE（flush 不 commit，与调用方同事务原子落库）。
    """
    from tagplatform.review.service import refresh_mounted_tag_names

    session.add(MediaAsset(asset_id="a2", asset_type="movie", title="影片二",
                           created_at=_NOW))
    session.add(MediaAsset(asset_id="a3", asset_type="movie", title="影片三",
                           created_at=_NOW))
    session.flush()  # 跨表插入分步 flush：父行先落库再插挂载行
    # 3 条挂载（content 粒度 target_id = asset_id 本身）
    session.add_all([
        AssetTag(asset_id="a1", target_type="content", target_id="a1",
                 tag_id="tag_1", tag_name="旧名", created_at=_NOW, updated_at=_NOW),
        AssetTag(asset_id="a2", target_type="content", target_id="a2",
                 tag_id="tag_1", tag_name="旧名", created_at=_NOW, updated_at=_NOW),
        AssetTag(asset_id="a3", target_type="content", target_id="a3",
                 tag_id="tag_1", tag_name="旧名", created_at=_NOW, updated_at=_NOW),
    ])
    session.commit()

    define = session.get(TagDefine, "tag_1")
    define.tag_name = "新名"
    session.commit()

    updated = refresh_mounted_tag_names(session, "tag_1")
    assert updated == 3
    # 批量 UPDATE 不同步已加载对象，过期后重读验证挂载行全量同步
    session.expire_all()
    names = {m.tag_name for m in session.query(AssetTag).filter_by(tag_id="tag_1")}
    assert names == {"新名"}

    # 无挂载的标签 → 0；未知定义 → ValueError（API 层映射 422）
    assert refresh_mounted_tag_names(session, "tag_2") == 0
    with pytest.raises(ValueError, match="标签定义不存在"):
        refresh_mounted_tag_names(session, "tag_none")
