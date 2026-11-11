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
    # 句向量模型（1024 维）。2026-11-05 由 e5-base 换到 BGE-M3：
    # 同一批 87 条人工标注上 AUC 0.903→0.928，且"重复 vs 新词"的分差
    # 0.028→0.187（e5 把所有中文短词挤在 0.85-0.95，阈值只能在噪声里切）。
    # 换模型必须重灌 tag_catalog.embedding（scripts/reembed_catalog.py）。
    embedding_model: str = "BAAI/bge-m3"
    # 导入校验：与库内标签余弦 ≥ 该值判"疑似重复"（含等于）。
    # e5 时代是 0.90；换 BGE-M3 后 0.90 只对几乎逐字相同的对子触发，下调到 0.85。
    semantic_similar_threshold: float = 0.85
    # True 时用确定性 MockEmbedder（测试专用，绝不触发模型下载）
    use_mock_embedder: bool = False
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

    # ---------- 日志 ----------

    # 根日志级别（TAGPLT_LOG_LEVEL 可覆盖）。生成流水线与过滤责任链的
    # 逐环节日志走 INFO；排查"标签怎么被丢的"时设 DEBUG 可看到传入明细
    log_level: str = "INFO"

    # ---------- 阶段 5：上传即生成资产（2026-11） ----------

    # 数据集根目录（MovieNet）；上传文件名命中后从这里取标注与关键帧
    dataset_root: str = "/cache/MovieNet"
    # 已备好的"影片资产目录"根：每部片一个子目录，内含 content.json 与
    # frames/（scene_XXX_frame_N.jpg）。这是"直接注册资产"入口的来源，
    # 与上传视频那条路并行存在（TAGPLT_MOVIE_ASSET_ROOT 可覆盖）。
    # 说明：目录布局按 2026-11 的现有布局写死，改布局只需改这里。
    movie_asset_root: str = "/cache/h50065879/movie_asset/movie_asset"
    # 目录导入的额外帧根（逗号分隔）：配置根与它的父目录之外，再补几个位置。
    # 生成任务找关键帧时会按 asset_id 在这些根下面自动反查目录。
    movie_asset_extra_roots: str = ""
    # 每场景抽几张关键帧喂多模态模型
    asset_frames: int = 3
    # 场景级并发（生成资产的模型调用）
    asset_workers: int = 4
    # 每场景最多带几行字幕（进资产 JSON；标签生成会全量使用这些台词）
    asset_dialogue_limit: int = 8
    # 是否让模型推断数据集没有的字段（中文描述/氛围/事件/说话人/剧情时间地点）。
    # False = 只填数据集事实，一次模型调用都不发。
    asset_infer: bool = True

    # ---------- 阶段 6：MovieTagGen 移植的开放式生成（2026-11） ----------
    # 生成轮数（跨轮并集，1 = 单轮）；轮数越高覆盖越好、成本线性上涨
    mtg_runs: int = 3
    # 每个场景取几张关键帧 / 全片最多几张（超出按场景轮转截断）
    mtg_frames_per_scene: int = 2
    mtg_max_frames: int = 24
    # 跨轮语义合并阈值（complete-link）。换 BGE-M3 后按"e5 0.90 会合掉的对子"
    # 反推：同一批候选里 e5≥0.90 有 31 对，BGE-M3 上对应 0.75（13 对）——
    # 刻意偏保守，宁可留两条相似候选让人看，也不要把不同的词合掉。
    mtg_merge_threshold: float = 0.75
    # 单轮输出上限（模型被要求 15-20 个，这里兜底截断）
    mtg_max_candidates_per_run: int = 20
    # judge 通过后的最终产出上限（用户口径 10-15 条）
    mtg_max_final_tags: int = 15
    # judge：模型与开关（关掉则只做合并，不过滤）
    mtg_judge_model: str = "deepseek-v4.1-flash"
    mtg_judge_enabled: bool = True
    mtg_judge_temperature: float = 0.0
    # 审计报告落盘目录（每任务一份 JSON：候选、判决、用量、耗时）
    mtg_report_dir: str = "data/audit/mtg"

    # ---------- 阶段 7：用标签库提升开放生成质量（2026-11） ----------
    # 总开关：关掉 = 回到"不看库"的旧行为（提示不注入、分档不做、排序不含分档）
    open_library_enabled: bool = True
    # 生成前注入的库内标签个数（只给名字）
    open_library_hint_enabled: bool = True
    open_library_hint_count: int = 30
    # 分档阈值：余弦 ≥ 该值判"疑似重复"。
    # 0.87 ≈ 候选×库随机配对的 99.9% 分位（实测 0.866），不是拍脑袋的数。
    open_library_suspect_cosine: float = 0.74
    # 名字字符重合度（Jaccard）≥ 该值判"疑似重复"
    open_library_name_overlap: float = 0.40
    # 疑似对子交给 LLM 复核"是不是同一个概念"（一批一次调用）；
    # 复核通过才把候选替换成库内标准名，失败/关掉则不替换
    open_library_verify_enabled: bool = True
    # 复核调用的输出上限：推理模型在模棱两可的对子上会把思考写爆
    # （实测一次 27k tokens，任务从 4 分钟拖到 7 分钟），这里硬性兜底
    open_library_verify_max_tokens: int = 8192
    # 替换总开关：关掉则只标记不替换（回到"只标记"口径）
    open_library_replace_enabled: bool = True
    # 提示词里最多列多少个可扩展目录（按全路径排序后截断，控制 token）。
    # 0 = 不注入目录，回到"生成后再按向量归类"的老路径。
    open_dir_hint_max: int = 60


@lru_cache
def get_settings() -> Settings:
    """返回全局单例 Settings（lru_cache 缓存，进程内只构造一次）。"""
    return Settings()
