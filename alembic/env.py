"""Alembic 迁移环境（Task 2 骨架）。

约定：
- 数据库 URL 不硬编码于 alembic.ini，运行时从
  `tagplatform.settings.get_settings().database_path` 动态构造，
  支持 `TAGPLT_DATABASE_PATH` 环境变量覆盖（供迁移测试使用）。
- 控制器 ruling：`tagplatform.db.models` 要到 Task 3 才存在，
  对其的 import 一律放在函数体内延迟执行，本模块被加载时不得 ImportError。
"""

from logging.config import fileConfig

from alembic import context

from tagplatform.settings import get_settings

config = context.config

# 应用 alembic.ini 中的日志配置
if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _database_path() -> str:
    """从全局 Settings 读取数据库路径。"""
    return get_settings().database_path


def run_migrations_offline() -> None:
    """离线模式：不连库，仅生成 SQL 脚本。"""
    # 延迟 import（控制器 ruling）：models.py 在 Task 3 才创建
    from tagplatform.db.models import Base

    context.configure(
        url=f"sqlite:///{_database_path()}",
        target_metadata=Base.metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,  # SQLite 不支持多数 ALTER，用批量重建表
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：连接数据库执行迁移。"""
    # 延迟 import（控制器 ruling）：models.py 在 Task 3 才创建
    from tagplatform.db import create_engine_for
    from tagplatform.db.models import Base

    # 复用基础层引擎：自动确保父目录存在，并逐连接强制 WAL/foreign_keys
    connectable = create_engine_for(_database_path())
    with connectable.connect() as connection:
        # SQLite 批重建（batch_alter_table）要 drop 再改名父表；外键开着时
        # 带子行的父表删不掉。PRAGMA 在事务里是空操作，必须在 SQLAlchemy
        # 隐式 BEGIN 之前、用原始 DBAPI 连接关掉外键检查。
        raw = connection.connection.dbapi_connection
        cursor = raw.cursor()
        cursor.execute("PRAGMA foreign_keys=OFF")
        cursor.close()
        context.configure(
            connection=connection,
            target_metadata=Base.metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
