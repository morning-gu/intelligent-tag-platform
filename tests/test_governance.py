"""标签治理流水线测试（v2 双表拆分重写）。

覆盖契约（brief 用例 + Review Focus 5 + 计划"导入落位规则"）：
- 层级导入拆分：一/二/三级 → tag_catalog 目录（一律 dir_static、
  applicable_scope 缺省 content，幂等复用同名目录）；「标签」列 →
  tag_define（tag_type 一律 val_fixed）挂最近目录；
- 扁平导入（parent_tag 为空）→ 自动创建根目录「未分类」（幂等复用，
  不重复建）；纯层级导入不产生「未分类」；批内层级根目录恰名「未分类」
  时扁平行复用该批内目录（不建第二个同名根目录）；
- 重复 (dir, name)：库内与批内两路都 skipped + duplicate_name；
- 别名冲突沿用：撞库内标签名 / 批内两行共享（两行均不插入）；
- 语义相似拦截退役：不同名同向量（余弦 1.0）不再被拦——疑似同义
  直接放行入库，由人工甄别；同名不同目录合法共存钉住放行语义；
- 父链：parent_not_found / 批内成环 parent_cycle / 兜底级联零静默搁浅；
- tag_change_log action='import'：after_json 带 task_id（revoke/export
  反查键），kind 区分 dir/tag 行；
- 复活：deprecated 同 (dir,name) 再导入恢复 active、保留原 tag_id；
- 批量编码：全批一次 embed 调用；GovernanceConfig 随 semantic_similar
  退役为空壳（v1 allowed_tag_types 白名单校验早已随 tag_type 枚举化退役）。

状态字面量：v2 统一 "deprecated"（v1 "deprecate" 退役）。
"""

import json
from dataclasses import fields
from datetime import datetime, timezone

import numpy as np

from conftest import MOCK_EMBEDDER
from tagplatform.db.models import TagCatalog, TagChangeLog, TagDefine, TagImportTask
from tagplatform.governance import TagRow, run_governance
from tagplatform.governance.pipeline import GovernanceConfig


class SameVecEmbedder:
    """确定性常向量替身：任意文本同向量（余弦 1.0）——语义拦截退役后
    用于钉死"不同名同向量也放行入库"的新契约。输出遵守 Embedder 契约
    （L2 归一化 float32），归一化后点积即余弦。
    """

    name, dim = "same-vec", 8

    def embed(self, texts, *, role):
        assert role == "passage"  # 入库语料侧向量：e5 passage 前缀契约
        mat = np.ones((len(texts), 8), np.float32)
        return mat / np.linalg.norm(mat, axis=1, keepdims=True)


def _row(name, parent=None, desc="d", aliases=None):
    return TagRow(None, name, "val_fixed", desc, parent, aliases or [], None, None)


# ---------- 落位拆分（Review Focus 5） ----------

def test_hierarchical_split_and_flat_fallback(session):
    # 层级混合落位：目录链 + 叶子标签 + 三级链缺级 + 扁平行，一次导入
    res = run_governance([
        _row("内容", None, ""),            # 一级 → 目录（根）
        _row("剧情", "内容", ""),          # 二级 → 目录（父=一级）
        _row("反转", "剧情", "情节反转"),   # 标签挂二级（三级缺级）
        _row("悬疑", None, "以悬念为核心"),  # 扁平行 → 「未分类」
    ], session, MOCK_EMBEDDER)
    assert res.issues == [] and len(res.inserted) == 2  # inserted 仅计标签行

    dirs = {d.dir_name: d for d in session.query(TagCatalog).all()}
    defines = {t.tag_name: t for t in session.query(TagDefine).all()}
    assert set(dirs) == {"内容", "剧情", "未分类"}
    assert set(defines) == {"反转", "悬疑"}
    assert dirs["内容"].parent_id is None
    assert dirs["剧情"].parent_id == dirs["内容"].dir_id
    assert all(d.dir_type == "dir_dynamic"
               and d.applicable_scope == "content,scene,character"
               for d in dirs.values())
    # (dir, name) 落位：反转挂剧情目录、悬疑挂「未分类」；一律 val_fixed
    assert defines["反转"].dir_id == dirs["剧情"].dir_id
    assert defines["悬疑"].dir_id == dirs["未分类"].dir_id
    assert all(t.tag_type == "val_fixed" and t.status == "active"
               for t in defines.values())

    # 整批重导：目录幂等复用（不重复建、无 issue），标签全部 skipped+issue
    res2 = run_governance([
        _row("内容", None, ""),
        _row("剧情", "内容", ""),
        _row("反转", "剧情", "情节反转"),
        _row("悬疑", None, "以悬念为核心"),
    ], session, MOCK_EMBEDDER)
    assert res2.inserted == []
    assert {i.code for i in res2.issues} == {"duplicate_name"}
    assert {i.tag_name for i in res2.issues} == {"反转", "悬疑"}
    assert session.query(TagCatalog).count() == 3
    assert session.query(TagDefine).count() == 2


def test_three_level_hierarchy_full_chain(session):
    # 一/二/三级全链：3 目录 + 1 标签挂三级；纯层级导入不产生「未分类」
    res = run_governance([
        _row("内容", None, ""),
        _row("剧情", "内容", ""),
        _row("悬疑", "剧情", ""),
        _row("反转", "悬疑", "情节反转"),
    ], session, MOCK_EMBEDDER)
    assert res.issues == [] and len(res.inserted) == 1
    dirs = {d.dir_name: d for d in session.query(TagCatalog).all()}
    assert set(dirs) == {"内容", "剧情", "悬疑"}
    defines = session.query(TagDefine).all()
    assert [t.tag_name for t in defines] == ["反转"]
    assert defines[0].dir_id == dirs["悬疑"].dir_id
    assert dirs["悬疑"].parent_id == dirs["剧情"].dir_id
    assert dirs["剧情"].parent_id == dirs["内容"].dir_id


def test_flat_import_falls_back_to_uncategorized_idempotent(session):
    # 扁平导入 → 自动创建根目录「未分类」；再次导入幂等复用不重复建
    rows = [_row("悬疑", None, "以悬念为核心")]
    r1 = run_governance(rows, session, MOCK_EMBEDDER)
    assert r1.issues == [] and len(r1.inserted) == 1
    dirs = session.query(TagCatalog).all()
    assert len(dirs) == 1
    root = dirs[0]
    assert root.dir_name == "未分类" and root.parent_id is None
    assert root.dir_type == "dir_dynamic"
    assert root.embedding is not None  # 目录入库即计算 embedding
    assert session.get(TagDefine, r1.inserted[0]).dir_id == root.dir_id

    r2 = run_governance(rows, session, MOCK_EMBEDDER)
    assert r2.inserted == [] and r2.issues[0].code == "duplicate_name"
    assert session.query(TagCatalog).count() == 1  # 未分类复用，未重复建


def test_batch_root_dir_named_uncategorized_reused_for_flat_rows(session):
    # 批内层级根目录恰名「未分类」+ 扁平行：扁平行复用批内同名根目录，
    # 不自动新建第二个同名根目录（review 复现缺陷——uncat 解析此前只查库）
    res = run_governance([
        _row("未分类", None, ""),           # 层级根目录（被下行引用 → 目录行）
        _row("反转", "未分类", "情节反转"),  # 标签挂批内「未分类」
        _row("悬疑", None, "以悬念为核心"),  # 扁平行 → 落同一个「未分类」
    ], session, MOCK_EMBEDDER)
    assert res.issues == [] and len(res.inserted) == 2
    dirs = session.query(TagCatalog).all()
    assert len(dirs) == 1  # 仍只有一个根目录，无第二个自动「未分类」
    assert dirs[0].dir_name == "未分类" and dirs[0].parent_id is None
    # 层级子行 + 扁平行都挂进该目录
    defines = {t.tag_name: t for t in session.query(TagDefine).all()}
    assert set(defines) == {"反转", "悬疑"}
    assert {t.dir_id for t in defines.values()} == {dirs[0].dir_id}


def test_batch_duplicate_same_dir_name_skipped(session):
    # 批内两行同名同目录（parent 同空 → 同落「未分类」）→ 后行 duplicate_name
    res = run_governance([
        _row("悬疑", None, "以悬念为核心"),
        _row("悬疑", None, "另一段描述"),
    ], session, MOCK_EMBEDDER)
    assert len(res.inserted) == 1
    dups = [i for i in res.issues if i.code == "duplicate_name"]
    assert len(dups) == 1 and dups[0].tag_name == "悬疑"


# ---------- 别名 / 语义 ----------

def test_alias_cross_conflict(session):
    # 库内：别名撞已有标签名
    run_governance([_row("推理", desc="以解谜推进")], session, MOCK_EMBEDDER)
    res = run_governance([_row("悬疑", aliases=["推理"])], session, MOCK_EMBEDDER)
    assert res.issues[0].code == "alias_conflict" and res.inserted == []
    # 批内：两行共享同一别名 → 两行均不插入
    res2 = run_governance([
        _row("甲", desc="d1", aliases=["共享词"]),
        _row("乙", desc="d2", aliases=["共享词"])], session, MOCK_EMBEDDER)
    assert res2.inserted == []
    assert all(i.code == "alias_conflict" for i in res2.issues)


def test_semantic_similar_retired_allows_insertion(session):
    """语义拦截退役：不同名同向量（余弦 1.0）不再报 semantic_similar，
    直接放行入库——疑似同义标签由人工经待处理列表甄别。"""
    run_governance([_row("推理", desc="以解谜推进")], session, SameVecEmbedder())
    res = run_governance([_row("侦探", desc="另一段描述")], session, SameVecEmbedder())
    # 不再拦截：无 issue、两枚标签并存
    assert res.issues == [] and len(res.inserted) == 1
    defines = {t.tag_name for t in session.query(TagDefine).all()}
    assert defines == {"推理", "侦探"}
    # result_summary.issues 为空明细列表（导入任务详情端点消费契约）
    task = session.get(TagImportTask, res.task_id)
    detail = json.loads(task.result_summary)
    assert detail["issues"] == [] and detail["inserted"] == 1


def test_same_name_different_dirs_coexists(session):
    # 同名不同目录合法共存：即使描述相同（MockEmbedder 同形文本同向量）也放行
    run_governance([
        _row("内容", None, ""),
        _row("悬疑", "内容", "以悬念为核心"),
    ], session, MOCK_EMBEDDER)
    res = run_governance([
        _row("风格", None, ""),
        _row("悬疑", "风格", "以悬念为核心"),
    ], session, MOCK_EMBEDDER)
    assert res.issues == [] and len(res.inserted) == 1
    defines = session.query(TagDefine).all()
    assert len(defines) == 2
    assert len({t.dir_id for t in defines}) == 2  # 分属 内容 / 风格 两个目录


# ---------- 父链 ----------

def test_parent_missing_and_cycle(session):
    res = run_governance([_row("子", parent="不存在的父")], session, MOCK_EMBEDDER)
    assert res.issues[0].code == "parent_not_found" and res.inserted == []
    # 批内互为父子：两行（均为目录行）整链报 parent_cycle
    res2 = run_governance([
        _row("甲", parent="乙", desc="d1"),
        _row("乙", parent="甲", desc="d2")], session, MOCK_EMBEDDER)
    assert {i.code for i in res2.issues} == {"parent_cycle"}
    assert res2.inserted == []
    assert session.query(TagCatalog).count() == 0


def test_batch_parent_chain_inserts(session):
    # 批内父子链：父行成目录，子行挂父目录（v1 拓扑序语义沿用）
    run_governance([
        _row("剧情", None, "叙事驱动"),
        _row("悬疑", "剧情", "以悬念为核心")], session, MOCK_EMBEDDER)
    dirs = {d.dir_name: d for d in session.query(TagCatalog).all()}
    child_tag = session.query(TagDefine).filter_by(tag_name="悬疑").one()
    assert child_tag.dir_id == dirs["剧情"].dir_id


def test_valve_cascade_no_silent_strand(session):
    # 兜底级联（v2 形态）：丙是被乙引用的目录行，别名撞批内标签名 甲 →
    # 丙被拒 → 乙父链断 → 甲挂靠失败——三级逐一报 issue，零静默搁浅
    res = run_governance([
        _row("甲", parent="乙", desc="d1"),
        _row("乙", parent="丙", desc="d2"),
        _row("丙", None, "d3", aliases=["甲"]),
    ], session, MOCK_EMBEDDER)
    assert res.inserted == []
    by_name = {i.tag_name: i.code for i in res.issues}
    assert by_name["丙"] == "alias_conflict"
    assert by_name["乙"] == "parent_not_found" and by_name["甲"] == "parent_not_found"
    assert session.query(TagCatalog).count() == 0
    assert session.query(TagDefine).count() == 0
    # 记账不变量：每行要么入库要么有 issue（零静默丢弃）
    assert set(by_name) == {"甲", "乙", "丙"}


# ---------- 复活与状态字面量 ----------

def test_deprecated_same_name_is_reactivated_on_import(session):
    """停用标签按相同目录再次导入时恢复为启用（status 字面量 v2 deprecated）。"""
    now = datetime.now(timezone.utc)
    session.add(TagCatalog(
        dir_id="dir_dep0001", dir_name="未分类", dir_type="dir_static",
        description="", parent_id=None, aliases=None,
        applicable_scope="content", embedding=None, created_at=now, updated_at=now))
    session.add(TagDefine(
        tag_id="tag_dep0001", tag_name="悬疑", tag_type="val_fixed",
        description="已废弃的同名标签", dir_id="dir_dep0001", aliases=None,
        status="deprecated", embedding=None, created_at=now, updated_at=now))
    session.commit()
    res = run_governance(
        [_row("悬疑", desc="以悬念为核心")], session, MOCK_EMBEDDER)
    assert res.inserted == ["tag_dep0001"] and res.issues == []
    assert session.get(TagImportTask, res.task_id) is not None
    assert session.query(TagDefine).count() == 1
    row = session.get(TagDefine, "tag_dep0001")
    assert row.status == "active" and row.dir_id == "dir_dep0001"


def test_status_literal_normalized_to_deprecated(session):
    # v1 "deprecate" 行内取值归一为 v2 "deprecated"（CHECK 约束字面量）
    res = run_governance(
        [TagRow(None, "旧词", "val_fixed", "d", None, [], None, "deprecate")],
        session, MOCK_EMBEDDER)
    assert res.issues == []
    assert session.get(TagDefine, res.inserted[0]).status == "deprecated"


# ---------- 审计日志 ----------

def test_change_log_import_rows_carry_task_id(session):
    # 审计直测：每个新建目录/标签行各落一条 tag_change_log(action='import')，
    # after_json 带 task_id（revoke/export 的反查键，替代 v1 batch_id）与
    # kind（'dir'|'tag'，撤销据此区分删除对象）；日志 tag_id 分别指向
    # tag_catalog.dir_id 与 tag_define.tag_id
    res = run_governance([
        _row("内容", None, ""),
        _row("剧情", "内容", ""),
        _row("反转", "剧情", "情节反转"),
    ], session, MOCK_EMBEDDER)
    logs = session.query(TagChangeLog).filter_by(action="import").all()
    by_id = {log.tag_id: log for log in logs}
    dirs = {d.dir_name: d for d in session.query(TagCatalog).all()}
    tags = {t.tag_name: t for t in session.query(TagDefine).all()}
    assert set(by_id) == {
        dirs["内容"].dir_id, dirs["剧情"].dir_id, tags["反转"].tag_id}
    for log in logs:
        after = json.loads(log.after_json)
        assert after["task_id"] == res.task_id
        assert after["kind"] in ("dir", "tag")
    assert json.loads(by_id[tags["反转"].tag_id].after_json)["kind"] == "tag"
    assert json.loads(by_id[dirs["内容"].dir_id].after_json)["kind"] == "dir"
    assert json.loads(by_id[dirs["剧情"].dir_id].after_json)["kind"] == "dir"


# ---------- 配置与批量编码 ----------

def test_governance_config_threshold_only(session):
    # GovernanceConfig 随 semantic_similar 退役为空壳：无字段，显式传参
    # 不再影响判定（不同名同向量余弦 1.0 依旧放行）
    assert not [f.name for f in fields(GovernanceConfig)]
    run_governance([_row("推理", desc="以解谜推进")], session, SameVecEmbedder())
    cfg = GovernanceConfig()
    res = run_governance([_row("侦探", desc="另一段描述")], session,
                         SameVecEmbedder(), config=cfg)
    assert res.issues == [] and len(res.inserted) == 1


def test_allowed_tag_types_check_retired(session):
    # tag_type 白名单校验退役：行 tag_type 任意值归一——非法回退 val_fixed，
    # val_fixed/val_generalized 之外不入库
    res = run_governance(
        [TagRow(None, "悬疑", "不存在的类型", "d", None, [], None, None)],
        session, MOCK_EMBEDDER)
    assert res.issues == [] and len(res.inserted) == 1
    assert session.get(TagDefine, res.inserted[0]).tag_type == "val_fixed"

    res2 = run_governance(
        [TagRow(None, "泛化悬疑", "val_generalized", "d", None, [], None, None)],
        session, MOCK_EMBEDDER)
    assert res2.issues == [] and len(res2.inserted) == 1
    assert (session.get(TagDefine, res2.inserted[0]).tag_type
            == "val_generalized")


def test_governance_batch_embedding_calls(session):
    calls = []

    class CountingEmbedder:
        name, dim = "counting", 8

        def embed(self, texts, *, role):
            calls.append((role, len(texts)))
            mat = np.ones((len(texts), 8), np.float32)
            return mat / np.linalg.norm(mat, axis=1, keepdims=True)

    # 5 行扁平行 → 全批一次 embed（含「未分类」目录文本尾项，仍是单次调用）
    run_governance([_row(f"标签{i}", desc=f"描述{i}") for i in range(5)],
                   session, CountingEmbedder())
    assert calls == [("passage", 6)]
