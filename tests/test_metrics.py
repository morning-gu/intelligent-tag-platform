"""质量指标测试（v2 重写，Task 9）：review_status 终态分布 + 目录覆盖。

核心层（session fixture，内存库，纯读函数直接断言）：
- 空库语义：pending_count 0、review_stats 三键 {count: 0, ratio: None}
  （分母 = pre 总行数，0 → None——"暂无"）、generated_pass_rate/
  evidence_coverage None、task_stats 计数 0 + 占比/时长 None；
- 已知小型数据集精确数字断言：review_stats 三键计数与 ratio（分母统一
  = pre 总行数）、merged 取 change_log 事件数（与经 merge 审核的行数
  刻意错开，钉死事件数口径）、生成产出通过率、evidence 覆盖率、任务
  统计（含 aware/naive 混存时长均值）；
- merged 事件口径专项：多行经 merge 审核但仅 1 条日志 → count == 1；
  行存在但无 merge 事件 → {count: 0, ratio: 0.0}（分母非 0 计 0 返回
  0.0 而非 None）；
- 分桶边界钉死：左闭右开 [lo, hi)——边界值归右侧桶（0.5 → "0.5-0.6"）、
  1.0 clamp 到末桶；0.3/0.7 为浮点乘法陷阱值（int(0.3*10)==2）必须正确
  归桶；None confidence 不计入；零计数桶保留；buckets 可调（宽度 0.2
  的 5 桶）；
- 目录覆盖三级归属（doc §6.4 归置时序）：行带 tag_id → tag_define →
  目录（优先于 candidate_dir_id——合入新建的行两者并存时按并入的定义
  目录归属）；候选行（tag_id 空）→ candidate_dir_id → 目录；两者皆无
  → "未归置"桶；approved 按 review_status=='approved' 计数；按 total
  降序（同数按 dir_name 升序决胜）；空库 → 空列表。

路由层（client fixture，真 app + 临时文件库，与核心层共用同一批已知
数据集播种函数）：summary / confidence-distribution / dir-coverage
三端点 200 精确 JSON、buckets 查询参数透传与校验、旧端点 tag-type-
coverage 退役 404、未登录 401（create_app protected_deps 统一挂载验证）。
"""

from datetime import datetime, timezone
from itertools import count

import pytest
from fastapi.testclient import TestClient

from tagplatform.db.models import (
    MediaAsset,
    PreAssetTag,
    TagCatalog,
    TagChangeLog,
    TagDefine,
    TagGenerationTask,
)
from tagplatform.metrics import (
    compute_metrics,
    confidence_distribution,
    tag_dir_coverage,
)

_NOW = datetime(2026, 11, 1, 12, 0, 0)

# 种子任务时间戳（时长可控：30s / 10s / 5s；t1 特意用 aware 时间戳——
# runner 落库即 aware，与 naive 行混存时均值仍须正确）
_T0 = datetime(2026, 11, 1, 12, 0, 0)
_T0_AWARE = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)

# open 自由候选行 candidate_name 自动编号（部分唯一索引防同资产重名）
_candidate_seq = count()


# ---------- 播种辅助 ----------


def _seed_parents(s, assets=("a1", "a2", "a3")):
    """FK 父行：媒体资产 + 两目录（dir_1 悬疑目录 / dir_2 情感目录）+
    两目录下的标签定义（tag_1 悬疑 / tag_2 温暖）。"""
    for asset_id in assets:
        s.add(MediaAsset(asset_id=asset_id, asset_type="movie",
                         title=f"影片{asset_id}", created_at=_NOW))
    s.add(TagCatalog(dir_id="dir_1", dir_name="悬疑目录",
                     dir_type="dir_static", description="以悬念和谜题为核心"))
    s.add(TagCatalog(dir_id="dir_2", dir_name="情感目录",
                     dir_type="dir_static", description="温情治愈氛围"))
    s.flush()  # 目录行先落库（tag_define.dir_id 外键）
    s.add(TagDefine(tag_id="tag_1", tag_name="悬疑", tag_type="val_fixed",
                    description="以悬念为核心", dir_id="dir_1"))
    s.add(TagDefine(tag_id="tag_2", tag_name="温暖", tag_type="val_fixed",
                    description="温情治愈", dir_id="dir_2"))
    s.flush()


def _add_pre(s, asset_id, *, origin="open", status="pending", tag_id=None,
             candidate_dir_id=None, confidence=0.5, evidence_json="[]",
             candidate_name=None) -> PreAssetTag:
    """单条 pre_asset_tag 行（content 粒度，target_id = asset_id）。

    closed 行传 origin="closed"+tag_id（候选字段清空）；open 自由候选行
    candidate_name 自动编号（不传即生成）；open 归一命中/合并行传 tag_id
    （candidate_name 保留可溯，doc §6.2 并入既有口径）。"""
    is_closed = origin == "closed"
    name = None if is_closed else (candidate_name
                                   or f"候选{next(_candidate_seq)}")
    row = PreAssetTag(
        asset_id=asset_id,
        target_type="content",
        target_id=asset_id,
        tag_id=tag_id,
        candidate_name=name,
        candidate_dir_id=candidate_dir_id,
        candidate_description=None if is_closed else "候选描述",
        origin=origin,
        relevance_score=0.9 if tag_id else None,
        confidence=confidence,
        evidence_json=evidence_json,
        task_id=None,
        review_status=status,
        created_at=_NOW,
        updated_at=_NOW,
    )
    s.add(row)
    s.flush()
    return row


def _add_change_log(s, tag_id, action):
    """单条 tag_change_log（after_json 最小占位；merge 事件由 metrics 计数）。"""
    s.add(TagChangeLog(tag_id=tag_id, action=action, before_json=None,
                       after_json="{}", operator="admin", created_at=_NOW))
    s.flush()


def _seed_tasks(s):
    """4 个生成任务（时长 30s/10s/5s + 1 个 queued 无时长）。"""
    s.add(TagGenerationTask(task_id="t1", asset_id="a1", mode="closed",
                            status="succeeded", created_at=_T0_AWARE,
                            finished_at=datetime(2026, 11, 1, 12, 0, 30,
                                                 tzinfo=timezone.utc)))
    s.add(TagGenerationTask(task_id="t2", asset_id="a2", mode="open",
                            status="succeeded", created_at=_T0,
                            finished_at=datetime(2026, 11, 1, 12, 0, 10)))
    s.add(TagGenerationTask(task_id="t3", asset_id="a1", mode="closed",
                            status="failed", created_at=_T0,
                            finished_at=datetime(2026, 11, 1, 12, 0, 5)))
    s.add(TagGenerationTask(task_id="t4", asset_id="a2", mode="open",
                            status="queued", created_at=_T0))


def _seed_metrics_world(s):
    """已知小型数据集（7 行 pre_asset_tag + 2 条变更日志 + 4 个生成任务）。

    行清单与归属（分母统一 = 7 行）：
    - 行1 a1 closed tag_1 pending；行2 a1 open 候选 pending → pending 2
    - 行3 a2 open tag_1 approved（并入既有——不落日志）；行4 a1 closed
      tag_2 approved；行7 a3 open tag_2+candidate_dir_id approved（合入
      新建——落 1 条 merge 日志）→ approved 3
    - 行5 a2 open 候选 rejected；行6 a3 closed tag_1 rejected → rejected 2
    - 经 merge 动作的行共 2（行3/行7）但日志仅 1 条 → merged 事件数 1，
      与行数刻意错开（钉死事件数口径）；另一条 action='create' 日志不计

    期望值（WORLD_SUMMARY，session 级与 API 级测试共用）：
    - pending_count = 2
    - review_stats：approved 3/7、rejected 2/7、merged 1/7——三键 ratio
      分母统一为 pre 总行数 7（merged 是事件数、不构成独立分母）
    - 生成产出通过率：pre 全部为生成产出（closed/open），已终审 5 行
      （approved 3 + rejected 2）、其中 approved 3 → 3/5
    - evidence 非空非 [] 的行：行3、行4、行7 → 3/7
    - 任务 4 个：succeeded×2（30s/10s）、failed×1（5s）、queued×1（无时长）→
      success_rate 2/4 = 0.5、avg_duration_ms = (30000+10000+5000)/3 = 15000
    """
    _seed_parents(s)
    _add_pre(s, "a1", origin="closed", tag_id="tag_1", status="pending")  # 行1
    _add_pre(s, "a1", status="pending", confidence=0.3,
             evidence_json=None)                                          # 行2
    _add_pre(s, "a2", status="approved", tag_id="tag_1", confidence=0.7,
             evidence_json='[{"scene_id": "s1"}]')                        # 行3
    _add_pre(s, "a1", origin="closed", tag_id="tag_2", status="approved",
             confidence=0.9, evidence_json='["台词提及温暖"]')             # 行4
    _add_pre(s, "a2", status="rejected", confidence=0.1,
             evidence_json="[]")                                          # 行5
    _add_pre(s, "a3", origin="closed", tag_id="tag_1", status="rejected",
             confidence=0.2)                                              # 行6
    _add_pre(s, "a3", status="approved", tag_id="tag_2",
             candidate_dir_id="dir_2", confidence=0.4,
             evidence_json='[{"scene_id": "s2"}]')                        # 行7
    _add_change_log(s, "tag_2", "merge")   # 行7 合入新建的唯一 merge 事件
    _add_change_log(s, "tag_1", "create")  # 干扰项：非 merge 动作不计入
    _seed_tasks(s)
    s.commit()


# _seed_metrics_world 的期望 summary（session 级与 API 级测试共用；
# 浮点经 pytest.approx 比较，None 原样相等）
WORLD_SUMMARY = {
    "pending_count": 2,
    "review_stats": {
        "approved": {"count": 3, "ratio": 3 / 7},
        "rejected": {"count": 2, "ratio": 2 / 7},
        "merged": {"count": 1, "ratio": 1 / 7},
    },
    "generated_pass_rate": 3 / 5,
    "evidence_coverage": 3 / 7,
    "task_stats": {
        "total": 4,
        "succeeded": 2,
        "failed": 1,
        "success_rate": 0.5,
        "avg_duration_ms": 15000.0,
    },
}


def _assert_summary_matches(actual, expected):
    """summary 逐字段比较：整型/None 精确相等，浮点经 pytest.approx
    （pytest.approx 不支持嵌套 dict，故手工展开）。"""
    assert actual["pending_count"] == expected["pending_count"]
    for key in ("approved", "rejected", "merged"):
        stat = actual["review_stats"][key]
        want = expected["review_stats"][key]
        assert stat["count"] == want["count"]
        if want["ratio"] is None:
            assert stat["ratio"] is None
        else:
            assert stat["ratio"] == pytest.approx(want["ratio"])
    for key in ("generated_pass_rate", "evidence_coverage"):
        if expected[key] is None:
            assert actual[key] is None
        else:
            assert actual[key] == pytest.approx(expected[key])
    for key in ("total", "succeeded", "failed"):
        assert actual["task_stats"][key] == expected["task_stats"][key]
    for key in ("success_rate", "avg_duration_ms"):
        if expected["task_stats"][key] is None:
            assert actual["task_stats"][key] is None
        else:
            assert (actual["task_stats"][key]
                    == pytest.approx(expected["task_stats"][key]))


# ---------- 核心层：compute_metrics ----------


def test_compute_metrics_empty_db(session):
    """空库：计数 0、占比类全 None（分母 0 → "暂无"）、时长无值。"""
    assert compute_metrics(session) == {
        "pending_count": 0,
        "review_stats": {
            "approved": {"count": 0, "ratio": None},
            "rejected": {"count": 0, "ratio": None},
            "merged": {"count": 0, "ratio": None},
        },
        "generated_pass_rate": None,
        "evidence_coverage": None,
        "task_stats": {
            "total": 0,
            "succeeded": 0,
            "failed": 0,
            "success_rate": None,
            "avg_duration_ms": None,
        },
    }


def test_compute_metrics_world_exact_numbers(session):
    """已知数据集精确数字：各字段与手算期望逐一相等（含 aware/naive 混存
    时长均值 15000ms；三键 ratio 分母统一 = pre 总行数 7）。"""
    _seed_metrics_world(session)
    _assert_summary_matches(compute_metrics(session), WORLD_SUMMARY)


def test_merged_counts_change_log_events_not_rows(session):
    """merged 按事件计（多行合并只看日志条数）：2 行经 merge 动作终审
    approved（1 并入既有不落日志 + 1 合入新建落 1 条日志）→
    merged.count == 1 ≠ 2——与行数解耦（doc §6.2 指标口径）。"""
    _seed_parents(session, assets=("a1", "a2"))
    _add_pre(session, "a1", status="approved", tag_id="tag_1")  # 并入既有
    _add_pre(session, "a2", status="approved", tag_id="tag_2",
             candidate_dir_id="dir_2")                          # 合入新建
    _add_change_log(session, "tag_2", "merge")
    session.commit()
    assert compute_metrics(session)["review_stats"]["merged"]["count"] == 1


def test_review_stats_zero_events_with_rows(session):
    """pre 行存在但无 merge 事件 → merged {count: 0, ratio: 0.0}（分母非 0
    计 0 返回 0.0 而非 None——"暂无"仅留给分母缺失）；rejected 同理。"""
    _seed_parents(session, assets=("a1",))
    _add_pre(session, "a1", status="approved", tag_id="tag_1")
    _add_pre(session, "a1", status="pending")
    session.commit()
    stats = compute_metrics(session)["review_stats"]
    assert stats["approved"] == {"count": 1, "ratio": pytest.approx(0.5)}
    assert stats["rejected"] == {"count": 0, "ratio": 0.0}
    assert stats["merged"] == {"count": 0, "ratio": 0.0}


def test_generated_pass_rate_none_without_terminal(session):
    """pre 无已终审行 → None（分母 0 → "暂无"，pending 不进分母）。"""
    _seed_parents(session, assets=("a1",))
    _add_pre(session, "a1", status="pending")
    _add_pre(session, "a1", status="pending")
    session.commit()
    assert compute_metrics(session)["generated_pass_rate"] is None


def test_generated_pass_rate_full_approval(session):
    """pre 终审行全部 approved → 通过率 1.0。"""
    _seed_parents(session, assets=("a1",))
    _add_pre(session, "a1", status="approved", tag_id="tag_1")
    _add_pre(session, "a1", origin="closed", tag_id="tag_2", status="approved")
    session.commit()
    assert compute_metrics(session)["generated_pass_rate"] == 1.0


def test_task_stats_without_terminal_tasks(session):
    """全部任务未终态：avg_duration_ms None、success_rate 0.0（total 非零
    不算分母缺失，队列中任务计入 total）。"""
    _seed_parents(session, assets=("a1",))
    session.add(TagGenerationTask(task_id="t1", asset_id="a1", mode="closed",
                                  status="queued", created_at=_T0))
    session.add(TagGenerationTask(task_id="t2", asset_id="a1", mode="open",
                                  status="running", created_at=_T0))
    session.commit()
    assert compute_metrics(session)["task_stats"] == {
        "total": 2,
        "succeeded": 0,
        "failed": 0,
        "success_rate": 0.0,
        "avg_duration_ms": None,
    }


# ---------- 核心层：confidence_distribution ----------


def test_confidence_distribution_empty_db(session):
    """空库：恒返回全部 10 桶（0 计数桶保留），标签一位小数固定文案。"""
    assert confidence_distribution(session) == [
        {"bucket": "0.0-0.1", "count": 0},
        {"bucket": "0.1-0.2", "count": 0},
        {"bucket": "0.2-0.3", "count": 0},
        {"bucket": "0.3-0.4", "count": 0},
        {"bucket": "0.4-0.5", "count": 0},
        {"bucket": "0.5-0.6", "count": 0},
        {"bucket": "0.6-0.7", "count": 0},
        {"bucket": "0.7-0.8", "count": 0},
        {"bucket": "0.8-0.9", "count": 0},
        {"bucket": "0.9-1.0", "count": 0},
    ]


def test_confidence_distribution_boundaries(session):
    """分桶边界钉死：左闭右开 [lo, hi)——边界值 0.1/0.5 归右侧桶、1.0
    clamp 到末桶；0.3/0.7 为浮点乘法陷阱（int(0.3*10)==2、
    int(0.7*10)==6）必须正确归桶；None confidence 不计入。"""
    _seed_parents(session, assets=("a1",))
    for conf in (0.05, 0.1, 0.3, 0.5, 0.7, 1.0, None):
        _add_pre(session, "a1", confidence=conf)
    session.commit()
    assert confidence_distribution(session) == [
        {"bucket": "0.0-0.1", "count": 1},  # 0.05
        {"bucket": "0.1-0.2", "count": 1},  # 0.1 边界值归右桶
        {"bucket": "0.2-0.3", "count": 0},
        {"bucket": "0.3-0.4", "count": 1},  # 0.3 浮点陷阱
        {"bucket": "0.4-0.5", "count": 0},
        {"bucket": "0.5-0.6", "count": 1},  # 0.5 边界值钉死归右桶
        {"bucket": "0.6-0.7", "count": 0},
        {"bucket": "0.7-0.8", "count": 1},  # 0.7 浮点陷阱
        {"bucket": "0.8-0.9", "count": 0},
        {"bucket": "0.9-1.0", "count": 1},  # 1.0 clamp 末桶（None 未计入）
    ]


def test_confidence_distribution_custom_buckets(session):
    """buckets=5：宽度 0.2，边界同样左闭右开（0.2 → 第二桶）。"""
    _seed_parents(session, assets=("a1",))
    for conf in (0.19, 0.2, 0.65, 1.0):
        _add_pre(session, "a1", confidence=conf)
    session.commit()
    assert confidence_distribution(session, buckets=5) == [
        {"bucket": "0.0-0.2", "count": 1},  # 0.19
        {"bucket": "0.2-0.4", "count": 1},  # 0.2 边界归右
        {"bucket": "0.4-0.6", "count": 0},
        {"bucket": "0.6-0.8", "count": 1},  # 0.65
        {"bucket": "0.8-1.0", "count": 1},  # 1.0 末桶
    ]


# ---------- 核心层：tag_dir_coverage ----------


# 已知覆盖数据集的期望（_seed_coverage_world，session 级与 API 级共用）：
# 悬疑目录 3 行（行7 合入新建行 tag_id 优先于 candidate_dir_id）、
# 情感目录 2 行（候选行 candidate_dir_id 归属）、未归置 2 行；
# 同 total 按 dir_name 升序决胜（情 U+60C5 < 未 U+672A）
DIR_COVERAGE_EXPECTED = [
    {"dir_name": "悬疑目录", "total": 3, "approved": 1},
    {"dir_name": "情感目录", "total": 2, "approved": 1},
    {"dir_name": "未归置", "total": 2, "approved": 1},
]


def _seed_coverage_world(s):
    """目录覆盖已知数据集（7 行，跨三资产）。

    悬疑目录：closed tag_1 行 ×2（1 approved）+ 行7 合入新建行（tag_1 与
    candidate_dir_id=dir_2 并存——tag_id 优先，归悬疑目录，钉死归属优先级）；
    情感目录：closed tag_2 行（rejected）+ 候选行 candidate_dir_id=dir_2
    （approved）；未归置：两行自由候选（tag_id/candidate_dir_id 皆无）。
    """
    _seed_parents(s)
    _add_pre(s, "a1", origin="closed", tag_id="tag_1", status="pending")
    _add_pre(s, "a2", origin="closed", tag_id="tag_1", status="approved")
    _add_pre(s, "a3", origin="closed", tag_id="tag_2", status="rejected")
    _add_pre(s, "a3", status="approved", candidate_dir_id="dir_2")
    _add_pre(s, "a1", status="pending")
    _add_pre(s, "a1", status="approved")
    _add_pre(s, "a3", status="pending", tag_id="tag_1",
             candidate_dir_id="dir_2")  # 行7：tag_id 优先于 candidate_dir_id
    s.commit()


def test_tag_dir_coverage_world(session):
    """三级归属：tag_id→define→目录（优先于 candidate_dir_id）、候选行按
    candidate_dir_id、皆无归"未归置"；按 total 降序（同数按 dir_name 升序
    决胜）。"""
    _seed_coverage_world(session)
    assert tag_dir_coverage(session) == DIR_COVERAGE_EXPECTED


def test_tag_dir_coverage_only_unplaced_rows(session):
    """全部行都无目录归属 → 仅"未归置"桶（无归属行不产生空目录行）。"""
    _seed_parents(session, assets=("a1",))
    _add_pre(session, "a1", status="approved")
    _add_pre(session, "a1", status="pending")
    session.commit()
    assert tag_dir_coverage(session) == [
        {"dir_name": "未归置", "total": 2, "approved": 1},
    ]


def test_tag_dir_coverage_empty_db(session):
    """空库 → 空列表（"未归置"桶仅在有未归属行时出现）。"""
    assert tag_dir_coverage(session) == []


# ---------- 路由层 ----------


def test_metrics_summary_endpoint(client):
    """GET /metrics/summary：已知数据集 → 200 精确 JSON（与核心层同期望）。"""
    factory = client.app.state.session_factory
    with factory() as s:
        _seed_metrics_world(s)
    resp = client.get("/api/v1/metrics/summary")
    assert resp.status_code == 200
    _assert_summary_matches(resp.json(), WORLD_SUMMARY)


def test_confidence_distribution_endpoint(client):
    """GET /metrics/confidence-distribution：默认 10 桶全返回、计数守恒。"""
    factory = client.app.state.session_factory
    with factory() as s:
        _seed_parents(s, assets=("a1",))
        for conf in (0.05, 0.5, 0.95):
            _add_pre(s, "a1", confidence=conf)
        s.commit()
    resp = client.get("/api/v1/metrics/confidence-distribution")
    assert resp.status_code == 200
    body = resp.json()
    # 期望 = 全 10 桶计数（0 计数桶保留），三桶各 1
    expected = {d["bucket"]: 0 for d in body}
    expected.update({"0.0-0.1": 1, "0.5-0.6": 1, "0.9-1.0": 1})
    assert {d["bucket"]: d["count"] for d in body} == expected
    assert sum(d["count"] for d in body) == 3  # 全桶返回、计数守恒


def test_confidence_distribution_endpoint_buckets_param(client):
    """buckets 查询参数透传核心层（buckets=5 → 宽度 0.2 的 5 桶）。"""
    factory = client.app.state.session_factory
    with factory() as s:
        _seed_parents(s, assets=("a1",))
        _add_pre(s, "a1", confidence=0.5)
        s.commit()
    resp = client.get("/api/v1/metrics/confidence-distribution",
                      params={"buckets": 5})
    assert resp.status_code == 200
    assert resp.json() == [
        {"bucket": "0.0-0.2", "count": 0},
        {"bucket": "0.2-0.4", "count": 0},
        {"bucket": "0.4-0.6", "count": 1},
        {"bucket": "0.6-0.8", "count": 0},
        {"bucket": "0.8-1.0", "count": 0},
    ]


def test_confidence_distribution_endpoint_bad_buckets_422(client):
    """buckets 非法（0）→ 422（Query 校验）。"""
    resp = client.get("/api/v1/metrics/confidence-distribution",
                      params={"buckets": 0})
    assert resp.status_code == 422


def test_dir_coverage_endpoint(client):
    """GET /metrics/dir-coverage：已知数据集 → 200 精确 JSON（v1
    tag-type-coverage 端点更名，前端消费 {dir_name, total, approved}）。"""
    factory = client.app.state.session_factory
    with factory() as s:
        _seed_coverage_world(s)
    resp = client.get("/api/v1/metrics/dir-coverage")
    assert resp.status_code == 200
    assert resp.json() == DIR_COVERAGE_EXPECTED


def test_tag_type_coverage_endpoint_retired_404(client):
    """旧端点 /metrics/tag-type-coverage 随 tag_type 维度退役 → 404
    （路由表无旧路径残留）。"""
    resp = client.get("/api/v1/metrics/tag-type-coverage")
    assert resp.status_code == 404


def test_metrics_endpoints_require_login(client):
    """未登录 401：新 TestClient 不带 cookie（protected_deps 统一挂载覆盖
    metrics router）。"""
    with TestClient(client.app) as fresh:
        for path in ("/api/v1/metrics/summary",
                     "/api/v1/metrics/confidence-distribution",
                     "/api/v1/metrics/dir-coverage"):
            assert fresh.get(path).status_code == 401
