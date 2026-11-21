"""Prompt 本地化设施：模板常量读取与渲染（纯读、无 DB、无状态）。

四层重构 Task 3 起 prompt 不再外置 DB（prompt_template 表由迁移 0018
drop，DB 版本机制随之删除）：文案是开发者资产，git 即版本机制。本包是
纯读设施——get_prompt 返回本地常量（未知 mode 抛 ValueError，调用方
显式失败），render_prompt 是纯文本渲染函数（str.format 语义）；占位符
契约（白名单 / 必需项）由 ALLOWED_FIELDS / REQUIRED_PLACEHOLDERS 导出，
模板合法性（必需占位符齐全、无白名单外字段）由 test_prompts_local 的
断言钉死——原落库前运行时校验随 DB 版本机制一起退役。

分层红线：本包不得 import fastapi / sqlalchemy（能力层红线 + 无 DB 依赖）；
消费方（tasks/runner、services/generation、closed_eval）一律向下 import 本包。
"""

from tagplatform.capabilities.prompts.templates import (
    CLOSED_TEMPLATE_TEXT,
    OPEN_TEMPLATE_TEXT,
)

__all__ = [
    "ALLOWED_FIELDS",
    "CLOSED_TEMPLATE_TEXT",
    "MODES",
    "OPEN_TEMPLATE_TEXT",
    "REQUIRED_PLACEHOLDERS",
    "get_prompt",
    "render_prompt",
]

# 支持的 mode 取值（只读契约视图按此枚举；与 generation_task.mode 的
# CHECK 约束同口径）
MODES: tuple[str, ...] = ("closed", "open")

# 各 mode 模板必须包含的占位符：closed/open 均仅内容——closed 的候选标签
# 分组 JSON 由 ClosedTagProducer 在 produce 内拼接（不经占位符注入）。
REQUIRED_PLACEHOLDERS: dict[str, tuple[str, ...]] = {
    "closed": ("{content}",),
    "open": ("{content}",),
}

# 占位符白名单：render_prompt 只以 content 一个关键字实参调用，模板里
# 任何其他字段（如含 {"tag_id": ...} 的 JSON 输出示例、空的 {}）都属
# 非法文案。字面花括号用 {{ }} 双写转义（parse 对转义段返回 None）。
ALLOWED_FIELDS: frozenset[str] = frozenset({"content"})

# mode → 模板常量（本地化的读取表；新增 mode = 加常量 + 更新契约映射）
_MODE_TEMPLATES: dict[str, str] = {
    "closed": CLOSED_TEMPLATE_TEXT,
    "open": OPEN_TEMPLATE_TEXT,
}
assert set(_MODE_TEMPLATES) == set(MODES)  # 读取表与 mode 枚举不漂移


def get_prompt(mode: str) -> str:
    """返回指定 mode 的本地模板文本常量；未知 mode 抛 ValueError。

    无 DB 无状态：返回值即 templates.py 常量本体（git 即版本）。缺模板
    属代码错误（常量缺失应有测试拦截），运行时显式失败、不静默回退。
    """
    if mode not in _MODE_TEMPLATES:
        raise ValueError(f"未知 mode：{mode}")
    return _MODE_TEMPLATES[mode]


def render_prompt(template_text: str, **fields: str) -> str:
    """以 fields 填充模板占位符（str.format 语义；纯函数）。

    缺少某个占位符的实参时 KeyError 自然抛出；多余实参被忽略。
    """
    return template_text.format(**fields)
