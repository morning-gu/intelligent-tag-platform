"""只读运行配置编排（GET /api/v1/config）——orchestration/config 域。

用例清单：`get_config`——Settings 只读视图，供运营侧核对部署参数（阶段 4
Task 2）。无 services/config（2026-11-21 精简审计砍除：prompt 移能力层后
仅剩"读冻结的 Settings 返回视图"，无状态/无事务/无写，不构成服务），
编排直读 Settings 拼装。

response_model 字段白名单保证机密不外泄——session_secret / admin_password
等敏感项不在 ConfigOut 中，即便未来 Settings 增列也不会自动进入响应。
只读语义：不提供任何写配置端点（改配置走环境变量 + 重启，见 README）。
"""

from pydantic import BaseModel

from tagplatform.settings import Settings


class ConfigOut(BaseModel):
    """运行配置只读视图：机密字段（session_secret / admin_password）绝不入列。

    模型字段覆盖后端实际使用的各端点：生成 LLM、开放流水线 judge、
    嵌入后端/模型、开放评测评审模型（空串 = 回落主 LLM，前端按此渲染）。
    均为模型名/后端名/本地路径，非机密；API key 与 base URL 不入列。
    """

    llm_model: str
    llm_temperature: float
    mtg_judge_model: str
    mtg_judge_enabled: bool
    embedding_backend: str
    embedding_model: str
    open_eval_review_model: str
    recall_top_k_per_type: int
    open_library_suspect_cosine: float
    open_library_name_overlap: float
    generation_workers: int
    use_mock_llm: bool
    use_mock_embedder: bool
    database_path: str


def get_config(settings: Settings) -> ConfigOut:
    """只读运行配置：字段与设计契约一一对应，不含任何机密。"""
    effective_embedding_model = (
        settings.embedding_api_model
        if settings.embedding_backend == "remote"
        else settings.embedding_model
    )
    return ConfigOut(
        llm_model=settings.llm_model,
        llm_temperature=settings.llm_temperature,
        mtg_judge_model=settings.mtg_judge_model,
        mtg_judge_enabled=settings.mtg_judge_enabled,
        embedding_backend=settings.embedding_backend,
        embedding_model=effective_embedding_model,
        open_eval_review_model=settings.open_eval_review_model,
        recall_top_k_per_type=settings.recall_top_k_per_type,
        open_library_suspect_cosine=settings.open_library_suspect_cosine,
        open_library_name_overlap=settings.open_library_name_overlap,
        generation_workers=settings.generation_workers,
        use_mock_llm=settings.use_mock_llm,
        use_mock_embedder=settings.use_mock_embedder,
        database_path=settings.database_path,
    )
