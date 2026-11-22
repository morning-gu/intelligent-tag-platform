"""`/api/v1/config` 薄路由：只读运行配置（仅 admin）。

接入层只做协议转换：require_admin 门槛 + Settings 注入 → 调编排 → 返回。
Settings 只读视图拼装在 tagplatform.orchestration.config（直读 Settings，
无 services/config——2026-11-21 精简审计砍除）。
"""

from fastapi import APIRouter, Depends

from tagplatform.api import get_settings_dep
from tagplatform.api.depends import require_admin
from tagplatform.capabilities.storage.models import UserAccount
from tagplatform.orchestration import config as config_orch
from tagplatform.orchestration.config import ConfigOut
from tagplatform.settings import Settings

router = APIRouter(prefix="/api/v1")


@router.get("/config", response_model=ConfigOut)
def get_config(
    _admin: UserAccount = Depends(require_admin),
    settings: Settings = Depends(get_settings_dep),
) -> ConfigOut:
    """只读运行配置（仅 admin）：字段与设计契约一一对应，不含任何机密。"""
    return config_orch.get_config(settings)
