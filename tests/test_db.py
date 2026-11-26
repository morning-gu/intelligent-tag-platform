"""数据库基础层测试（Task 2）。

覆盖契约：`create_engine_for(path)` 每连接强制 WAL + foreign_keys=ON、
文件库自动确保父目录存在。
"""

from tagplatform.capabilities.storage import create_engine_for


def test_engine_wal_and_fk(tmp_path):
    engine = create_engine_for(str(tmp_path / "t.db"))
    with engine.connect() as c:
        assert c.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
        assert c.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1


def test_engine_ensures_parent_dir(tmp_path):
    """文件库父目录不存在时应自动创建（覆盖默认 data/ 目录场景），且连接真正可用。"""
    db_path = tmp_path / "nested" / "t.db"
    engine = create_engine_for(str(db_path))
    assert db_path.parent.is_dir()
    with engine.connect() as c:
        assert c.exec_driver_sql("SELECT 1").scalar() == 1
