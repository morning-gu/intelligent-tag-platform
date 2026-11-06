"""质量指标推导测试：指标口径随 v2 表结构（pre_asset_tag / tag_generation_task）。

口径要点：
- pending / approved / rejected 一律按 `pre_asset_tag` 行状态计数；
- 审核流水表（review_record）不再写入，merged/modified 与 approved 同终态、
  行上不可区分，恒为 0 且占比为 None；
- 新标签通过率的分母是「库外新词（tag_id 为空）且已终审」，分子是其中
  approved 的行——与 origin 无关（origin 只表示 closed/open 来源）；
- tag_type 覆盖率只统计带 tag_id 的行（库外新词尚无类型）。
"""

from datetime import datetime, timezone

import pytest

from conftest import TestClient
from tagplatform.db.models import (
    MediaAsset,
    PreAssetTag,
    TagCatalog,
    TagGenerationTask,
)
from tagplatform.metrics import (
    compute_metrics,
    confidence_distribution,
    tag_type_coverage,
)

_NOW = datetime(2026, 11, 1, 12, 0, 0)

# 种子任务时间戳（时长可控：30s / 10s / 5s；t1 特意用 aware 时间戳——
# runner 落库即 aware，与 naive 行混存时均值仍须正确）
_T0 = datetime(2026, 11, 1, 12, 0, 0)
_T0_AWARE = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)


# ---------- 播种辅助 ----------

_candidate_seq = iter(__import__("itertools").count())


def _seed_parents(s, assets=("a1", "a2")):
    """FK 父行：媒体资产（默认两个）+ 两类型目录标签（genre/mood）。"""
    for asset_id in assets:
        s.add(MediaAsset(asset_id=asset_id, asset_type="movie",
                         title=f"影片{asset_id}", created_at=_NOW))
    s.add(TagCatalog(tag_id="tag_1", tag_name="悬疑", tag_type="genre",
                     description="以悬念和谜题为核心"))
    s.add(TagCatalog(tag_id="tag_2", tag_name="温暖", tag_type="mood",
                     description="温情治愈氛围"))
    s.flush()


def _add_tag(s, asset_id, *, origin="open", status="pending", tag_id=None,
             confidence=0.5, evidence_json="[]"):
    """单条 pre_asset_tag 行（tag_id 为空即库外新词，带候选名）。"""
    row = PreAssetTag(
        asset_id=asset_id,
        target_type="content",
        target_id=asset_id,
        tag_id=tag_id,
        candidate_name=(f"候选{next(_candidate_seq)}" if tag_id is None else None),
        candidate_description=("候选描述" if tag_id is None else None),
        origin=origin,
        relevance_score=None if tag_id is None else 0.9,
        confidence=confidence,
        evidence_json=evidence_json,
        review_status=status,
        created_at=_NOW,
        updated_at=_NOW,
    )
    s.add(row)
    s.flush()
    return row


def _seed_metrics_world(s):
    """已知小型数据集（6 行待审标签 + 4 个生成任务）。

    期望值（WORLD_SUMMARY，session 级与 API 级测试共用）：
    - pending_count = 2（r1 库内 pending、r2 新词 pending）
    - approved 3（r3/r4/r6）、rejected 1（r5）→ 占比 0.75 / 0.25；
      merged、modified 恒 0 / None（v2 无审核流水）
    - 新标签通过率：tag_id 为空的已终审行 = r3(approved)、r5(rejected) →
      1/2 = 0.5
    - evidence 非空非 [] 的行：r3、r4、r6 → 3/6 = 0.5
    - 任务 4 个：succeeded×2（30s/10s）、failed×1（5s）、queued×1 →
      success_rate 2/4 = 0.5、avg_duration_ms = 15000
    """
    _seed_parents(s)
    r1 = _add_tag(s, "a1", origin="closed", status="pending", tag_id="tag_1",
                  evidence_json="[]")
    r2 = _add_tag(s, "a1", status="pending", confidence=0.3, evidence_json=None)
    r3 = _add_tag(s, "a1", status="approved", confidence=0.7,
                  evidence_json='[{"scene_id": "s1"}]')
    r4 = _add_tag(s, "a1", origin="closed", status="approved", tag_id="tag_2",
                  confidence=0.9, evidence_json='["台词提及温暖"]')
    r5 = _add_tag(s, "a2", status="rejected", confidence=0.1,
                  evidence_json="[]")
    r6 = _add_tag(s, "a2", status="approved", tag_id="tag_1",
                  confidence=0.4, evidence_json='[{"scene_id": "s2"}]')
    assert r1.id and r6.id
    s.add(TagGenerationTask(task_id="t1", asset_id="a1", mode="closed",
                            status="succeeded", created_at=_T0_AWARE,
                            finished_at=datetime(2026, 11, 1, 12, 0, 30,
                                                 tzinfo=timezone.utc)))
    s.add(TagGenerationTask(task_id="t2", asset_id="a1", mode="open",
                            status="succeeded", created_at=_T0,
                            finished_at=datetime(2026, 11, 1, 12, 0, 10)))
    s.add(TagGenerationTask(task_id="t3", asset_id="a2", mode="closed",
                            status="failed", created_at=_T0,
                            finished_at=datetime(2026, 11, 1, 12, 0, 5)))
    s.add(TagGenerationTask(task_id="t4", asset_id="a2", mode="open",
                            status="queued", created_at=_T0))
    s.commit()


WORLD_SUMMARY = {
    "pending_count": 2,
    "review_stats": {
        "approved": {"count": 3, "ratio": 0.75},
        "rejected": {"count": 1, "ratio": 0.25},
        "merged": {"count": 0, "ratio": None},
        "modified": {"count": 0, "ratio": None},
    },
    "generated_pass_rate": 0.5,
    "evidence_coverage": 0.5,
    "task_stats": {
        "total": 4,
        "succeeded": 2,
        "failed": 1,
        "success_rate": 0.5,
        "avg_duration_ms": 15000.0,
    },
}


def _assert_summary_matches(actual, expected):
    """summary 逐字段比较：整型/None 精确相等，浮点经 pytest.approx。"""
    assert actual["pending_count"] == expected["pending_count"]
    for key in ("approved", "rejected", "merged", "modified"):
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
            "modified": {"count": 0, "ratio": None},
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
    """已知数据集精确数字（含 aware/naive 混存时长均值 15000ms）。"""
    _seed_metrics_world(session)
    _assert_summary_matches(compute_metrics(session), WORLD_SUMMARY)


def test_generated_pass_rate_none_without_terminal_new_tags(session):
    """库内标签行 approved 不进分母：新词只有 pending → None。"""
    _seed_parents(session)
    _add_tag(session, "a1", origin="closed", status="approved", tag_id="tag_1")
    _add_tag(session, "a1", status="pending")
    session.commit()
    assert compute_metrics(session)["generated_pass_rate"] is None


def test_generated_pass_rate_full_approval(session):
    """新词全部 approved → 通过率 1.0。"""
    _seed_parents(session)
    _add_tag(session, "a1", status="approved")
    _add_tag(session, "a2", status="approved")
    session.commit()
    assert compute_metrics(session)["generated_pass_rate"] == 1.0


def test_task_stats_without_terminal_tasks(session):
    """全部任务未终态：avg_duration_ms None、success_rate 0.0（total 非零
    不算分母缺失，队列中任务计入 total）。"""
    _seed_parents(session)
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
    """分桶边界钉死：左闭右开 [lo, hi)，1.0 clamp 末桶，None 不计入。"""
    _seed_parents(session)
    for conf in (0.05, 0.1, 0.3, 0.5, 0.7, 1.0, None):
        _add_tag(session, "a1", confidence=conf)
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
    _seed_parents(session)
    for conf in (0.19, 0.2, 0.65, 1.0):
        _add_tag(session, "a1", confidence=conf)
    session.commit()
    assert confidence_distribution(session, buckets=5) == [
        {"bucket": "0.0-0.2", "count": 1},  # 0.19
        {"bucket": "0.2-0.4", "count": 1},  # 0.2 边界归右
        {"bucket": "0.4-0.6", "count": 0},
        {"bucket": "0.6-0.8", "count": 1},  # 0.65
        {"bucket": "0.8-1.0", "count": 1},  # 1.0 末桶
    ]


# ---------- 核心层：tag_type_coverage ----------

COVERAGE_EXPECTED = [
    {"tag_type": "genre", "total": 3, "approved": 2},
    {"tag_type": "mood", "total": 1, "approved": 0},
]


def _seed_coverage_world(s):
    """tag_type 覆盖率已知数据集（6 行，跨三资产）。

    genre：tag_1 ×3（a1 approved、a2 pending、a3 approved）；
    mood：tag_2 rejected；另两行是库外新词（无类型，不计入）。
    """
    _seed_parents(s, assets=("a1", "a2", "a3"))
    _add_tag(s, "a1", origin="closed", status="approved", tag_id="tag_1")
    _add_tag(s, "a2", origin="closed", status="pending", tag_id="tag_1")
    _add_tag(s, "a3", status="approved", tag_id="tag_1")
    _add_tag(s, "a1", origin="closed", status="rejected", tag_id="tag_2")
    _add_tag(s, "a2", status="approved")
    _add_tag(s, "a1", status="pending")
    s.commit()


def test_tag_type_coverage_world(session):
    """按 tag_catalog.tag_type 聚合；total 降序、同数按 tag_type 升序。"""
    _seed_coverage_world(session)
    assert tag_type_coverage(session) == COVERAGE_EXPECTED


def test_tag_type_coverage_skips_untyped_rows(session):
    """库外新词无类型不计入；空库 → 空列表。"""
    assert tag_type_coverage(session) == []
    _seed_parents(session)
    _add_tag(session, "a1")
    session.commit()
    assert tag_type_coverage(session) == []


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
        _seed_parents(s)
        for conf in (0.05, 0.5, 0.95):
            _add_tag(s, "a1", confidence=conf)
        s.commit()
    resp = client.get("/api/v1/metrics/confidence-distribution")
    assert resp.status_code == 200
    body = resp.json()
    expected = {d["bucket"]: 0 for d in body}
    expected.update({"0.0-0.1": 1, "0.5-0.6": 1, "0.9-1.0": 1})
    assert {d["bucket"]: d["count"] for d in body} == expected
    assert sum(d["count"] for d in body) == 3  # 全桶返回、计数守恒


def test_confidence_distribution_endpoint_buckets_param(client):
    """buckets 查询参数透传核心层（buckets=5 → 宽度 0.2 的 5 桶）。"""
    factory = client.app.state.session_factory
    with factory() as s:
        _seed_parents(s)
        _add_tag(s, "a1", confidence=0.5)
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


def test_tag_type_coverage_endpoint(client):
    """GET /metrics/tag-type-coverage：已知数据集 → 200 精确 JSON。"""
    factory = client.app.state.session_factory
    with factory() as s:
        _seed_coverage_world(s)
    resp = client.get("/api/v1/metrics/tag-type-coverage")
    assert resp.status_code == 200
    assert resp.json() == COVERAGE_EXPECTED


def test_metrics_endpoints_require_login(client):
    """未登录 401：新 TestClient 不带 cookie。"""
    with TestClient(client.app) as fresh:
        for path in ("/api/v1/metrics/summary",
                     "/api/v1/metrics/confidence-distribution",
                     "/api/v1/metrics/tag-type-coverage"):
            assert fresh.get(path).status_code == 401
