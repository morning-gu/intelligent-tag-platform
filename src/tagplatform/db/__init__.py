"""数据库基础层：SQLite 引擎与会话工厂。

分层红线：本包不得 import fastapi（设计文档 §9.4）。
`create_engine_for` 产出的引擎在每个新连接上强制 WAL 日志模式与
foreign_keys=ON（SQLite 外键默认关闭，必须逐连接开启）。
"""

from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker


def create_engine_for(path: str) -> Engine:
    """为 SQLite 数据库创建引擎（`:memory:` 或文件路径）。

    文件库会自动确保父目录存在（如默认的 `data/`）；每个新连接执行
    `PRAGMA journal_mode=WAL` 与 `PRAGMA foreign_keys=ON`。
    """
    if path != ":memory:":
        # SQLite 不会自建多级目录，缺父目录会报 unable to open database file
        Path(path).parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(f"sqlite:///{path}")

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record):
        """每个新连接强制 WAL 与外键约束（这两个 PRAGMA 均逐连接生效）。"""
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    """创建绑定 engine 的会话工厂。

    `expire_on_commit=False`：提交后对象属性仍可读，避免会话关闭后
    访问已提交对象触发 DetachedInstanceError。
    """
    return sessionmaker(bind=engine, expire_on_commit=False)
