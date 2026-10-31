"""Prompt 模板服务：激活模板查询与占位符渲染。

模板正文外置在 prompt_template 表（v1 种子由迁移 0002 逐字播种），本模块
只负责读取与渲染，不含任何模板文案。阶段 2 生成流水线（Task 5 产出器、
Task 8 执行器）是这两个函数的消费方。
分层红线：本模块不得 import fastapi（设计文档 §9.4）。
"""

from sqlalchemy.orm import Session

from tagplatform.db.models import PromptTemplate


def get_active_prompt(session: Session, mode: str) -> PromptTemplate:
    """返回指定 mode 当前激活（is_active 为真）的 Prompt 模板。

    无激活模板时抛 ValueError：缺模板属配置错误（种子未跑或被误停用），
    必须显式失败，调用方不应静默回退。
    """
    template = (
        session.query(PromptTemplate)
        .filter(PromptTemplate.mode == mode, PromptTemplate.is_active.is_(True))
        .one_or_none()
    )
    if template is None:
        raise ValueError(f"mode={mode} 无激活的 Prompt 模板")
    return template


def render_prompt(template: PromptTemplate, **fields: str) -> str:
    """以 fields 填充模板占位符（str.format 语义）。

    缺少某个占位符的实参时 KeyError 自然抛出；多余实参被忽略。
    """
    return template.template_text.format(**fields)
