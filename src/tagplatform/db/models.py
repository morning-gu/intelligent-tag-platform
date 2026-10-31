"""9 张核心表 ORM 模型（数据底座的列契约）。

本模块的属性名即平台契约：后续任务（治理流水线、内容导入、API 层）一律
按这些属性读写数据库。列定义与设计文档逐字对应；括号注记的枚举取值
（如 movie|series|scene）以注释记录，不加 CHECK 约束（契约未要求，
合法性校验留待服务层）。
分层红线：本模块不得 import fastapi（设计文档 §9.4）。
"""

from datetime import datetime

from sqlalchemy import (
    REAL,
    Boolean,
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
    """媒体资产：movie/series/scene 三级结构，子级经 parent_id 挂到父级。"""

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
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class MediaContext(Base):
    """媒体上下文：资产的深度语义描述（content_json 正文 + 句向量）。"""

    __tablename__ = "media_context"

    context_id: Mapped[str] = mapped_column(Text, primary_key=True)
    asset_id: Mapped[str] = mapped_column(
        Text, ForeignKey("media_asset.asset_id"), nullable=False, index=True
    )
    content_json: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    source: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    recognition_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class TagCatalog(Base):
    """标签目录：平台级标签字典，父子层级 + 别名 + 适用范围。"""

    __tablename__ = "tag_catalog"

    tag_id: Mapped[str] = mapped_column(Text, primary_key=True)
    tag_name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    tag_type: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    parent_id: Mapped[str | None] = mapped_column(
        ForeignKey("tag_catalog.tag_id"), nullable=True
    )
    # JSON 数组字符串（如 '["悬念"]'），由服务层序列化
    aliases: Mapped[str | None] = mapped_column(Text, nullable=True)
    applicable_scope: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str | None] = mapped_column(
        Text, nullable=True, server_default="active"
    )
    embedding: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class GenerationTask(Base):
    """标签生成任务：closed（目录内封闭生成）| open（开放候选生成）。"""

    __tablename__ = "generation_task"

    task_id: Mapped[str] = mapped_column(Text, primary_key=True)
    context_id: Mapped[str | None] = mapped_column(
        ForeignKey("media_context.context_id"), nullable=True
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


class AssetTag(Base):
    """资产-标签结果行：目录挂载（origin=catalog，tag_id 必填）或开放生成
    候选（origin=generated，candidate_name 必填）。

    两个部分唯一索引保证重跑幂等：
    - ux_asset_tag_catalog：同一资产不重复挂同一目录标签；
    - ux_asset_tag_candidate：同一资产不重复收录同一候选名。
    approved 保留 / pending 覆盖的重跑语义由阶段 2 服务层实现，索引只保证不重复。
    """

    __tablename__ = "asset_tag"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset_id: Mapped[str] = mapped_column(
        Text, ForeignKey("media_asset.asset_id"), nullable=False
    )
    tag_id: Mapped[str | None] = mapped_column(
        ForeignKey("tag_catalog.tag_id"), nullable=True
    )
    candidate_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    candidate_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    candidate_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 取值 catalog|generated
    origin: Mapped[str] = mapped_column(Text, nullable=False)
    relevance_score: Mapped[float | None] = mapped_column(REAL, nullable=True)
    confidence: Mapped[float | None] = mapped_column(REAL, nullable=True)
    evidence_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    review_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="pending"
    )
    task_id: Mapped[str | None] = mapped_column(
        ForeignKey("generation_task.task_id"), nullable=True
    )
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        Index(
            "ux_asset_tag_catalog",
            "asset_id",
            "tag_id",
            unique=True,  # 唯一性是 brief 的语义要求（部分"唯一"索引）；缺失会静默不查重
            sqlite_where=text("tag_id IS NOT NULL"),
        ),
        Index(
            "ux_asset_tag_candidate",
            "asset_id",
            "candidate_name",
            unique=True,
            sqlite_where=text("tag_id IS NULL AND candidate_name IS NOT NULL"),
        ),
    )


class ReviewRecord(Base):
    """人工审核记录：对 asset_tag 结果行的 approve/reject/modify/merge 动作。"""

    __tablename__ = "review_record"

    record_id: Mapped[str] = mapped_column(Text, primary_key=True)
    result_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("asset_tag.id"), nullable=False
    )
    # 取值 approve|reject|modify|merge
    action: Mapped[str] = mapped_column(Text, nullable=False)
    reviewer_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    before_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    after_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ImportBatch(Base):
    """导入批次：跟踪一次文件导入的处理状态与结果摘要。"""

    __tablename__ = "import_batch"

    batch_id: Mapped[str] = mapped_column(Text, primary_key=True)
    file_name: Mapped[str] = mapped_column(Text, nullable=False)
    # 取值 processing|completed|failed
    status: Mapped[str] = mapped_column(Text, nullable=False)
    # JSON 字符串（处理结果摘要）
    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class TagChangeLog(Base):
    """标签变更日志：审计 tag_catalog 的全部生命周期动作。

    契约中 tag_id 不加外键——日志需能指向已废弃/合并掉的标签。
    """

    __tablename__ = "tag_change_log"

    log_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tag_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    # 取值 create|update|merge|deprecate|import
    action: Mapped[str] = mapped_column(Text, nullable=False)
    before_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    after_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    operator: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


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


@event.listens_for(PromptTemplate.__table__, "after_create")
def _seed_prompt_v1(target, connection, **kw):
    """prompt_template 建表后自动播种 v1 激活模板（仅 Base.metadata.create_all
    路径触发，覆盖 conftest 的 session/client fixture）。

    alembic 迁移 0002 经 op.create_table 建表，不触发本事件，由迁移自带的
    bulk_insert 播种——两条路径互斥，不会重复插入。
    """
    connection.execute(target.insert(), [dict(row) for row in PROMPT_V1_SEED])
