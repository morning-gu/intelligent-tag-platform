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

    # ---------- 阶段 2：智能标签生产（LLM 客户端与生成流水线） ----------

    # LLM 模型名（OpenAI 兼容端点）
    llm_model: str = "qwen3.7-plus"
    # Base URL / API Key 的环境变量名（.env 提供，make_llm_client 构建 transport 时读取）
    llm_base_url_env: str = "OPENAI_BASE_URL"
    llm_api_key_env: str = "OPENAI_API_KEY"
    # 采样温度
    llm_temperature: float = 0.7
    # 相邻 LLM 请求最小间隔秒数（限速）
    llm_request_interval: float = 0.5
    # True 时用确定性 MockLLMClient（测试专用，零网络）
    use_mock_llm: bool = False
    # 模式 A 召回：每 tag_type 召回上限
    recall_top_k_per_type: int = 50
    # 模式 B 归一匹配阈值：与库内标签相似度 >= 该值（含等于）归入 catalog
    open_match_threshold: float = 0.85
    # 生成任务线程池大小；0 = 同步执行（测试与单步调试）
    generation_workers: int = 2

    # ---------- 阶段 3：审核后台（会话认证） ----------

    # 会话令牌 HMAC 密钥（TAGPLT_SESSION_SECRET）；默认值仅供开发/测试，
    # 生产必改（泄漏即可伪造任意用户登录态，README 环境变量表警告）
    session_secret: str = "dev-secret"

    # ---------- 阶段 4：运营收尾（上传占位） ----------

    # 上传视频落盘根目录（TAGPLT_MEDIA_ROOT 可覆盖）；上传时按需创建目录
    media_root: str = "data/media/"

    # ---------- 阶段 5：上传即生成资产（2026-11） ----------

    # 数据集根目录（MovieNet）；上传文件名命中后从这里取标注与关键帧
    dataset_root: str = "/cache/MovieNet"
    # 每场景抽几张关键帧喂多模态模型
    asset_frames: int = 3
    # 场景级并发（生成资产的模型调用）
    asset_workers: int = 4
    # 每场景最多带几行字幕（台词进资产，但全量会让 JSON 涨 8 倍）
    asset_dialogue_limit: int = 5
    # 是否让模型推断数据集没有的字段（中文描述/氛围/事件/说话人/剧情时间地点）。
    # False = 只填数据集事实，一次模型调用都不发。
    asset_infer: bool = True


@lru_cache
def get_settings() -> Settings:
    """返回全局单例 Settings（lru_cache 缓存，进程内只构造一次）。"""
    return Settings()
