"""11 张 v2 表 ORM 模型（数据底座的列契约，doc §3 DDL 逐字落地）。

本模块的属性名即平台契约：后续任务（内容导入、治理导入、生成流水线、
审核、API 层、前端）一律按这些属性读写数据库。v2 相对 v1：
- media_asset 与 media_context 合并（content_json/source 落列，识别状态
  = content_json IS NULL）；
- 目录（tag_catalog）与标签（tag_define）拆双表；
- 生成产出与审核折进 pre_asset_tag（行即日志），正式挂载独立为干净的
  asset_tag，review_record 表删除；
- generation_task → tag_generation_task（外键随合并改 asset_id）、
  import_batch → tag_import_task（改语义名，结构不变）。

列定义与设计文档 §3 逐字对应；v2 起枚举列按 DDL 加 CHECK 约束（doc §3
内联 CHECK 逐字落地，合法性由库层兜底），括号注记的取值（如
movie|series|scene）以注释记录。user_account / prompt_template /
asset_build_task 三表本期物理不动，ORM 保持 v1 形状（不加 CHECK，PG 迁移
时按 doc §3.9-3.11 统一对齐）。
分层红线：本模块不得 import fastapi（设计文档 §9.4）。
"""

from datetime import datetime

from sqlalchemy import (
    REAL,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Text,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """全平台 ORM 基类；metadata 供 alembic autogenerate 使用。"""


class MediaAsset(Base):
    """媒体资产：movie/series/scene 三级结构，子级经 parent_id 挂到父级。

    v2 由 v1 media_asset + media_context 合并：content_json（内容识别产出
    JSON 串，PG 目标 JSONB）与 source（识别结果来源）落本表列；重新识别
    直接覆盖 content_json（历史版本由 mtg 审计文件留档）。v1 的
    media_context.embedding 内容向量缓存列不再保留——内容向量在生成任务
    启动时实时计算（doc §3.1）。
    """

    __tablename__ = "media_asset"

    asset_id: Mapped[str] = mapped_column(Text, primary_key=True)
    # 取值 movie|series|scene
    asset_type: Mapped[str] = mapped_column(Text, nullable=False)
    parent_id: Mapped[str | None] = mapped_column(
        ForeignKey("media_asset.asset_id"), nullable=True
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    video_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    # JSON 串（PG 目标 JSONB）；NULL = 未完成内容识别
    content_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 识别结果来源（外部项目 / 上传构建）
    source: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        CheckConstraint("asset_type IN ('movie', 'series', 'scene')"),
        Index("idx_media_asset_parent", "parent_id"),
        Index("idx_media_asset_type", "asset_type"),
    )


class TagCatalog(Base):
    """标签目录：目录树的节点（目录本身不是标签），父子层级 + 别名 +
    适用范围。

    v2 与标签定义拆分：叶子标签在 tag_define（经 dir_id 挂到本表）。
    dir_type 决定子节点能否由开放生成动态扩充（dir_static 固定枚举 /
    dir_dynamic 可扩充，doc §3.2）。
    """

    __tablename__ = "tag_catalog"

    dir_id: Mapped[str] = mapped_column(Text, primary_key=True)
    dir_name: Mapped[str] = mapped_column(Text, nullable=False)
    # 取值 dir_static|dir_dynamic
    dir_type: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    parent_id: Mapped[str | None] = mapped_column(
        ForeignKey("tag_catalog.dir_id"), nullable=True
    )
    # JSON 数组字符串（如 '["悬念"]'），由服务层序列化
    aliases: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 取值 content|scene|character（声明目录适用范围）
    applicable_scope: Mapped[str | None] = mapped_column(
        Text, nullable=True, server_default="content"
    )
    # 目录描述向量（open 候选归置/目录检索用）
    embedding: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        CheckConstraint("dir_type IN ('dir_static', 'dir_dynamic')"),
        CheckConstraint("applicable_scope IN ('content', 'scene', 'character')"),
        # 根目录（parent_id NULL）同名在 SQL 层不拦（UNIQUE 对 NULL 互不区分），
        # 由服务层校验——沿 v1 0006 口径（doc §3.2）
        UniqueConstraint("parent_id", "dir_name", name="ux_tag_catalog_parent_name"),
        Index("idx_tag_catalog_type", "dir_type"),
        Index("idx_tag_catalog_scope", "applicable_scope"),
    )


class TagDefine(Base):
    """标签定义：具体标签，隶属目录（dir_id 必填）。

    tag_type 决定值固定还是泛化（val_fixed 本期启用 / val_generalized 结构
    预留：tag_id 指向定义、实际值存 asset_tag.tag_name，doc §3.3）；
    status 字面量为 deprecated（v2 统一口径，替代 v1 的 "deprecate"）。
    标签适用范围继承所属目录的 applicable_scope，不在本表重复声明。
    """

    __tablename__ = "tag_define"

    tag_id: Mapped[str] = mapped_column(Text, primary_key=True)
    tag_name: Mapped[str] = mapped_column(Text, nullable=False)
    # 取值 val_fixed|val_generalized
    tag_type: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    dir_id: Mapped[str] = mapped_column(
        Text, ForeignKey("tag_catalog.dir_id"), nullable=False
    )
    aliases: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 取值 active|deprecated
    status: Mapped[str | None] = mapped_column(
        Text, nullable=True, server_default="active"
    )
    # 标签语义向量（closed 召回 passage 向量持久化）
    embedding: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        CheckConstraint("tag_type IN ('val_fixed', 'val_generalized')"),
        CheckConstraint("status IN ('active', 'deprecated')"),
        UniqueConstraint("dir_id", "tag_name", name="ux_tag_define_dir_name"),
        Index("idx_tag_define_status", "status"),
        Index("idx_tag_define_dir", "dir_id"),
    )


class TagGenerationTask(Base):
    """标签生成任务：closed（目录内封闭生成）| open（开放候选生成）。

    v2 外键从 v1 generation_task.context_id（→media_context）改为 asset_id
    （→media_asset，随 media 合并，doc §3.4）。closed 模式仍消费
    prompt_template；open 模式由 mtg-v1 内置流水线接管（不走模板）。
    """

    __tablename__ = "tag_generation_task"

    task_id: Mapped[str] = mapped_column(Text, primary_key=True)
    asset_id: Mapped[str] = mapped_column(
        Text, ForeignKey("media_asset.asset_id"), nullable=False
    )
    # 取值 closed|open
    mode: Mapped[str] = mapped_column(Text, nullable=False)
    model_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    policy_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 取值 queued|running|succeeded|failed
    status: Mapped[str] = mapped_column(Text, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        CheckConstraint("mode IN ('closed', 'open')"),
        CheckConstraint("status IN ('queued', 'running', 'succeeded', 'failed')"),
        Index("idx_gen_task_asset", "asset_id"),
        Index("idx_gen_task_status", "status"),
    )


class PreAssetTag(Base):
    """待审核标签行：生成任务的产出落地此表，审核通过后复制到 asset_tag。

    本表是 asset×tag 去重后的生成与审核记录（全量原始轮次档案由 mtg 审计
    文件兼任）：审核事实（reviewer_id/review_comment/review_status）内嵌
    行内，行即日志、至多一审且终态不被覆盖（v1 review_record 表删除，
    doc §6.2）。origin 记录产出来源且不可变；open 行合入标签库时才赋
    tag_id/candidate_dir_id（生成端 mtg-v1 自拟分类不知道目录体系，
    candidate_dir_id 生成时可空，doc §6.4）。

    两个部分唯一索引保证重跑幂等（重跑语义：approved/rejected 终审跳过、
    pending 删旧插新，索引只保证不重复，doc §6.1）：
    - ux_pre_tag_catalog：同一资产同一实体不重复挂同一标签定义；
    - ux_pre_tag_candidate：同一资产同一实体不重复收录同一候选名。
    """

    __tablename__ = "pre_asset_tag"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset_id: Mapped[str] = mapped_column(
        Text, ForeignKey("media_asset.asset_id"), nullable=False
    )
    # 挂载粒度：content 本期启用（target_id = asset_id 本身）；scene/character
    # 结构预留（target_id 取 content_json 内的 scene_id/character_id，doc §2.2）
    target_type: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="content"
    )
    target_id: Mapped[str] = mapped_column(Text, nullable=False)
    tag_id: Mapped[str | None] = mapped_column(
        ForeignKey("tag_define.tag_id"), nullable=True
    )
    candidate_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    candidate_dir_id: Mapped[str | None] = mapped_column(
        ForeignKey("tag_catalog.dir_id"), nullable=True
    )
    candidate_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 取值 closed|open（不可变，记录产出来源）
    origin: Mapped[str] = mapped_column(Text, nullable=False)
    relevance_score: Mapped[float | None] = mapped_column(REAL, nullable=True)
    confidence: Mapped[float | None] = mapped_column(REAL, nullable=True)
    evidence_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    task_id: Mapped[str | None] = mapped_column(
        ForeignKey("tag_generation_task.task_id"), nullable=True
    )
    # 取值 pending|approved|rejected（审核动作映射见 doc §6.2）
    review_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="pending"
    )
    reviewer_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    review_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        CheckConstraint("target_type IN ('content', 'scene', 'character')"),
        CheckConstraint("origin IN ('closed', 'open')"),
        CheckConstraint("review_status IN ('pending', 'approved', 'rejected')"),
        Index(
            "ux_pre_tag_catalog",
            "asset_id",
            "target_type",
            "target_id",
            "tag_id",
            unique=True,  # 唯一性是 doc 的语义要求（部分"唯一"索引）；缺失会静默不查重
            sqlite_where=text("tag_id IS NOT NULL"),
        ),
        Index(
            "ux_pre_tag_candidate",
            "asset_id",
            "target_type",
            "target_id",
            "candidate_name",
            unique=True,
            sqlite_where=text("tag_id IS NULL AND candidate_name IS NOT NULL"),
        ),
        Index("idx_pre_tag_asset", "asset_id"),
        Index("idx_pre_tag_task", "task_id"),
        Index("idx_pre_tag_review", "review_status"),
    )


class AssetTag(Base):
    """资产-标签正式挂载行：审核通过的标签复制到此表。

    只保留挂载关系与展示值（tag_name），不含评分/证据（留在 pre 表，
    doc §3.6）。tag_name 为冗余拷贝：val_fixed 从 tag_define.tag_name 拷贝，
    定义改名/合并时同步刷新全部挂载行（doc §6.3）；val_generalized（预留）
    存实际值。唯一键含 tag_name 正是为泛化值预留：同一 tag_id 可在同一资产
    挂多个实际值（多位导演）。
    """

    __tablename__ = "asset_tag"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset_id: Mapped[str] = mapped_column(
        Text, ForeignKey("media_asset.asset_id"), nullable=False
    )
    # 挂载粒度：content 本期启用；scene/character 结构预留（doc §2.2）
    target_type: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="content"
    )
    target_id: Mapped[str] = mapped_column(Text, nullable=False)
    tag_id: Mapped[str] = mapped_column(
        Text, ForeignKey("tag_define.tag_id"), nullable=False
    )
    tag_name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        CheckConstraint("target_type IN ('content', 'scene', 'character')"),
        UniqueConstraint(
            "asset_id", "target_type", "target_id", "tag_id", "tag_name",
            name="ux_asset_tag",
        ),
        Index("idx_asset_tag_asset", "asset_id"),
        Index("idx_asset_tag_tag", "tag_id"),
    )


class TagImportTask(Base):
    """标签导入任务：跟踪一次文件导入的处理状态与结果摘要。

    v1 import_batch 改语义名，结构不变（含 revoke/导出语义，doc §3.7）。
    """

    __tablename__ = "tag_import_task"

    task_id: Mapped[str] = mapped_column(Text, primary_key=True)
    file_name: Mapped[str] = mapped_column(Text, nullable=False)
    # 取值 processing|completed|failed|revoked
    status: Mapped[str] = mapped_column(Text, nullable=False)
    # JSON 字符串（处理结果摘要）
    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        CheckConstraint("status IN ('processing', 'completed', 'failed', 'revoked')"),
        Index("idx_import_task_status", "status"),
    )


class TagChangeLog(Base):
    """标签变更日志：审计 tag_define 的全部生命周期变更（行即日志口径下
    merge 动作也落此表，作为 metrics 的 merge 次数统计源，doc §1/§6.2）。

    契约中 tag_id 不加外键——日志需能指向已废弃/合并掉的标签。
    """

    __tablename__ = "tag_change_log"

    log_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tag_id: Mapped[str] = mapped_column(Text, nullable=False)
    # 取值 create|update|merge|deprecate|import|delete（delete 为 v1 生命周期
    # 已有动作，2026-11-06 补入枚举——v1 无 CHECK 属历史漏网的文档缺口修复）
    action: Mapped[str] = mapped_column(Text, nullable=False)
    before_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    after_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    operator: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "action IN ('create', 'update', 'merge', 'deprecate', 'import', 'delete')"
        ),
        Index("idx_change_log_tag", "tag_id"),
    )


class PromptTemplate(Base):
    """Prompt 模板：按 mode+version 版本化的外置提示词正文。

    is_active 为真的是该 mode 当前生效版本（生成流水线 Task 5/8 经
    get_active_prompt 读取）；v1 种子见下方播种机制（create_all 路径）与
    迁移 0002（alembic 生产路径），两路共用同一份种子常量。
    契约只列出语义列，主键采用平台通行的自增 id（与 asset_tag 同风格），
    (mode, version) 唯一性按契约以 UniqueConstraint 保证。
    """

    __tablename__ = "prompt_template"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 取值 closed|open（与 generation_task.mode 同义）
    mode: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    template_text: Mapped[str] = mapped_column(Text, nullable=False)
    # 0 = 历史版本，1 = 当前生效（Boolean 映射 SQLite 整型 0/1）
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="0"
    )
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("mode", "version"),
    )


# ---------- v1 种子模板（阶段 2 实施计划附录 A/B 逐字文案，契约冻结） ----------

# 文案一个字不改；迁移 0002 与下方 after_create 钩子共用本常量，避免
# 测试库（create_all）与生产库（alembic）两份拷贝漂移。
_PROMPT_V1_CLOSED_TEXT = "\n".join([
    "你是影视内容标签专家。以下是一部影视内容的结构化描述：",
    "",
    "{content}",
    "",
    "候选标签清单（格式：tag_id | 名称 | 分类 | 定义）：",
    "{candidates}",
    "",
    "任务：从候选清单中选出与内容真正相关的标签。对每个选中标签输出：",
    "- tag_id（必须与候选清单一致，不得编造）",
    "- tag_name（与候选一致）",
    "- relevance：标签与内容的相关程度，0 到 1",
    "- confidence：你对该判断的置信度，0 到 1",
    "- evidence：支持该标签的内容证据列表，每条含 scene_id、start_ms、end_ms、description",
    "",
    "只输出 JSON 数组，不要输出其他文字。未选中的标签不要输出。",
])

_PROMPT_V1_OPEN_TEXT = "\n".join([
    "你是影视内容标签专家。以下是一部影视内容的结构化描述：",
    "",
    "{content}",
    "",
    "任务：为该内容自由生成最贴切的语义标签（不受任何既有标签体系限制）。对每个标签输出：",
    "- tag_name：简洁的标签名（2-8 字）",
    "- tag_type：标签分类（如 genre/mood/theme/narrative/style/character 或自拟）",
    "- description：一句话语义定义",
    "- confidence：你对该标签的置信度，0 到 1",
    "- evidence：支持该标签的内容证据列表，每条含 scene_id、start_ms、end_ms、description",
    "",
    "只输出 JSON 数组，不要输出其他文字。输出 5 到 15 个标签。",
])

# 种子固定创作日期（确定性：同一种子在任何环境落库时间戳一致）
_PROMPT_V1_SEED_CREATED_AT = datetime(2026, 10, 31)

# 公开给迁移 0002 引用的种子行（只读；is_active=True 落库为整型 1）
PROMPT_V1_SEED = (
    {
        "mode": "closed",
        "version": 1,
        "template_text": _PROMPT_V1_CLOSED_TEXT,
        "is_active": True,
        "created_at": _PROMPT_V1_SEED_CREATED_AT,
    },
    {
        "mode": "open",
        "version": 1,
        "template_text": _PROMPT_V1_OPEN_TEXT,
        "is_active": True,
        "created_at": _PROMPT_V1_SEED_CREATED_AT,
    },
)

# open 模板自 2026-11 起不再被任何生成任务消费（open 改由
# generation/mtg 的内置流水线 mtg-v1 接管）。按"不删但标注过时"的口径：
# v1 行原样保留（历史可查、API 仍可读），另插一条 inactive 的 v2 作为
# 过时标记——提示词管理页能直接看到；同 mode 恰一个 active 的不变量不变。
_PROMPT_DEPRECATED_OPEN_TEXT = "\n".join([
    "【已过时】open 模式自 2026-11 起由内置流水线 mtg-v1 接管",
    "（多轮生成 + 语义合并 + judge 审核），本模板不再被任何生成任务消费。",
    "closed 模式仍使用自己的模板；本行仅作历史标注，不要激活。",
    "",
    "{content}",
])

PROMPT_DEPRECATED_OPEN_SEED = (
    {
        "mode": "open",
        "version": 2,
        "template_text": _PROMPT_DEPRECATED_OPEN_TEXT,
        "is_active": False,
        "created_at": datetime(2026, 11, 3),
    },
)


@event.listens_for(PromptTemplate.__table__, "after_create")
def _seed_prompt_v1(target, connection, **kw):
    """prompt_template 建表后自动播种 v1 激活模板与 open 过时标注行。

    仅 Base.metadata.create_all 路径触发（覆盖 conftest 的 session/client
    fixture）。alembic 迁移 0002/0005 经 op.create_table + bulk_insert 播种，
    不触发本事件——两条路径互斥，不会重复插入。
    """
    rows = [dict(row) for row in PROMPT_V1_SEED]
    rows += [dict(row) for row in PROMPT_DEPRECATED_OPEN_SEED]
    connection.execute(target.insert(), rows)


class UserAccount(Base):
    """用户账号：审核后台登录账户（阶段 3 第 10 张表）。

    role 取值 admin|reviewer（括号注记枚举以注释记录，不加 CHECK 约束，
    合法性由服务层保证）；username 唯一约束是登录与重名校验的依据；
    password_hash 存 `pbkdf2$200000$salt_hex$hash_hex`（见 auth/password）。
    user_id 由服务层生成（`user_`+uuid4().hex[:8]），created_at 显式赋值。
    """

    __tablename__ = "user_account"

    user_id: Mapped[str] = mapped_column(Text, primary_key=True)
    username: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    # 取值 admin|reviewer
    role: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

class AssetBuildTask(Base):
    """上传视频后自动生成资产的后台任务（前端轮询进度）。

    status 取值 queued|running|succeeded|failed；done_scenes/total_scenes 供
    前端显示「正在分析第 N/M 个场景」。asset_id 指向上传时登记的媒体资产，
    生成完成后由内容导入服务写入同一 asset_id 的 media_context。
    """

    __tablename__ = "asset_build_task"

    task_id: Mapped[str] = mapped_column(Text, primary_key=True)
    asset_id: Mapped[str] = mapped_column(
        Text, ForeignKey("media_asset.asset_id"), nullable=False, index=True
    )
    movie_id: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    total_scenes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    done_scenes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
