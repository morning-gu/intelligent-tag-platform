"""10 张核心表 ORM 模型（数据底座的列契约）。

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
    """媒体资产：movie/series/scene 三级结构，子级经 parent_id 挂到父级。

    一条影片资产只有一行，内容（标准资产 JSON）就放在 `content_json`：
    重新生成时整块覆盖，不再像旧 `media_context` 那样每次导入堆一行。
    场景行是物理切片，不存内容（`content_json`/`source` 恒为 NULL）。
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
    # 标准资产 JSON（结构见 new_json.txt 模板）；未生成为 NULL
    content_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 内容口径：v5-infer（大模型补字段）/ v5-facts（只填数据集事实）
    source: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 最近一次写入内容的时间（没内容时为上传时间）
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


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
    tag_name: Mapped[str] = mapped_column(Text, nullable=False)
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

    __table_args__ = (
        UniqueConstraint("parent_id", "tag_name", name="ux_tag_catalog_parent_name"),
    )


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
    # 标签层级：film（影片级，全片共有特征）/ scene（场景级，个别场景特征）。
    # 生成阶段由 MTG 的 scope 决定；闭环与目录标签恒为 film。
    level: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="film"
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
    # 取值 processing|completed|failed|revoked
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
    生成完成后内容写进同一条资产的 content_json（media_context 过渡期双写）。
    任务可以多条（每次上传/重新生成一条），资产只有一条。
    """

    __tablename__ = "asset_build_task"

    task_id: Mapped[str] = mapped_column(Text, primary_key=True)
    asset_id: Mapped[str] = mapped_column(
        Text, ForeignKey("media_asset.asset_id"), nullable=False, index=True
    )
    movie_id: Mapped[str] = mapped_column(Text, nullable=False)
    # 是否用大模型补充资产字段：True=多模态推断（描述/氛围/事件/说话人），
    # False=只填数据集已有的事实（不调模型）。上传时由前端勾选，建任务落库；
    # 同时决定写进资产的内容口径（v5-infer / v5-facts）。
    infer: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="1"
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    total_scenes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    done_scenes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
