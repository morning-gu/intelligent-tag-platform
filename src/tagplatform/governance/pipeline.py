"""标签治理流水线：TagRow 行集 → 逐行校验 → 拓扑序入库 + 批次/审计落库。

两遍法（brief 处理规则）：
- 第一遍逐行按契约顺序判定 issue、判定即停（一行只报首个命中的 code），
  通过全部检查的行进入"有效行集"；
- 第二遍对有效行集按批内父子拓扑序统一插入（先父后子，逐行 flush——
  TagCatalog 自引用外键无 relationship()，同批 flush 会按类名字母序
  先插子行而炸外键），最后写 import_batch 摘要并提交。

判定顺序（契约行 `按上述顺序判定，判定即停`）：
    duplicate_name → alias_conflict → semantic_similar →
    parent_not_found → parent_cycle → tag_type_invalid

契约细则的实现解读（详见 task-7-report）：
- duplicate_name 对"同名不同 tag_type"的行不立即判停：test_semantic_
  threshold_inclusive 契约要求该情形让位给 semantic_similar（同名 →
  同名同描述文本 → 余弦 1.0 ≥ 阈值）；若语义未命中，则回填 duplicate_name
  ——tag_name 列有唯一约束，放行必然 IntegrityError，故同名必拦截。
- alias_conflict 为批内全局预扫描（"批内两行共享同一 alias"）：共享别名的
  两行均判 alias_conflict、均不插入（test_alias_cross_conflict 断言
  res2.inserted == []）。
- parent_not_found 的"批内有效行"按名取自整批行集（含尚未判定的行）：
  test_parent_missing_and_cycle 中甲行判定时乙行尚未过检，若只看已过检
  行会误报 parent_not_found。父名只命中批内未通过校验行的情况在第二遍
  插入时兜底拦截（报 parent_not_found、不插入），避免悬空外键。
- parent_cycle 只查批内新增边：父链走出批内（命中库内名）即止，库内
  已有链由库保证无环。
- 时间戳列无默认值，插入时显式赋 UTC now（Task 3 审查遗留注意项）。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
from sqlalchemy.orm import Session

from tagplatform.db.models import ImportBatch, TagCatalog, TagChangeLog
from tagplatform.governance.excel import TagRow
from tagplatform.settings import get_settings
from tagplatform.vectors import decode_vector, encode_vector, search_similar

# run_governance 无文件名入参，import_batch.file_name 记录写入来源；
# API 层（Task 10）如需真实文件名可在拿到 batch_id 后回填
_BATCH_FILE_NAME = "governance"

# 审计日志操作者标识：本流水线产生的 import 动作统一记 governance
_OPERATOR = "governance"


@dataclass
class Issue:
    """单行治理问题；similar_to 仅 semantic_similar 记对端标签名。"""

    tag_name: str
    code: str
    detail: str
    similar_to: str | None = None


@dataclass
class GovernanceResult:
    """治理结果：本批 batch_id、入库 tag_id 列表、问题列表（与插入行不相交）。"""

    batch_id: str
    inserted: list[str]
    issues: list[Issue]


@dataclass(eq=False)
class _ValidRow:
    """通过全部校验的行：预生成 tag_id（父子引用一致）并缓存查询向量。"""

    row: TagRow
    tag_id: str
    vec: np.ndarray  # 1-D float32 归一化向量（embedder.embed 单行输出）


def _parse_allowed_types(raw: str) -> set[str]:
    """逗号分隔白名单解析；空串/纯分隔符 → 空集（不校验）。"""
    return {part.strip() for part in raw.split(",") if part.strip()}


def _chain_reaches_cycle(row: TagRow, batch_by_name: dict[str, TagRow]) -> bool:
    """沿批内 parent 链上行，链中重现任一名称即成环。

    父名不在批内（命中库内或缺失）时链出批：库内已有链不会成环，
    缺失父链由 parent_not_found 单独负责。
    """
    seen: set[str] = set()
    current = row
    while True:
        if current.tag_name in seen:
            return True
        seen.add(current.tag_name)
        parent_name = current.parent_tag
        if parent_name is None or parent_name not in batch_by_name:
            return False
        current = batch_by_name[parent_name]


def run_governance(rows: list[TagRow], session: Session, embedder) -> GovernanceResult:
    """治理一批标签行：校验 → 拓扑序入库 → 批次摘要与审计日志落库并提交。"""
    settings = get_settings()
    threshold = settings.semantic_similar_threshold
    allowed_types = _parse_allowed_types(settings.allowed_tag_types)

    # 库内基线（非废弃标签）：名称→行 用于判重/父解析；带向量行用于语义比对
    db_rows = session.query(TagCatalog).all()
    db_by_name = {t.tag_name: t for t in db_rows if t.status != "deprecate"}
    db_candidates = [
        (t.tag_name, decode_vector(t.embedding))
        for t in db_rows
        if t.status != "deprecate" and t.embedding is not None
    ]

    # 批内预扫描：按名索引（parent 解析/成环走查用）与共享别名归属（批内全局判冲突）
    batch_by_name: dict[str, TagRow] = {}
    for r in rows:
        batch_by_name.setdefault(r.tag_name, r)
    alias_owners: dict[str, list[int]] = {}
    name_owners: dict[str, list[int]] = {}
    for i, r in enumerate(rows):
        # TagRow.aliases 标注 list[str]，但直接构造可传 None（brief 用例
        # 即如此）：治理层按"None = 无别名"兼容
        for alias in (r.aliases or []):
            alias_owners.setdefault(alias, []).append(i)
        name_owners.setdefault(r.tag_name, []).append(i)

    # ---------- 第一遍：逐行判定，判定即停，累计有效行集 ----------
    issues: list[Issue] = []
    valid: list[_ValidRow] = []
    valid_by_name: dict[str, _ValidRow] = {}
    earlier: list[tuple[str, str]] = []  # 批内更早行的 (tag_name, tag_type)

    for i, r in enumerate(rows):
        # 1) duplicate_name：同名同 type 立即判停；同名不同 type 让位语义检查
        #    （见模块 docstring），先记为待定，语义未命中时回填
        dup_now: str | None = None
        dup_deferred: str | None = None
        db_hit = db_by_name.get(r.tag_name)
        if db_hit is not None:
            if db_hit.tag_type == r.tag_type:
                dup_now = f"已存在 tag_id={db_hit.tag_id}"
            else:
                dup_deferred = (
                    f"同名标签已存在 tag_id={db_hit.tag_id}（tag_type 不同）")
        if dup_now is None:
            for earlier_name, earlier_type in earlier:
                if earlier_name != r.tag_name:
                    continue
                if earlier_type == r.tag_type:
                    dup_now = "批内重复"
                    break
                if dup_deferred is None:
                    dup_deferred = "批内更早行存在同名标签（tag_type 不同）"
        if dup_now is not None:
            issues.append(Issue(r.tag_name, "duplicate_name", dup_now))
            earlier.append((r.tag_name, r.tag_type))
            continue

        # 2) alias_conflict：别名撞库内名称 / 撞批内其他行名称 / 批内共享（预扫描）
        conflict_alias: str | None = None
        conflict_detail: str | None = None
        for alias in (r.aliases or []):
            if alias in db_by_name:
                conflict_alias, conflict_detail = alias, f"别名「{alias}」与库内标签名称冲突"
                break
            # 撞批内其他行名称（name_owners 含自身下标，须排除 i；别名与
            # 本行同名但无其他行同名时不冲突——自别名合法）
            if any(owner != i for owner in name_owners.get(alias, [])):
                conflict_alias, conflict_detail = alias, f"别名「{alias}」与本批其他行标签名称冲突"
                break
            if any(owner != i for owner in alias_owners.get(alias, [])):
                conflict_alias, conflict_detail = alias, f"别名「{alias}」与本批其他行共享"
                break
        if conflict_alias is not None:
            issues.append(Issue(r.tag_name, "alias_conflict", conflict_detail))
            earlier.append((r.tag_name, r.tag_type))
            continue

        # 3) semantic_similar：与库内向量（含本批已过检行）余弦 ≥ 阈值（含等于）
        text = f"{r.tag_name}。{r.description}"
        vec = embedder.embed([text])[0]
        hits = search_similar(
            vec, db_candidates + [(v.row.tag_name, v.vec) for v in valid], top_k=1)
        if hits and hits[0][1] >= threshold:
            similar_name, score = hits[0]
            issues.append(Issue(
                r.tag_name, "semantic_similar",
                f"与已有标签「{similar_name}」余弦相似度 {score:.3f} ≥ 阈值 {threshold}",
                similar_to=similar_name))
            earlier.append((r.tag_name, r.tag_type))
            continue

        # 3.5) 同名不同 type 且语义未命中 → 回填 duplicate_name
        #      （tag_name 唯一约束，放行必然 IntegrityError，必须拦截）
        if dup_deferred is not None:
            issues.append(Issue(r.tag_name, "duplicate_name", dup_deferred))
            earlier.append((r.tag_name, r.tag_type))
            continue

        # 4) parent_not_found：父名既不在库内也不在批内任一行
        if r.parent_tag is not None and r.parent_tag not in db_by_name \
                and r.parent_tag not in batch_by_name:
            issues.append(Issue(
                r.tag_name, "parent_not_found", f"父标签「{r.parent_tag}」不在库内或本批"))
            earlier.append((r.tag_name, r.tag_type))
            continue

        # 5) parent_cycle：只查批内新增边（父链走出批内即无环风险）
        if r.parent_tag is not None and r.parent_tag in batch_by_name \
                and _chain_reaches_cycle(r, batch_by_name):
            issues.append(Issue(
                r.tag_name, "parent_cycle", f"批内父链成环（涉及「{r.parent_tag}」）"))
            earlier.append((r.tag_name, r.tag_type))
            continue

        # 6) tag_type_invalid：白名单非空且不在白名单（契约序末位）
        if allowed_types and r.tag_type not in allowed_types:
            issues.append(Issue(
                r.tag_name, "tag_type_invalid",
                f"tag_type「{r.tag_type}」不在白名单：{'、'.join(sorted(allowed_types))}"))
            earlier.append((r.tag_name, r.tag_type))
            continue

        # 通过全部检查 → 有效行集（tag_id 此时定死，供子行 parent 引用）
        valid_row = _ValidRow(
            row=r,
            tag_id=r.tag_id if r.tag_id else f"tag_{uuid.uuid4().hex[:8]}",
            vec=vec,
        )
        valid.append(valid_row)
        valid_by_name[r.tag_name] = valid_row
        earlier.append((r.tag_name, r.tag_type))

    # ---------- 第二遍：有效行集按批内父子拓扑序统一插入 ----------
    now = datetime.now(timezone.utc)
    batch_id = f"batch_{uuid.uuid4().hex[:8]}"
    inserted: list[str] = []
    inserted_names: set[str] = set()
    pending = list(valid)
    while pending:
        # 就绪 = 无批内父、父已插入、或父不在有效行集（库内父 / 兜底拦截两种情形）
        ready = [v for v in pending
                 if v.row.parent_tag is None
                 or v.row.parent_tag in inserted_names
                 or v.row.parent_tag not in valid_by_name]
        if not ready:
            # 理论不可达：parent_cycle 已拦截全部批内环，有效行集父链无环
            break
        for v in ready:
            pending.remove(v)
            parent_name = v.row.parent_tag
            parent_id: str | None = None
            if parent_name is not None:
                if parent_name in db_by_name:
                    parent_id = db_by_name[parent_name].tag_id
                elif parent_name in valid_by_name:
                    parent_id = valid_by_name[parent_name].tag_id
                else:
                    # 父名只命中批内未通过校验的行：不插入，避免悬空外键
                    issues.append(Issue(
                        v.row.tag_name, "parent_not_found",
                        f"父标签「{parent_name}」在本批未通过校验"))
                    continue
            status = v.row.status or "active"  # status 缺省语义由入库层决定
            session.add(TagCatalog(
                tag_id=v.tag_id,
                tag_name=v.row.tag_name,
                tag_type=v.row.tag_type,
                description=v.row.description,
                parent_id=parent_id,
                aliases=json.dumps(v.row.aliases, ensure_ascii=False) if v.row.aliases else None,
                applicable_scope=v.row.applicable_scope,
                status=status,
                embedding=encode_vector(v.vec),
                created_at=now,
                updated_at=now,
            ))
            # 自引用外键无 relationship()：逐行 flush 保证父行先落库
            session.flush()
            after = {
                "tag_id": v.tag_id,
                "tag_name": v.row.tag_name,
                "tag_type": v.row.tag_type,
                "description": v.row.description,
                "parent_id": parent_id,
                "aliases": v.row.aliases,
                "applicable_scope": v.row.applicable_scope,
                "status": status,
                "batch_id": batch_id,
            }
            session.add(TagChangeLog(
                tag_id=v.tag_id,
                action="import",
                before_json=None,
                after_json=json.dumps(after, ensure_ascii=False),
                operator=_OPERATOR,
                created_at=now,
            ))
            inserted.append(v.tag_id)
            inserted_names.add(v.row.tag_name)

    # ---------- 批次摘要落库并提交 ----------
    total = len(rows)
    session.add(ImportBatch(
        batch_id=batch_id,
        file_name=_BATCH_FILE_NAME,
        status="completed",
        result_summary=json.dumps(
            {
                "total": total,
                "inserted": len(inserted),
                "skipped": total - len(inserted),
                "issues": len(issues),
            },
            ensure_ascii=False,
        ),
        created_at=now,
    ))
    session.commit()
    return GovernanceResult(batch_id=batch_id, inserted=inserted, issues=issues)
