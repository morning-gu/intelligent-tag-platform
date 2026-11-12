"""核心表 ORM 模型（列契约）。

标签库分为 `tag_catalog`(目录) + `tag_define`(标签)（v2 迁移 0007 重建），
导入批次为 `tag_import_task`（v1 的 `import_batch` 已退役）。本模块的属性名即
平台契约：后续任务（治理流水线、内容导入、API 层）一律按这些属性读写数据库。
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
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """全平台 ORM 基类；metadata 供 alembic autogenerate 使用。"""


class MediaAsset(Base):
    """媒体资产：movie/series/scene 三级结构，子级经 parent_id 挂到父级。

    一条影片资产只有一行，内容（标准资产 JSON）就放在 `content_json`：
    重新生成时整块覆盖，全库只有这一份内容（v1 的 `media_context` 已并入本表，
    doc §3.1）。
    场景行是物理切片，不存内容（`content_json`/`source` 恒为 NULL）。
    """

    __tablename__ = "media_asset"
    # 与设计文档 §3.1 对齐：asset_type 枚举约束 + parent/type 两个索引
    __table_args__ = (
        CheckConstraint(
            "asset_type IN ('movie', 'series', 'scene')",
            name="ck_media_asset_asset_type",
        ),
        Index("idx_media_asset_parent", "parent_id"),
        Index("idx_media_asset_type", "asset_type"),
    )

    asset_id: Mapped[str] = mapped_column(Text, primary_key=True)
    # 取值 movie|series|scene（CHECK 约束见上）
    asset_type: Mapped[str] = mapped_column(Text, nullable=False)
    parent_id: Mapped[str | None] = mapped_column(
        ForeignKey("media_asset.asset_id"), nullable=True
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    video_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 标准资产 JSON（结构见 new_json.txt 模板）；未生成为 NULL。
    # SQLite 无 JSONB，用 TEXT 存 JSON 文本；迁 PostgreSQL 时改 JSONB。
    content_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 内容口径：asset_builder:v5-infer（大模型补字段）/
    # asset_builder:v5-facts（只填数据集事实）——与 main 分支编码一致，
    # 双方产出的内容能互相识别「同口径可复用」
    source: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, server_default=func.now()
    )
    # 最近一次写入内容的时间（没内容时为上传时间）
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class TagCatalog(Base):
    """标签目录（v2）：目录树节点，标签定义见 `TagDefine`。

    与 main 分支的 v2 schema 逐字对齐（v2 重建时从 v1 混合表切换过来）：
    `dir_type` 区分静态/动态目录，`applicable_scope` 声明该目录可挂的粒度
    （content/scene/character 的逗号组合），`parent_id` 自引用构成目录树。
    标签本体在 `tag_define`，`pre_asset_tag.candidate_dir_id` 指这里的 dir_id。
    """

    __tablename__ = "tag_catalog"

    __table_args__ = (
        CheckConstraint(
            "dir_type IN ('dir_static', 'dir_dynamic')",
            name="ck_tag_catalog_dir_type",
        ),
        CheckConstraint(
            "applicable_scope IN ('content', 'scene', 'character', 'content,scene', "
            "'content,character', 'scene,character', 'content,scene,character')",
            name="ck_tag_catalog_applicable_scope",
        ),
        UniqueConstraint("parent_id", "dir_name", name="ux_tag_catalog_parent_name"),
        Index("idx_tag_catalog_type", "dir_type"),
        Index("idx_tag_catalog_scope", "applicable_scope"),
    )

    dir_id: Mapped[str] = mapped_column(Text, primary_key=True)
    dir_name: Mapped[str] = mapped_column(Text, nullable=False)
    # 取值 dir_static|dir_dynamic
    dir_type: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    parent_id: Mapped[str | None] = mapped_column(
        ForeignKey("tag_catalog.dir_id"), nullable=True
    )
    # JSON 数组字符串，由服务层序列化
    aliases: Mapped[str | None] = mapped_column(Text, nullable=True)
    applicable_scope: Mapped[str | None] = mapped_column(
        Text, nullable=True, server_default="content,scene,character"
    )
    embedding: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class TagDefine(Base):
    """标签定义（v2）：挂在目录下的标签本体，`pre_asset_tag.tag_id` 指这里。

    `tag_type` 区分 val_fixed（固定值标签）/ val_generalized（预留：同标签可挂
    多个实际值，如"执行导演→张三"）；`status` 支持 active/deprecated 软下架；
    (dir_id, tag_name) 唯一，与 main 的 `ux_tag_define_dir_name` 对齐。
    """

    __tablename__ = "tag_define"

    __table_args__ = (
        CheckConstraint(
            "tag_type IN ('val_fixed', 'val_generalized')",
            name="ck_tag_define_tag_type",
        ),
        CheckConstraint(
            "status IN ('active', 'deprecated')", name="ck_tag_define_status"
        ),
        UniqueConstraint("dir_id", "tag_name", name="ux_tag_define_dir_name"),
        Index("idx_tag_define_dir", "dir_id"),
        Index("idx_tag_define_status", "status"),
    )

    tag_id: Mapped[str] = mapped_column(Text, primary_key=True)
    tag_name: Mapped[str] = mapped_column(Text, nullable=False)
    # 取值 val_fixed|val_generalized
    tag_type: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    dir_id: Mapped[str] = mapped_column(
        Text, ForeignKey("tag_catalog.dir_id"), nullable=False
    )
    aliases: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str | None] = mapped_column(
        Text, nullable=True, server_default="active"
    )
    embedding: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class TagGenerationTask(Base):
    """标签生成任务：closed（目录内封闭生成）| open（开放候选生成）。

    v2 起任务直接绑资产（`asset_id`），不设 context 中转（v1 的
    `media_context` 已并入 `media_asset.content_json`）：任务与资产一对多
    （每次生成一条）。
    """

    __tablename__ = "tag_generation_task"
    __table_args__ = (
        CheckConstraint("mode IN ('closed', 'open')", name="ck_tag_gen_task_mode"),
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')",
            name="ck_tag_gen_task_status",
        ),
        Index("idx_gen_task_asset", "asset_id"),
        Index("idx_gen_task_status", "status"),
    )

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


class PreAssetTag(Base):
    """待审核标签：生成结果 + 审核队列（asset×tag 去重，每资产每标签一行）。

    - `origin` 记录产出来源（closed/open），不可变；
    - `tag_id` 有值 = 命中库内标签（closed 全量；open 的重复项合入时赋值）；
    - `tag_id` 为空 + `candidate_name` 有值 = 库外新词，审核通过后由合入流程
      补 `tag_id` / `candidate_dir_id`；
    - `target_type/target_id` 显式挂载粒度，本期只写 content（= asset_id），
      scene/character 为结构预留（取 `content_json` 里的 scene_id/character_id）。

    v2 的 `tag_id` 外键目标是 `tag_define`、`candidate_dir_id` 指向
    `tag_catalog`(目录表)；标签库已从 v1 的 `tag_catalog` 单表拆成
    `tag_catalog` + `tag_define`（v2 重建）。
    """

    __tablename__ = "pre_asset_tag"
    __table_args__ = (
        CheckConstraint(
            "target_type IN ('content', 'scene', 'character')",
            name="ck_pre_tag_target_type",
        ),
        CheckConstraint("origin IN ('closed', 'open')", name="ck_pre_tag_origin"),
        CheckConstraint(
            "review_status IN ('pending', 'approved', 'rejected')",
            name="ck_pre_tag_review_status",
        ),
        Index(
            "ux_pre_tag_catalog",
            "asset_id",
            "target_type",
            "target_id",
            "tag_id",
            unique=True,
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

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset_id: Mapped[str] = mapped_column(
        Text, ForeignKey("media_asset.asset_id"), nullable=False
    )
    # 挂载粒度：content（本期启用）| scene | character（结构预留）
    target_type: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="content"
    )
    # content 级 = asset_id；场景/角色级 = content_json 里的 scene_id / character_id
    target_id: Mapped[str] = mapped_column(Text, nullable=False)
    # 命中库内标签时赋值；库外新词为空，合入标签库时补
    tag_id: Mapped[str | None] = mapped_column(
        ForeignKey("tag_define.tag_id"), nullable=True
    )
    candidate_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 人工最终采纳的归属目录（指向 v2 目录表；生成时可空，审核 modify 可改）
    candidate_dir_id: Mapped[str | None] = mapped_column(
        ForeignKey("tag_catalog.dir_id"), nullable=True
    )
    candidate_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 系统自动并入时模型给出的原词：将来写入 tag_define.aliases
    candidate_alias: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 产出来源：closed | open（不可变）
    origin: Mapped[str] = mapped_column(Text, nullable=False)
    relevance_score: Mapped[float | None] = mapped_column(REAL, nullable=True)
    confidence: Mapped[float | None] = mapped_column(REAL, nullable=True)
    evidence_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    task_id: Mapped[str | None] = mapped_column(
        ForeignKey("tag_generation_task.task_id"), nullable=True
    )
    review_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="pending"
    )
    # 审核人 = 会话用户名；备注 = 审核（尤其修改）时填写的说明
    reviewer_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    review_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)



class AssetTag(Base):
    """资产-标签正式挂载行（v2）：审核通过的标签复制到这里。

    只保留挂载关系与展示值（`tag_name` 冗余拷贝），评分/证据留在
    `pre_asset_tag`；`ux_asset_tag` 含 `tag_name` 是为泛化值预留——同一
    tag_id 可在同一资产挂多个实际值（如多位导演）。与 main 分支逐字对齐，
    v2 重建（v1 的 15 列旧表已废弃）。
    """

    __tablename__ = "asset_tag"
    __table_args__ = (
        CheckConstraint(
            "target_type IN ('content', 'scene', 'character')",
            name="ck_asset_tag_target_type",
        ),
        UniqueConstraint(
            "asset_id",
            "target_type",
            "target_id",
            "tag_id",
            "tag_name",
            name="ux_asset_tag",
        ),
        Index("idx_asset_tag_asset", "asset_id"),
        Index("idx_asset_tag_tag", "tag_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset_id: Mapped[str] = mapped_column(
        Text, ForeignKey("media_asset.asset_id"), nullable=False
    )
    # 挂载粒度：content（本期启用）| scene | character（结构预留）
    target_type: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="content"
    )
    target_id: Mapped[str] = mapped_column(Text, nullable=False)
    tag_id: Mapped[str] = mapped_column(
        Text, ForeignKey("tag_define.tag_id"), nullable=False
    )
    # 展示值：val_fixed 拷贝 tag_define.tag_name，定义改名/合并时同步刷新
    tag_name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class TagImportTask(Base):
    """标签导入任务（v2）：一次标签文件导入的处理状态与结果摘要。

    与 main 分支对齐：v1 的 `import_batch` 改语义名，列结构不变
    （`batch_id` → `task_id`），保留 revoke/导出语义。
    """

    __tablename__ = "tag_import_task"

    __table_args__ = (
        CheckConstraint(
            "status IN ('processing', 'completed', 'failed', 'revoked')",
            name="ck_tag_import_task_status",
        ),
        Index("idx_import_task_status", "status"),
    )

    task_id: Mapped[str] = mapped_column(Text, primary_key=True)
    file_name: Mapped[str] = mapped_column(Text, nullable=False)
    # 取值 processing|completed|failed|revoked
    status: Mapped[str] = mapped_column(Text, nullable=False)
    # JSON 字符串（处理结果摘要）
    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class TagChangeLog(Base):
    """标签变更日志：审计标签库（目录/定义）的全部生命周期动作。

    契约中 tag_id 不加外键——日志需能指向已废弃/合并掉的标签。
    """

    __tablename__ = "tag_change_log"

    __table_args__ = (
        CheckConstraint(
            "action IN ('create', 'update', 'merge', 'deprecate', 'import', 'delete')",
            name="ck_tag_change_log_action",
        ),
        Index("idx_change_log_tag", "tag_id"),
    )

    log_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tag_id: Mapped[str] = mapped_column(Text, nullable=False)
    # 取值 create|update|merge|deprecate|import|delete
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
    生成完成后内容写进同一条资产的 content_json（v1 的 media_context 已并入）。
    任务可以多条（每次上传/重新生成一条），资产只有一条。
    """

    __tablename__ = "asset_build_task"
    # status 枚举约束 + asset/status 两个命名索引（0004 的旧索引已在 0009 换名）
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')",
            name="ck_asset_build_task_status",
        ),
        Index("idx_build_task_asset", "asset_id"),
        Index("idx_build_task_status", "status"),
    )

    task_id: Mapped[str] = mapped_column(Text, primary_key=True)
    asset_id: Mapped[str] = mapped_column(
        Text, ForeignKey("media_asset.asset_id"), nullable=False
    )
    movie_id: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    total_scenes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    done_scenes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
