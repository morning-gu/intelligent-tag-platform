"""标签治理流水线 v2：TagRow 行集 → 目录/标签双表落位 + 导入任务与审计。

落位规则（计划契约速查，docs/table_design.md §2.1/§8）：
- 行分类：名称被批内任一行的 parent_tag 引用 → **目录行**（tag_catalog，
  dir_type 取行值（树状 XML 目录属性），缺省/非法归一 dir_dynamic；
  applicable_scope 为 content/scene/character 组合，缺省/非法归一三值
  全选）；其余 → **标签行**（tag_define，tag_type 取行值，缺省/非法
  归一 val_fixed，val_generalized 放行落库）。层级 Excel/XML 的一/二/
  三级天然被下级行引用成目录链，「标签」列与扁平行不被引用即标签——
  分类无需感知解析层模板形状。已知边界：解析层把层级节点摊平为
  TagRow（不保留"目录节点"标记），**无子引用的末级空目录**（如 XML 的
  `<level2 name="剧情"/>` 且其下无标签）会退化为标签行；层级 Excel 不受
  影响（标签列必填，层级行恒被本行标签引用）；
- 标签行归属目录：parent_tag 命中批内目录行 > 库内同名目录（根目录
  优先、同级按 dir_id 稳定）> parent_tag 为空时落根目录「未分类」
  （批内同名根目录计划 > 库内 > 自动创建，不重复建第二个同名根目录）；
  三者皆不中报 parent_not_found；
- 目录行幂等复用：同 (父目录, 目录名) 已存在（库内或批内已计划）则
  复用，不报 issue、不重复建、不写变更日志；
- 状态字面量 v2 统一 "deprecated"：行 status 的 v1 取值 "deprecate"
  一并归一，其余取值按 active 入库（CHECK 约束兜底）；
- 停用标签同 (dir, name) 再导入复活：保留原 tag_id 恢复 active，写
  before_json 供撤销还原（v1 语义沿用）；
- 别名冲突收窄至标签命名空间（v1 单表为目录/标签混用全员）：别名撞
  「标签名」（库内非停用 tag_define 名 / 批内标签行名）或批内共享判
  冲突，目录行与标签行同规则；目录名不入该命名空间（目录不是标签）。

两遍法保留 v1 形状：第一遍逐行判定、判定即停（目录行按父链依赖序先行
检查——父计划就绪后子行才判定，被拒父的批内子孙自动级联拦截，零静默
搁浅；issue 最终按行序回排）；第二遍统一插入（目录链拓扑序 + 逐行
flush——自引用外键无 relationship()，保证父行先落库）。判定序
（v1 的 tag_type_invalid 随白名单校验退役而移除；semantic_similar
随手动导入取消余弦相似控制而退役）：
    duplicate_name → alias_conflict → parent_not_found
    （目录行另有 parent_cycle；标签行是叶子，无环可成）

向量编码仅用于入库存储（tag_define.embedding / tag_catalog.embedding
语料侧持久化，供召回/归一检索）：passage 角色批量预 embed（全批一次
调用，判定与插入共用同一批向量）。手动导入不再做语义相似拦截——
疑似同义标签由人工经待处理列表甄别。

审计：每个新建目录/标签行写 tag_change_log(action='import')，after_json
带 task_id（revoke/export 反查键，替代 v1 的 batch_id 字段名）与 kind
（'dir'|'tag'，撤销需区分删除对象——目录行复用不记日志）。

记账不变量（v2 口径）：每行要么产生新实体（inserted / 变更日志）、要么
幂等复用已有目录、要么有 issue。
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

import numpy as np
from sqlalchemy.orm import Session

from tagplatform.db.models import TagCatalog, TagChangeLog, TagDefine, TagImportTask
from tagplatform.governance.excel import TagRow
from tagplatform.vectors import encode_vector

# run_governance 无文件名入参，tag_import_task.file_name 记录写入来源；
# API 层拿到 task_id 后回填真实上传文件名（与 parse_errors 同一回写）
_TASK_FILE_NAME = "governance"

# 扁平标签行（parent_tag 为空）落入的自动根目录
_UNCATEGORIZED_DIR_NAME = "未分类"

# 审计日志操作者标识：本流水线产生的 import 动作统一记 governance
_OPERATOR = "governance"

# v2 落位常量：导入路径缺省值——动态目录 + 三范围全选 + 固定值标签
# （行值优先，非法枚举归一回默认；dir_type=dir_static / val_generalized
# 行值合法时按行落库）
_DIR_TYPES = ("dir_static", "dir_dynamic")
_DEFAULT_DIR_TYPE = "dir_dynamic"
_SCOPES = ("content", "scene", "character")
_DEFAULT_SCOPE = "content,scene,character"
_TAG_TYPES = ("val_fixed", "val_generalized")
_DEFAULT_TAG_TYPE = "val_fixed"


@dataclass
class Issue:
    """单行治理问题；similar_to 保留字段位（semantic_similar 退役后
    现有 issue 码不再填充，历史序列化结果兼容）。"""

    tag_name: str
    code: str
    detail: str
    similar_to: str | None = None


@dataclass
class GovernanceResult:
    """治理结果：本批 task_id、入库标签 tag_id 列表（目录行不计入——
    inserted 语义沿 v1 为"标签入库数"，目录创建属结构落位）、问题列表
    （与插入行不相交；目录复用行既不在 inserted 也无 issue）。

    v2 计数口径统一为标签行：result_summary 的 total（= 标签行数）/
    inserted / skipped（= total - inserted）三键同口径；目录行与
    parse_errors 不进任何计数。"""

    task_id: str
    inserted: list[str]
    issues: list[Issue]


@dataclass
class GovernanceConfig:
    """治理显式配置。v2 随手动导入取消余弦相似控制，semantic_similar
    检查退役（similar_threshold / settings.semantic_similar_threshold
    一并退役）；类保留为 run_governance 的 config 参数类型占位——
    显式传参（如旧测试）被忽略，不再影响判定行为。"""

    pass


@dataclass(eq=False)
class _DirPlan:
    """批内目录行的落位计划：依赖序检查通过后按序插入/复用。"""

    row: TagRow
    vec: np.ndarray  # 本行向量（批量编码矩阵中本行向量，仅用于入库存储）
    dir_id: str  # 复用库内目录时为库内 dir_id，新建时为预生成 id
    parent_plan: _DirPlan | None  # 父为批内目录行时指向其计划
    parent_db_id: str | None  # 父为库内已有目录时的 dir_id
    reused: bool  # True = 幂等复用库内目录（不插入、不记变更日志）


@dataclass(eq=False)
class _ValidTag:
    """通过全部校验的标签行：目标目录已解析、向量与 tag_id 就绪。"""

    row: TagRow
    tag_id: str
    vec: np.ndarray  # 1-D float32 归一化向量（判定与插入共用）
    dir_id: str  # 目标目录 id（批内新目录为预生成 id，插入阶段已落库）
    reactivate: TagDefine | None  # 非 None = 复活该库内停用行（UPDATE 而非 INSERT）


def _normalize_scope(raw: str | None) -> str:
    """目录 applicable_scope 归一：content/scene/character 组合（逗号或
    | 分隔、去重、规范序），空/全非法回退三值全选（CHECK 兜底）。"""
    if not raw:
        return _DEFAULT_SCOPE
    tokens = {t.strip() for t in re.split(r"[,，;；|]", raw) if t.strip()}
    valid = [s for s in _SCOPES if s in tokens]
    return ",".join(valid) if valid else _DEFAULT_SCOPE


def _normalize_dir_type(raw: str | None) -> str:
    """目录 dir_type 归一：非法/缺省回退 dir_dynamic。"""
    return raw if raw in _DIR_TYPES else _DEFAULT_DIR_TYPE


def _normalize_tag_type(raw: str | None) -> str:
    """标签 tag_type 归一：非法/缺省回退 val_fixed（val_generalized 放行）。"""
    return raw if raw in _TAG_TYPES else _DEFAULT_TAG_TYPE


def _normalize_status(raw: str | None) -> str:
    """标签 status 归一：v1 字面量 "deprecate" 并入 v2 "deprecated"。"""
    return "deprecated" if raw in ("deprecated", "deprecate") else "active"


def run_governance(rows: list[TagRow], session: Session, embedder,
                   config: GovernanceConfig | None = None) -> GovernanceResult:
    """治理一批标签行：分类落位 → 校验 → 双表拓扑序入库 → 任务摘要与
    审计日志落库并提交。

    config 参数保留仅为既有调用方（tests、API 路由）签名兼容；
    semantic_similar 检查退役后其阈值不再参与判定，配置对象被忽略。
    """
    now = datetime.now(timezone.utc)
    task_id = f"task_{uuid.uuid4().hex[:8]}"

    # ---------- 库内基线 ----------
    db_dirs = session.query(TagCatalog).all()
    # 裸名解析：根目录优先、同级按 dir_id 稳定（多目录同名时取首个，
    # 与 v1 batch_by_name/db_by_name 的"按名解析"语义一脉相承）
    db_dir_by_name: dict[str, TagCatalog] = {}
    for d in sorted(db_dirs, key=lambda d: (d.parent_id is not None, d.dir_id)):
        db_dir_by_name.setdefault(d.dir_name, d)
    # (父目录 id, 目录名) → 目录行：目录幂等复用查表（根目录 NULL 键由
    # 服务层查重——UNIQUE 对 NULL 互不区分，沿 v1 0006 口径）
    db_dir_by_key: dict[tuple[str | None, str], TagCatalog] = {}
    for d in db_dirs:
        db_dir_by_key.setdefault((d.parent_id, d.dir_name), d)

    db_defines = session.query(TagDefine).all()
    db_define_by_key = {(t.dir_id, t.tag_name): t for t in db_defines}
    db_define_names = {t.tag_name for t in db_defines if t.status != "deprecated"}

    # ---------- 批内分类：被引用名为目录行，其余为标签行 ----------
    referenced_names = {r.parent_tag for r in rows if r.parent_tag is not None}
    dir_items = [(i, r) for i, r in enumerate(rows) if r.tag_name in referenced_names]
    dir_row_by_name: dict[str, TagRow] = {}
    for _i, r in dir_items:
        # 裸名引用解析取首个出现的目录行（v1 batch_by_name.setdefault 语义）
        dir_row_by_name.setdefault(r.tag_name, r)
    tag_name_owners: dict[str, list[int]] = {}
    for i, r in enumerate(rows):
        if r.tag_name not in referenced_names:
            tag_name_owners.setdefault(r.tag_name, []).append(i)

    # 别名归属预扫描（批内全局，v1 语义：共享别名判冲突，全部行参与）
    alias_owners: dict[str, list[int]] = {}
    for i, r in enumerate(rows):
        # TagRow.aliases 标注 list[str]，但直接构造可传 None：按"None = 无
        # 别名"兼容
        for alias in (r.aliases or []):
            alias_owners.setdefault(alias, []).append(i)

    def _alias_conflict(r: TagRow, i: int) -> tuple[str | None, str | None]:
        """别名撞库内标签名 / 撞批内其他标签行名 / 批内共享（预扫描）。"""
        for alias in (r.aliases or []):
            if alias in db_define_names:
                return alias, f"别名「{alias}」与库内标签名称冲突"
            # 撞批内其他标签行名（owners 含自身下标，须排除 i；别名与本行
            # 同名但无其他行同名时不冲突——自别名合法）
            if any(owner != i for owner in tag_name_owners.get(alias, [])):
                return alias, f"别名「{alias}」与本批其他标签名称冲突"
            if any(owner != i for owner in alias_owners.get(alias, [])):
                return alias, f"别名「{alias}」与本批其他行共享"
        return None, None

    # ---------- 批量编码：全部待检行一次 embed ----------
    # 判定阶段的语义检查与第二遍插入共用同一批向量；扁平标签行需要自动
    # 「未分类」目录且库内没有时，其文本作为尾项并入同一批调用（仍是
    # 单次批量编码，无逐行调用）。向量与行按下标对齐，尾项向量单独取用。
    # role=passage：入库向量成为被检索语料（与召回/归一的 query 侧相对）。
    uncat_existing = db_dir_by_name.get(_UNCATEGORIZED_DIR_NAME)
    # 「未分类」落点回退值（库内 > 新建）：批内层级根目录恰名「未分类」
    # 时在 pass A 后改指批内计划（见 uncat_plan 回填）——扁平行复用该
    # 批内目录，不重复建第二个同名根目录
    uncat_dir_id = (uncat_existing.dir_id if uncat_existing is not None
                    else f"dir_{uuid.uuid4().hex[:8]}")
    needs_uncat = any(
        r.parent_tag is None and r.tag_name not in referenced_names for r in rows
    )
    uncat_vec: np.ndarray | None = None
    texts = [f"{r.tag_name}。{r.description}" for r in rows]
    if needs_uncat and uncat_existing is None:
        texts.append(f"{_UNCATEGORIZED_DIR_NAME}。")
    batch_vecs = embedder.embed(texts, role="passage") if texts else []
    if needs_uncat and uncat_existing is None and len(batch_vecs) > len(rows):
        uncat_vec = batch_vecs[len(rows)]

    issues: list[tuple[int, Issue]] = []  # (行下标, issue)——最终按行序回排

    def _check_dir_row(i: int, r: TagRow, parent_index: int | None) -> None:
        """单个目录行的依赖序检查：alias_conflict → 父解析 → 幂等复用/计划。

        父行已检查但无计划（被拒）→ 本行 parent_not_found 并级联——其批内
        子孙下一轮因父无计划而同样走此分支，零静默搁浅。
        """
        checked_dir.add(i)
        vec = batch_vecs[i]

        # 1) alias_conflict（目录行别名同样不得撞标签名/批内共享）
        conflict_alias, conflict_detail = _alias_conflict(r, i)
        if conflict_alias is not None:
            issues.append((i, Issue(r.tag_name, "alias_conflict", conflict_detail)))
            return

        # 2) 父解析：批内目录行计划 / 库内同名目录 / 根
        parent_plan: _DirPlan | None = None
        parent_db_id: str | None = None
        if r.parent_tag is not None:
            if parent_index is not None:
                parent_plan = plans.get(parent_index)
                if parent_plan is None:
                    # 父目录行在本批未通过校验：本行拦截，子孙级联
                    issues.append((i, Issue(
                        r.tag_name, "parent_not_found",
                        f"父目录「{r.parent_tag}」在本批未通过校验")))
                    return
            else:
                db_parent = db_dir_by_name.get(r.parent_tag)
                if db_parent is None:
                    issues.append((i, Issue(
                        r.tag_name, "parent_not_found",
                        f"父目录「{r.parent_tag}」不在库内或本批")))
                    return
                parent_db_id = db_parent.dir_id

        # 3) 幂等复用：同 (父目录, 目录名) 的批内计划 / 库内目录直接复用
        parent_id = (parent_plan.dir_id if parent_plan is not None
                     else parent_db_id)
        key = (parent_id, r.tag_name)
        dedup = plan_by_key.get(key)
        if dedup is not None:
            plan_by_name.setdefault(r.tag_name, dedup)
            return
        db_reuse = db_dir_by_key.get(key)
        if db_reuse is not None:
            plan = _DirPlan(row=r, vec=vec, dir_id=db_reuse.dir_id,
                            parent_plan=parent_plan, parent_db_id=parent_db_id,
                            reused=True)
        else:
            plan = _DirPlan(row=r, vec=vec,
                            dir_id=(r.tag_id if r.tag_id
                                    else f"dir_{uuid.uuid4().hex[:8]}"),
                            parent_plan=parent_plan, parent_db_id=parent_db_id,
                            reused=False)
        plans[i] = plan
        plan_by_name.setdefault(r.tag_name, plan)
        plan_by_key[key] = plan
        checked_plans.append(plan)

    # ---------- 第一遍 A：目录行按父链依赖序检查（父计划就绪才判子行） ----------
    # 目录行不能按行序直判（子行可能先于父行出现），按依赖序反复扫描：
    # 父目录行已决（有计划或已被拒）才检查本行；永不能就绪的行即批内环
    # 链成员（含链入环的行），整链报 parent_cycle。
    checked_dir: set[int] = set()
    plans: dict[int, _DirPlan] = {}  # 行下标 → 计划
    plan_by_name: dict[str, _DirPlan] = {}  # 裸名 → 首个同名目录行计划
    plan_by_key: dict[tuple[str | None, str], _DirPlan] = {}  # (父id, 名) → 计划
    checked_plans: list[_DirPlan] = []  # 拓扑序（父先于子），插入阶段按序消费
    item_by_name: dict[str, tuple[int, TagRow]] = {}
    for i, r in dir_items:
        item_by_name.setdefault(r.tag_name, (i, r))

    remaining = list(dir_items)
    while remaining:
        progressed = False
        still_pending: list[tuple[int, TagRow]] = []
        for i, r in remaining:
            parent_item = item_by_name.get(r.parent_tag) if r.parent_tag else None
            if parent_item is not None and parent_item[0] not in checked_dir:
                still_pending.append((i, r))  # 父目录行未决，等下一轮
                continue
            progressed = True
            _check_dir_row(i, r, parent_item[0] if parent_item else None)
        remaining = still_pending
        if not progressed:
            break
    for i, r in remaining:
        # 批内父链成环（含链入环的行）：目录行整链拦截
        issues.append((i, Issue(
            r.tag_name, "parent_cycle", f"批内父链成环（涉及「{r.parent_tag}」）")))
        checked_dir.add(i)

    # 「未分类」解析回填：批内层级根目录恰名「未分类」时，扁平行复用该
    # 批内目录计划（plan_by_key 根键），不自动新建第二个同名根目录；
    # 批内无此计划（或该行被拒）时维持库内/新建回退值
    uncat_plan = plan_by_key.get((None, _UNCATEGORIZED_DIR_NAME))
    if uncat_plan is not None:
        uncat_dir_id = uncat_plan.dir_id

    # ---------- 第一遍 B：标签行按行序检查，判定即停 ----------
    valid_tags: list[_ValidTag] = []
    claimed: set[tuple[str, str]] = set()  # (目标目录标识, 标签名) 批内占用

    for i, r in enumerate(rows):
        if r.tag_name in referenced_names:
            continue  # 目录行已在 A 段处理

        vec = batch_vecs[i]
        # 目标目录解析：批内目录行计划 > 库内同名目录 > 「未分类」> 缺失
        if r.parent_tag is None:
            target_dir_id = uncat_dir_id
            target_missing = False
        elif (plan := plan_by_name.get(r.parent_tag)) is not None:
            target_dir_id = plan.dir_id
            target_missing = False
        elif (db_dir := db_dir_by_name.get(r.parent_tag)) is not None:
            target_dir_id = db_dir.dir_id
            target_missing = False
        else:
            target_dir_id = None
            target_missing = True

        # 1) duplicate_name：库内 (dir_id, tag_name)（停用行放行待复活）
        #    与批内占用两路；目标缺失时按缺失父标识占位判批内重复
        reactivate: TagDefine | None = None
        if not target_missing:
            db_hit = db_define_by_key.get((target_dir_id, r.tag_name))
            if db_hit is not None:
                if db_hit.status == "deprecated":
                    reactivate = db_hit  # 停用标签再导入复活，保留原 tag_id
                else:
                    issues.append((i, Issue(
                        r.tag_name, "duplicate_name",
                        f"已存在 tag_id={db_hit.tag_id}")))
                    continue
            claim_key = target_dir_id
        else:
            claim_key = f"missing:{r.parent_tag}"
        if (claim_key, r.tag_name) in claimed:
            issues.append((i, Issue(r.tag_name, "duplicate_name", "批内重复")))
            continue

        # 2) alias_conflict（目录行与标签行同规则：别名不得撞标签名）
        conflict_alias, conflict_detail = _alias_conflict(r, i)
        if conflict_alias is not None:
            issues.append((i, Issue(r.tag_name, "alias_conflict", conflict_detail)))
            continue

        # 3) parent_not_found：父名既不在批内目录行也不在库内目录
        #    （v2 semantic_similar 余弦拦截退役：疑似同义标签直接放行
        #    入库，由人工经待处理列表甄别）
        if target_missing:
            issues.append((i, Issue(
                r.tag_name, "parent_not_found",
                f"父目录「{r.parent_tag}」不在库内或本批")))
            continue

        # 通过全部检查 → 有效标签行（复活行沿用库内 tag_id，其余按行自带
        # tag_id 或预生成——与 v1 的 id 来源语义一致）
        valid_tags.append(_ValidTag(
            row=r,
            tag_id=(reactivate.tag_id if reactivate is not None
                    else (r.tag_id if r.tag_id else f"tag_{uuid.uuid4().hex[:8]}")),
            vec=vec,
            dir_id=target_dir_id,
            reactivate=reactivate,
        ))
        claimed.add((claim_key, r.tag_name))

    # ---------- 第二遍：双表拓扑序统一插入（逐行 flush 保证父行先落库） ----------
    inserted: list[str] = []

    # 1) 目录：按拓扑序插入未复用计划（自引用外键无 relationship()，逐行
    #    flush 保证父行先落库）；复用计划不插入不记日志
    for plan in checked_plans:
        if plan.reused:
            continue
        parent_id = (plan.parent_plan.dir_id if plan.parent_plan is not None
                     else plan.parent_db_id)
        session.add(TagCatalog(
            dir_id=plan.dir_id,
            dir_name=plan.row.tag_name,
            dir_type=_normalize_dir_type(plan.row.dir_type),
            description=plan.row.description,
            parent_id=parent_id,
            aliases=(json.dumps(plan.row.aliases, ensure_ascii=False)
                     if plan.row.aliases else None),
            applicable_scope=_normalize_scope(plan.row.applicable_scope),
            embedding=encode_vector(plan.vec),
            created_at=now,
            updated_at=now,
        ))
        session.flush()
        after = {
            "kind": "dir",
            "dir_id": plan.dir_id,
            "dir_name": plan.row.tag_name,
            "parent_id": parent_id,
            "task_id": task_id,
        }
        session.add(TagChangeLog(
            tag_id=plan.dir_id,
            action="import",
            before_json=None,
            after_json=json.dumps(after, ensure_ascii=False),
            operator=_OPERATOR,
            created_at=now,
        ))

    # 2) 「未分类」根目录：有扁平标签落点、批内无同名根目录计划且库内没有
    #    时创建（幂等复用；批内计划已在 1) 按拓扑序落库）
    if needs_uncat and uncat_existing is None and uncat_plan is None:
        session.add(TagCatalog(
            dir_id=uncat_dir_id,
            dir_name=_UNCATEGORIZED_DIR_NAME,
            dir_type=_DEFAULT_DIR_TYPE,
            description="",
            parent_id=None,
            aliases=None,
            applicable_scope=_DEFAULT_SCOPE,
            embedding=encode_vector(uncat_vec) if uncat_vec is not None else None,
            created_at=now,
            updated_at=now,
        ))
        session.flush()
        after = {
            "kind": "dir",
            "dir_id": uncat_dir_id,
            "dir_name": _UNCATEGORIZED_DIR_NAME,
            "parent_id": None,
            "task_id": task_id,
        }
        session.add(TagChangeLog(
            tag_id=uncat_dir_id,
            action="import",
            before_json=None,
            after_json=json.dumps(after, ensure_ascii=False),
            operator=_OPERATOR,
            created_at=now,
        ))

    # 3) 标签：逐行插入 / 复活（dir_id 已就绪），逐行 flush + 变更日志
    for v in valid_tags:
        existing = v.reactivate
        before_json = None
        if existing is None:
            session.add(TagDefine(
                tag_id=v.tag_id,
                tag_name=v.row.tag_name,
                tag_type=_normalize_tag_type(v.row.tag_type),
                description=v.row.description,
                dir_id=v.dir_id,
                aliases=(json.dumps(v.row.aliases, ensure_ascii=False)
                         if v.row.aliases else None),
                status=_normalize_status(v.row.status),
                embedding=encode_vector(v.vec),
                created_at=now,
                updated_at=now,
            ))
        else:
            before_json = json.dumps(
                {"status": existing.status}, ensure_ascii=False)
            existing.tag_type = _normalize_tag_type(v.row.tag_type)
            existing.description = v.row.description
            existing.dir_id = v.dir_id
            existing.aliases = (
                json.dumps(v.row.aliases, ensure_ascii=False)
                if v.row.aliases else None
            )
            existing.status = _normalize_status(v.row.status)
            existing.embedding = encode_vector(v.vec)
            existing.updated_at = now
        session.flush()
        after = {
            "kind": "tag",
            "tag_id": v.tag_id,
            "tag_name": v.row.tag_name,
            "dir_id": v.dir_id,
            "description": v.row.description,
            "aliases": v.row.aliases,
            "status": _normalize_status(v.row.status),
            "task_id": task_id,
        }
        session.add(TagChangeLog(
            tag_id=v.tag_id,
            action="import",
            before_json=before_json,
            after_json=json.dumps(after, ensure_ascii=False),
            operator=_OPERATOR,
            created_at=now,
        ))
        inserted.append(v.tag_id)

    # ---------- 导入任务摘要落库并提交 ----------
    # total 仅计标签定义行（目录行不计）：同 inserted/skipped 的标签行
    # 口径——目录创建属结构落位，不进计数（如 37 目录 + 514 标签的模板
    # 文件 total=514 而非 551）
    total = len(rows) - len(dir_items)
    session.add(TagImportTask(
        task_id=task_id,
        file_name=_TASK_FILE_NAME,
        status="completed",
        result_summary=json.dumps(
            {
                "total": total,
                # inserted 仅计标签行（v1 语义延续）；目录复用行既不计入
                # inserted 也无 issue，体现在 skipped = total - inserted 中
                "inserted": len(inserted),
                "skipped": total - len(inserted),
                # issues 为明细列表（非计数）：GET /api/v1/import/tasks/{task_id}
                # 与前端导入任务页按此解析（Task 12 消费契约）
                "issues": [asdict(issue) for _i, issue in
                           sorted(issues, key=lambda pair: pair[0])],
            },
            ensure_ascii=False,
        ),
        created_at=now,
    ))
    session.commit()
    ordered_issues = [issue for _i, issue in sorted(issues, key=lambda pair: pair[0])]
    return GovernanceResult(task_id=task_id, inserted=inserted, issues=ordered_issues)
