"""配置层：全局 Settings 与缓存读取。

分层红线：本模块不得 import fastapi（设计文档 §9.4）。
环境变量前缀 `TAGPLT_`（如 `TAGPLT_DATABASE_PATH`）。
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """平台全局配置。

    后续任务统一经 `get_settings()` 取配置；测试可直接构造
    `Settings(database_path=":memory:")` 覆盖默认值。
    """

    model_config = SettingsConfigDict(env_prefix="TAGPLT_")

    # SQLite 数据库文件路径
    database_path: str = "data/tagplatform.db"
    # 句向量模型（intfloat/multilingual-e5-base，768 维）
    embedding_model: str = "intfloat/multilingual-e5-base"
    # True 时用确定性 MockEmbedder（测试专用，绝不触发模型下载）
    use_mock_embedder: bool = False
    # 语义相似阈值，余弦相似度 >= 该值判为疑似同义（含等于）
    semantic_similar_threshold: float = 0.90
    # 标签类型逗号分隔白名单；空 = 不校验
    allowed_tag_types: str = ""


@lru_cache
def get_settings() -> Settings:
    """返回全局单例 Settings（lru_cache 缓存，进程内只构造一次）。"""
    return Settings()
