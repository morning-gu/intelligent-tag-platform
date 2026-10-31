"""质量指标测试（Task 7）：核心推导函数与三个 metrics 路由。

核心层（session fixture，内存库，纯读函数直接断言）：
- 空库语义：计数为 0、所有占比类字段为 None（分母 0 统一"暂无"）、
  distribution 恒返回全部桶（0 计数桶保留）、coverage 空列表；
- 已知小型数据集精确数字断言（pending / 审核动作计数与占比 / 新标签
  通过率 / evidence 覆盖率 / 任务统计，含时长均值）；
- 分桶边界钉死：左闭右开 [lo, hi)——边界值归右侧桶（0.5 → "0.5-0.6"）、
  1.0 clamp 到末桶；0.3/0.7 为浮点乘法陷阱值（int(0.3*10)==2）必须正确
  归桶；None confidence 不计入；buckets 可调（宽度 0.2 的 5 桶）；
- tag_type 行语义值：带 tag_id 行（catalog/merged）按目录类型、generated
  行按 candidate_type、无法归类行不计入；按 total 降序（同数按 tag_type
  升序决胜）。

路由层（client fixture，真 app + 临时文件库，与核心层共用同一批已知
数据集播种函数）：三端点 200 精确 JSON、buckets 查询参数透传与校验、
未登录 401（create_app protected_deps 统一挂载验证）。
"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from tagplatform.db.models import (
    AssetTag,
    GenerationTask,
    MediaAsset,
    ReviewRecord,
    TagCatalog,
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


def _add_tag(s, asset_id, *, origin="generated", status="pending", tag_id=None,
             candidate_type="narrative", confidence=0.5, evidence_json="[]"):
    """单条 asset_tag 行（generated 且无 tag_id 带候选字段，其余候选清空）。"""
    is_generated = origin == "generated"
    has_candidate = is_generated and tag_id is None
    row = AssetTag(
        asset_id=asset_id,
        tag_id=tag_id,
        candidate_name=(f"候选{next(_candidate_seq)}" if has_candidate else None),
        candidate_type=candidate_type if has_candidate else None,
        candidate_description="候选描述" if has_candidate else None,
        origin=origin,
        relevance_score=None if is_generated else 0.9,
        confidence=confidence,
        evidence_json=evidence_json,
        review_status=status,
        created_at=_NOW,
        updated_at=_NOW,
    )
    s.add(row)
    s.flush()  # 取自增 id 供 review_record 外键
    return row


def _add_record(s, result_id, action):
    """单条审核记录（record_id 由 result_id+action 保证本数据集内唯一）。"""
    s.add(ReviewRecord(
        record_id=f"rev_{result_id}_{action}",
        result_id=result_id,
        action=action,
        reviewer_id="admin",
        before_json="{}",
        after_json="{}",
        created_at=_NOW,
    ))


def _seed_metrics_world(s):
    """已知小型数据集（6 行 asset_tag + 5 条审核记录 + 4 个生成任务）。

    期望值（WORLD_SUMMARY，session 级与 API 级测试共用）：
    - pending_count = 2（行1 catalog pending、行2 generated pending）
    - 审核记录 5 条：approve×2、reject、merge、modify →
      approved 2/5=0.4、rejected 1/5=0.2、merged 1/5=0.2、modified 1/5=0.2
      （modify 记录落在仍 pending 的行上——计数按记录而非行当前状态）
    - generated 4 行（行2 pending、行3 approved、行5 rejected、行6 merged），
      已终审 3 行、其中 approved 1 行 → 通过率 1/3（merged 计入分母）
    - evidence 非空非 [] 的行：行3、行4、行6 → 3/6 = 0.5
    - 任务 4 个：succeeded×2（30s/10s）、failed×1（5s）、queued×1（无时长）→
      success_rate 2/4 = 0.5、avg_duration_ms = (30000+10000+5000)/3 = 15000
    """
    _seed_parents(s)
    r1 = _add_tag(s, "a1", origin="catalog", status="pending", tag_id="tag_1",
                  evidence_json="[]")
    r2 = _add_tag(s, "a1", status="pending", candidate_type="narrative",
                  confidence=0.3, evidence_json=None)
    r3 = _add_tag(s, "a1", status="approved", candidate_type="mood",
                  confidence=0.7, evidence_json='[{"scene_id": "s1"}]')
    r4 = _add_tag(s, "a1", origin="catalog", status="approved", tag_id="tag_2",
                  confidence=0.9, evidence_json='["台词提及温暖"]')
    r5 = _add_tag(s, "a2", status="rejected", candidate_type="style",
                  confidence=0.1, evidence_json="[]")
    r6 = _add_tag(s, "a2", origin="generated", status="merged", tag_id="tag_1",
                  candidate_type=None, confidence=0.4,
                  evidence_json='[{"scene_id": "s2"}]')
    for row, action in ((r3, "approve"), (r4, "approve"), (r5, "reject"),
                        (r6, "merge"), (r1, "modify")):
        _add_record(s, row.id, action)
    s.add(GenerationTask(task_id="t1", mode="closed", status="succeeded",
                         created_at=_T0_AWARE,
                         finished_at=datetime(2026, 11, 1, 12, 0, 30,
                                              tzinfo=timezone.utc)))
    s.add(GenerationTask(task_id="t2", mode="open", status="succeeded",
                         created_at=_T0,
                         finished_at=datetime(2026, 11, 1, 12, 0, 10)))
    s.add(GenerationTask(task_id="t3", mode="closed", status="failed",
                         created_at=_T0,
                         finished_at=datetime(2026, 11, 1, 12, 0, 5)))
    s.add(GenerationTask(task_id="t4", mode="open", status="queued",
                         created_at=_T0))
    s.commit()


# _seed_metrics_world 的期望 summary（session 级与 API 级测试共用；
# 浮点经 pytest.approx 比较，None 原样相等）
WORLD_SUMMARY = {
    "pending_count": 2,
    "review_stats": {
        "approved": {"count": 2, "ratio": 0.4},
        "rejected": {"count": 1, "ratio": 0.2},
        "merged": {"count": 1, "ratio": 0.2},
        "modified": {"count": 1, "ratio": 0.2},
    },
    "generated_pass_rate": 1 / 3,
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
    """summary 逐字段比较：整型/None 精确相等，浮点经 pytest.approx
    （pytest.approx 不支持嵌套 dict，故手工展开）。"""
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
    """已知数据集精确数字：各字段与手算期望逐一相等（含 aware/naive 混存
    时长均值 15000ms）。"""
    _seed_metrics_world(session)
    _assert_summary_matches(compute_metrics(session), WORLD_SUMMARY)


def test_generated_pass_rate_none_without_terminal_generated(session):
    """generated 无已终审行 → None（即便 catalog 行已 approved——分母只看
    generated 行）。"""
    _seed_parents(session)
    _add_tag(session, "a1", origin="catalog", status="approved", tag_id="tag_1")
    _add_tag(session, "a1", status="pending")
    session.commit()
    assert compute_metrics(session)["generated_pass_rate"] is None


def test_generated_pass_rate_full_approval(session):
    """generated 全部 approved → 通过率 1.0。"""
    _seed_parents(session)
    _add_tag(session, "a1", status="approved")
    _add_tag(session, "a1", status="approved", candidate_type="mood")
    session.commit()
    assert compute_metrics(session)["generated_pass_rate"] == 1.0


def test_task_stats_without_terminal_tasks(session):
    """全部任务未终态：avg_duration_ms None、success_rate 0.0（total 非零
    不算分母缺失，队列中任务计入 total）。"""
    _seed_parents(session)
    session.add(GenerationTask(task_id="t1", mode="closed", status="queued",
                               created_at=_T0))
    session.add(GenerationTask(task_id="t2", mode="open", status="running",
                               created_at=_T0))
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


# 已知覆盖数据集的期望（_seed_coverage_world，session 级与 API 级共用）：
# merged generated 行随其并入的目录类型（tag_1 → genre）
COVERAGE_EXPECTED = [
    {"tag_type": "genre", "total": 3, "approved": 1},
    {"tag_type": "narrative", "total": 2, "approved": 1},
    {"tag_type": "mood", "total": 1, "approved": 0},
    {"tag_type": "style", "total": 1, "approved": 0},
]


def _seed_coverage_world(s):
    """tag_type 覆盖率已知数据集（7 行，跨两资产）。

    genre：catalog tag_1 ×2（1 approved）+ generated merged 并入 tag_1；
    narrative：generated 候选 ×2（1 approved）；mood：catalog tag_2 rejected；
    style：generated 候选 pending。目录行与 merged 行分挂三资产（同资产同
    tag_id 受 ux_asset_tag_catalog 部分唯一索引约束）。
    """
    _seed_parents(s, assets=("a1", "a2", "a3"))
    _add_tag(s, "a1", origin="catalog", status="approved", tag_id="tag_1")
    _add_tag(s, "a2", origin="catalog", status="pending", tag_id="tag_1")
    _add_tag(s, "a1", origin="catalog", status="rejected", tag_id="tag_2")
    _add_tag(s, "a1", status="approved", candidate_type="narrative")
    _add_tag(s, "a1", status="pending", candidate_type="narrative")
    _add_tag(s, "a1", status="pending", candidate_type="style")
    _add_tag(s, "a3", origin="generated", status="merged", tag_id="tag_1",
             candidate_type=None)
    s.commit()


def test_tag_type_coverage_world(session):
    """行语义类型：带 tag_id 行（catalog/merged）按目录类型、generated 行
    按 candidate_type；按 total 降序（同数按 tag_type 升序决胜）。"""
    _seed_coverage_world(session)
    assert tag_type_coverage(session) == COVERAGE_EXPECTED


def test_tag_type_coverage_skips_untyped_rows(session):
    """generated 行 candidate_type 为空 → 无法归类不计入；空库 → 空列表。"""
    assert tag_type_coverage(session) == []
    _seed_parents(session)
    _add_tag(session, "a1", candidate_type=None)
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
    # 期望 = 全 10 桶计数（0 计数桶保留），三桶各 1
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
    """未登录 401：新 TestClient 不带 cookie（protected_deps 统一挂载覆盖
    metrics router）。"""
    with TestClient(client.app) as fresh:
        for path in ("/api/v1/metrics/summary",
                     "/api/v1/metrics/confidence-distribution",
                     "/api/v1/metrics/tag-type-coverage"):
            assert fresh.get(path).status_code == 401
