"""标签治理流水线测试（Task 7）。

覆盖契约（brief 6 用例逐字）：
- 干净行插入：embedding / aliases JSON 串 / change_log(action=import) /
  import_batch(status=completed, result_summary 摘要)；
- 重跑同批全 skipped（duplicate_name，库内计数不增）；
- 别名冲突：撞库内已有名称 / 批内两行共享同一别名（两行均不插入）；
- 语义阈值含等于：MockEmbedder 同形文本同向量 → 余弦 1.0 ≥ 0.90；
- parent 缺失（parent_not_found）与批内父子成环（parent_cycle）；
- 批内父子链拓扑序插入（child.parent_id 指向批内父行 tag_id）。
"""

import json

from conftest import MOCK_EMBEDDER
from tagplatform.db.models import ImportBatch, TagCatalog, TagChangeLog
from tagplatform.governance import TagRow, run_governance


def test_clean_rows_inserted_with_embedding_and_log(session):
    res = run_governance([TagRow(None, "悬疑", "genre", "以悬念与谜题为核心", None, ["悬念"], None, None)], session, MOCK_EMBEDDER)
    assert res.inserted and not res.issues
    t = session.get(TagCatalog, res.inserted[0])
    assert t.aliases == '["悬念"]' and t.embedding is not None and t.parent_id is None
    log = session.query(TagChangeLog).filter_by(tag_id=t.tag_id).one()
    assert log.action == "import"
    batch = session.get(ImportBatch, res.batch_id)
    assert batch.status == "completed" and json.loads(batch.result_summary)["inserted"] == 1
    assert json.loads(batch.result_summary)["issues"] == []  # issues 为明细列表：无问题时空列表

def test_rerun_same_excel_all_skipped(session):
    rows = [TagRow(None, "悬疑", "genre", "以悬念为核心", None, [], None, None)]
    r1 = run_governance(rows, session, MOCK_EMBEDDER)
    r2 = run_governance(rows, session, MOCK_EMBEDDER)
    assert r2.inserted == [] and r2.issues[0].code == "duplicate_name"
    assert session.query(TagCatalog).count() == 1

def test_alias_cross_conflict(session):
    # 库内：别名撞已有名称
    run_governance([TagRow(None, "推理", "genre", "以解谜推进", None, [], None, None)], session, MOCK_EMBEDDER)
    res = run_governance([TagRow(None, "悬疑", "genre", "d", None, ["推理"], None, None)], session, MOCK_EMBEDDER)
    assert res.issues[0].code == "alias_conflict" and res.inserted == []
    # 批内：两行共享同一别名 → 后行（或两行均）报 alias_conflict，不插入
    res2 = run_governance([
        TagRow(None, "甲", "genre", "d1", None, ["共享词"], None, None),
        TagRow(None, "乙", "genre", "d2", None, ["共享词"], None, None)], session, MOCK_EMBEDDER)
    assert res2.inserted == [] and any(i.code == "alias_conflict" for i in res2.issues)

def test_semantic_threshold_inclusive(session):
    # MockEmbedder 同形文本同向量 → "推理" vs "推理" 余弦 1.0 ≥ 0.90
    run_governance([TagRow(None, "推理", "genre", "以解谜推进", None, [], None, None)], session, MOCK_EMBEDDER)
    res = run_governance([TagRow(None, "推理", "style", "以解谜推进", None, [], None, None)], session, MOCK_EMBEDDER)
    assert res.issues[0].code == "semantic_similar" and res.issues[0].similar_to == "推理"
    # result_summary.issues 为明细列表（非计数）：similar_to 进入序列化结果
    # （Task 10 批次查询端点 / 阶段 3 待处理列表的消费契约）
    summary = json.loads(session.get(ImportBatch, res.batch_id).result_summary)
    assert isinstance(summary["issues"], list) and len(summary["issues"]) == 1
    issue_detail = summary["issues"][0]
    assert issue_detail["code"] == "semantic_similar" and issue_detail["tag_name"] == "推理"
    assert issue_detail["similar_to"] == "推理"

def test_parent_missing_and_cycle(session):
    res = run_governance([TagRow(None, "子", "genre", "d", "不存在的父", None, None, None)], session, MOCK_EMBEDDER)
    assert res.issues[0].code == "parent_not_found"
    res2 = run_governance([
        TagRow(None, "甲", "genre", "d1", "乙", None, None, None),
        TagRow(None, "乙", "genre", "d2", "甲", None, None, None)], session, MOCK_EMBEDDER)
    assert {i.code for i in res2.issues} == {"parent_cycle"}

def test_batch_parent_chain_inserts(session):
    res = run_governance([
        TagRow(None, "剧情", "genre", "叙事驱动", None, None, None, None),
        TagRow(None, "悬疑", "genre", "以悬念为核心", "剧情", None, None, None)], session, MOCK_EMBEDDER)
    child = next(t for t in session.query(TagCatalog).all() if t.tag_name == "悬疑")
    parent = next(t for t in session.query(TagCatalog).all() if t.tag_name == "剧情")
    assert child.parent_id == parent.tag_id
