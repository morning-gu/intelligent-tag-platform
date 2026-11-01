"""Prompt 模板服务：版本管理、激活模板查询与占位符渲染。

模板正文外置在 prompt_template 表（v1 种子由迁移 0002 逐字播种），本模块
负责版本写入（阶段 4 Task 2：POST /prompts 新版本 + 激活切换的业务逻辑）
与读取渲染，不含任何模板文案。阶段 2 生成流水线（Task 5 产出器、
Task 8 执行器）是读取函数的消费方。
分层红线：本模块不得 import fastapi（设计文档 §9.4）。
"""

from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from tagplatform.db.models import PromptTemplate

# 各 mode 模板必须包含的占位符：closed 需内容 + 候选清单，open 仅内容
# （渲染期 str.format 需实参齐全，缺占位符的模板会静默丢上下文）
_REQUIRED_PLACEHOLDERS = {
    "closed": ("{content}", "{candidates}"),
    "open": ("{content}",),
}


def _validate_template_text(mode: str, template_text: str) -> str:
    """校验模板正文并返回 strip 后的正文；不合法抛 ValueError（路由层映射 422）。

    规则：strip 后非空，且包含该 mode 必需的占位符；mode 取值
    closed|open 之外直接拒绝（表无 CHECK 约束，合法性由本服务层保证）。
    """
    if mode not in _REQUIRED_PLACEHOLDERS:
        raise ValueError(f"未知 mode：{mode}")
    text = template_text.strip()
    if not text:
        raise ValueError("template_text 不能为空（strip 后）")
    for placeholder in _REQUIRED_PLACEHOLDERS[mode]:
        if placeholder not in text:
            raise ValueError(f"模板缺少必需占位符 {placeholder}（mode={mode}）")
    return text


def _commit_or_rollback(session: Session) -> None:
    """提交当前事务；失败时回滚再抛出（不留半提交状态，会话可复用）。"""
    try:
        session.commit()
    except Exception:
        session.rollback()
        raise


def create_prompt_version(session: Session, mode: str,
                          template_text: str) -> PromptTemplate:
    """新建激活版本：version = 该 mode 现最大版本 + 1，同事务停用旧激活行。

    单事务保证（Review Focus 1）：去激活 UPDATE 与新行 INSERT 在同一次
    commit 中生效——任一环节失败整体回滚，不留半行，同 mode 恒恰一个
    is_active 为真。正文经校验后以 strip 结果入库；历史版本不可变：
    不提供编辑/删除，换提示词 = 追加新版本 + 切换激活。
    created_at 显式赋 UTC now（平台通行约定，见 auth/service.create_user）。
    """
    text = _validate_template_text(mode, template_text)
    max_version = (
        session.query(func.max(PromptTemplate.version))
        .filter(PromptTemplate.mode == mode)
        .scalar()
    )
    # 同事务第一步：停用该 mode 现有激活行（恰一个 active 的不变量由此维持）
    session.query(PromptTemplate).filter(
        PromptTemplate.mode == mode,
        PromptTemplate.is_active.is_(True),
    ).update({"is_active": False}, synchronize_session=False)
    row = PromptTemplate(
        mode=mode,
        version=(max_version or 0) + 1,
        template_text=text,
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    session.add(row)
    _commit_or_rollback(session)
    return row


def activate_prompt_version(session: Session,
                            prompt_id: int) -> PromptTemplate | None:
    """激活指定 id 的模板行（同 mode 其它行停用）；未知 id 返回 None。

    幂等：目标已激活时不做任何写库、原样返回。切换在单事务内完成
    （Review Focus 1）：去激活 UPDATE 与目标行置位一起 commit，失败整体
    回滚——同 mode 恒恰一个 is_active 为真。
    """
    row = session.get(PromptTemplate, prompt_id)
    if row is None or row.is_active:
        return row  # 未知 → None（路由层映射 404）；已激活 → 幂等无写库
    session.query(PromptTemplate).filter(
        PromptTemplate.mode == row.mode,
        PromptTemplate.is_active.is_(True),
    ).update({"is_active": False}, synchronize_session=False)
    row.is_active = True
    _commit_or_rollback(session)
    return row


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
