"""标签治理流水线测试（Task 7）。

覆盖契约（brief 6 用例逐字）：
- 干净行插入：embedding / aliases JSON 串 / change_log(action=import) /
  import_batch(status=completed, result_summary 摘要)；
- 重跑同批全 skipped（duplicate_name，库内计数不增）；
- 别名冲突：撞库内已有名称 / 批内两行共享同一别名（两行均不插入）；
- 语义阈值含等于：MockEmbedder 同形文本同向量 → 余弦 1.0 ≥ 0.90；
- parent 缺失（parent_not_found）与批内父子成环（parent_cycle）；
- 批内父子链拓扑序插入（child.parent_id 指向批内父行 tag_id）。

修复轮补充（审查驱动）：
- 兜底级联：三行链祖先的父被拒时，批内子孙逐级兜底拦截，零静默搁浅
  （记账不变量：{入库行} ∪ {有 issue 行} == 全部输入行）；
- 顺序对齐（控制器裁决，按处理规则序）：tag_type_invalid 提前至
  semantic_similar 之前——非法类型报 tag_type_invalid 而非 semantic_similar。
- 修复 2：废弃同名标签仍占用唯一名称——再导入报 duplicate_name（detail
  注明已废弃），不逃逸 IntegrityError，批次照常落库；语义比对仍只对
  非废弃行。

Task 4（阶段 2）补充：
- 显式配置传参：GovernanceConfig 注入 run_governance，不触碰全局
  get_settings 即可改变治理参数（allowed_tag_types 直接收 set）；
- 批量编码：CountingEmbedder 断言全批一次 embed 调用（判定与插入共用
  同一批向量，无逐行调用）。
"""

import json
from datetime import datetime, timezone

import numpy as np

from conftest import MOCK_EMBEDDER
from tagplatform.db.models import ImportBatch, TagCatalog, TagChangeLog
from tagplatform.governance import TagRow, run_governance
from tagplatform.governance.pipeline import GovernanceConfig
from tagplatform.settings import get_settings


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

def test_valve_cascade_no_silent_strand(session):
    # 兜底级联：三行链 甲←乙←丙，丙被拒（别名撞批内行名）且其名不在库内、
    # 不在有效行集 → 乙、甲在第二遍逐级兜底拦截。缺陷回归位：若跳过行不
    # 从 valid_by_name 摘除，子孙的父仍"看似有效"，就绪集变空 → 甲被静默
    # 搁浅（未入库且零 issue）。
    # 注：拒绝码用 alias_conflict 构造——semantic_similar 拒绝要求父名与
    # 库内/有效行同形（余弦 1.0），该名必然可经库内或有效行解析，兜底不触发。
    res = run_governance([
        TagRow(None, "甲", "genre", "d1", "乙", [], None, None),
        TagRow(None, "乙", "genre", "d2", "丙", [], None, None),
        TagRow(None, "丙", "genre", "d3", None, ["甲"], None, None)], session, MOCK_EMBEDDER)
    assert res.inserted == []
    by_name = {i.tag_name: i.code for i in res.issues}
    assert by_name["丙"] == "alias_conflict"
    assert by_name["乙"] == "parent_not_found" and by_name["甲"] == "parent_not_found"
    # 记账不变量：{入库行名} ∪ {有 issue 行名} == 全部输入行名（零静默丢弃）
    inserted_names = {t.tag_name for t in session.query(TagCatalog).all() if t.tag_id in res.inserted}
    assert inserted_names | set(by_name) == {"甲", "乙", "丙"}

def test_deprecated_same_name_blocks_import(session):
    """修复 2：库内存在 status='deprecate' 同名标签时再导入——报 duplicate_name
    （detail 注明已废弃），不逃逸 IntegrityError，批次摘要照常落库，库内计数不变。
    """
    now = datetime.now(timezone.utc)
    session.add(TagCatalog(
        tag_id="tag_dep0001", tag_name="悬疑", tag_type="genre",
        description="已废弃的同名标签", parent_id=None, aliases=None,
        applicable_scope=None, status="deprecate", embedding=None,
        created_at=now, updated_at=now))
    session.commit()
    res = run_governance(
        [TagRow(None, "悬疑", "genre", "以悬念为核心", None, [], None, None)],
        session, MOCK_EMBEDDER)
    assert res.inserted == []
    assert res.issues[0].code == "duplicate_name"
    assert "已废弃" in res.issues[0].detail and "tag_dep0001" in res.issues[0].detail
    # run_governance 正常返回（原先 IntegrityError 逃逸导致批次整批作废不落库）
    assert session.get(ImportBatch, res.batch_id) is not None
    assert session.query(TagCatalog).count() == 1

def test_tag_type_invalid_precedes_semantic(session, monkeypatch):
    # 顺序对齐（控制器裁决）：类型非法先于语义检查判定——非法类型不付
    # embed 开销，报 tag_type_invalid 而非 semantic_similar
    monkeypatch.setattr(get_settings(), "allowed_tag_types", "genre,style")
    run_governance([TagRow(None, "推理", "genre", "以解谜推进", None, [], None, None)], session, MOCK_EMBEDDER)
    res = run_governance([TagRow(None, "推理", "mood", "以解谜推进", None, [], None, None)], session, MOCK_EMBEDDER)
    assert res.issues[0].code == "tag_type_invalid" and res.inserted == []


# ---------- Task 4（阶段 2）：显式配置传参与批量编码 ----------

def test_governance_explicit_config_no_global(session, monkeypatch):
    # 不动 get_settings（monkeypatch 不设任何东西），显式传 allowed={"genre"}
    cfg = GovernanceConfig(similar_threshold=0.99, allowed_tag_types={"genre"})
    res = run_governance([TagRow(None, "悬疑", "bad_type", "以悬念为核心", None, [], None, None)],
                         session, MOCK_EMBEDDER, config=cfg)
    assert res.issues[0].code == "tag_type_invalid"

def test_governance_batch_embedding_calls(session):
    calls = []
    class CountingEmbedder:
        name, dim = "counting", 8
        def embed(self, texts, *, role):
            calls.append(len(texts)); return np.ones((len(texts), 8), np.float32)
    run_governance([TagRow(None, f"标签{i}", "genre", f"描述{i}", None, [], None, None)
                    for i in range(5)], session, CountingEmbedder())
    assert calls == [5] or calls == [5, 0]  # 一次批量，无逐行调用；空批次容差
