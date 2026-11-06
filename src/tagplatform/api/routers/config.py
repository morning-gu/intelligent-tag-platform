"""`/api/v1/config` 路由：只读运行配置（仅 admin）。

薄路由层：Settings 只读视图，供运营侧核对部署参数（阶段 4 Task 2）。
response_model 字段白名单保证机密不外泄——session_secret / admin_password
等敏感项不在 ConfigOut 中，即便未来 Settings 增列也不会自动进入响应。
只读语义：不提供任何写配置端点（改配置走环境变量 + 重启，见 README）。
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from tagplatform.api import get_settings_dep
from tagplatform.api.depends import require_admin
from tagplatform.db.models import UserAccount
from tagplatform.settings import Settings

router = APIRouter(prefix="/api/v1")


class ConfigOut(BaseModel):
    """运行配置只读视图：机密字段（session_secret / admin_password）绝不入列。"""

    llm_model: str
    llm_temperature: float
    recall_top_k_per_type: int
    open_match_threshold: float
    generation_workers: int
    use_mock_llm: bool
    use_mock_embedder: bool
    database_path: str


@router.get("/config", response_model=ConfigOut)
def get_config(
    _admin: UserAccount = Depends(require_admin),
    settings: Settings = Depends(get_settings_dep),
) -> ConfigOut:
    """只读运行配置（仅 admin）：字段与设计契约一一对应，不含任何机密。"""
    return ConfigOut(
        llm_model=settings.llm_model,
        llm_temperature=settings.llm_temperature,
        recall_top_k_per_type=settings.recall_top_k_per_type,
        open_match_threshold=settings.open_match_threshold,
        generation_workers=settings.generation_workers,
        use_mock_llm=settings.use_mock_llm,
        use_mock_embedder=settings.use_mock_embedder,
        database_path=settings.database_path,
    )
