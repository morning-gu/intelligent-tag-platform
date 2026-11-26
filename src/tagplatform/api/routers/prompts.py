"""`/api/v1/prompts` 路由：Prompt 模板只读契约视图。

四层重构 Task 3 prompt 本地化：文案是代码常量
（tagplatform.capabilities.prompts.templates，git 即版本机制），
prompt_template 表由迁移 0018 drop，DB 版本机制（POST 新版本 /
activate 切换）随之删除。本路由只读——GET 返回各 mode 模板文本 +
allowed/required 占位符（兼作契约单一来源：前端配置页只读展示、
生成流水线与测试按同一契约渲染）；写端点消失（POST → 405 Method
Not Allowed、activate 路径 → 404，契约锁定测试钉死于
test_api_prompts_admin）。无 DB 读取：session 依赖一并去除。
"""

from fastapi import APIRouter, Query
from pydantic import BaseModel

from tagplatform.capabilities.prompts import (
    ALLOWED_FIELDS,
    MODES,
    REQUIRED_PLACEHOLDERS,
    get_prompt,
)
from tagplatform.capabilities.prompts.registry import list_prompt_registry

router = APIRouter(prefix="/api/v1")


class PromptContractOut(BaseModel):
    """只读契约项：mode + 模板正文 + 占位符白名单/必需项。

    无版本字段——git 即版本，DB 版本机制已随 0018 删除（配置页只读）。
    """

    mode: str
    template_text: str
    allowed_fields: list[str]
    required_placeholders: list[str]


class RegistryItemOut(BaseModel):
    """全量文案清单条目：常量名 + 文案本体。"""

    name: str
    text: str


class RegistryGroupOut(BaseModel):
    """全量文案清单组：按流水线分组（closed/mtg/assets/open_review）。"""

    module: str
    title: str
    items: list[RegistryItemOut]


@router.get("/prompts", response_model=list[PromptContractOut])
def list_prompts(mode: str | None = Query(None)) -> list[PromptContractOut]:
    """Prompt 模板只读清单：mode 精确过滤（未知 mode → 空列表，沿用
    既有过滤语义），按 mode 排序；正文与本地常量逐字一致。"""
    modes = MODES if mode is None else (mode,)
    return [
        PromptContractOut(
            mode=m,
            template_text=get_prompt(m),
            allowed_fields=sorted(ALLOWED_FIELDS),
            required_placeholders=list(REQUIRED_PLACEHOLDERS[m]),
        )
        for m in modes
        if m in MODES
    ]


@router.get("/prompts/registry", response_model=list[RegistryGroupOut])
def prompt_registry() -> list[dict]:
    """全量 Prompt 文案只读清单（2026-11-22 文案集中新增）。

    四组（closed/mtg/assets/open_review），条目 = 常量名 + 文案本体；与
    capabilities/prompts 各模块常量单一来源（registry 静态聚合，无第二份
    拷贝）。只读：文案是代码常量，git 即版本，无写端点。
    """
    return list_prompt_registry()
