"""`/api/v1/prompts` 路由：Prompt 模板列表与版本管理（写路径仅 admin）。

薄路由层：读取走 GET /prompts（模板清单，正文大字段不出列表——正文仅供
生成流水线 Task 8 执行器消费）；写入经 tagplatform.prompts 服务层
（create_prompt_version / activate_prompt_version）——版本计算、占位符
校验与激活切换的业务逻辑都在服务层，本层只做协议转换：正文非法
（服务层 ValueError）→ 422，未知 id → 404，权限门槛 require_admin
（非 admin 403）。历史版本不可变：不提供编辑/删除端点，换提示词 =
追加新版本 + 切换激活（改提示词零代码变更的运营闭环，阶段 4 Task 2）。
"""

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from tagplatform import prompts as prompt_service
from tagplatform.api import get_session
from tagplatform.api.depends import require_admin
from tagplatform.db.models import PromptTemplate, UserAccount

router = APIRouter(prefix="/api/v1")


class PromptOut(BaseModel):
    """Prompt 模板列表项：id + 模式 + 版本 + 是否激活 + 创建时间。

    id 供 activate 路由定位（prompt_template 表自增主键，阶段 2 迁移 0002）。
    """

    id: int
    mode: str
    version: int
    is_active: bool
    created_at: datetime | None = None


class PromptActivateOut(BaseModel):
    """激活结果：契约三字段（mode/version/is_active）。"""

    mode: str
    version: int
    is_active: bool


class PromptCreateRequest(BaseModel):
    """新建版本请求：mode 取值 closed|open 之外 422；正文合法性
    （strip 非空 + 必需占位符）由服务层校验（ValueError → 422）。"""

    mode: Literal["closed", "open"]
    template_text: str


@router.get("/prompts", response_model=list[PromptOut])
def list_prompts(
    mode: str | None = Query(None),
    session: Session = Depends(get_session),
) -> list[PromptOut]:
    """Prompt 模板列表：mode 精确过滤，按 mode + version 排序。"""
    query = session.query(PromptTemplate)
    if mode is not None:
        query = query.filter(PromptTemplate.mode == mode)
    rows = query.order_by(PromptTemplate.mode, PromptTemplate.version).all()
    return [
        PromptOut(id=r.id, mode=r.mode, version=r.version, is_active=r.is_active,
                  created_at=r.created_at)
        for r in rows
    ]


@router.post("/prompts", response_model=PromptOut)
def create_prompt_version(
    payload: PromptCreateRequest,
    _admin: UserAccount = Depends(require_admin),
    session: Session = Depends(get_session),
) -> PromptOut:
    """新建 Prompt 版本（仅 admin）：正文非法 422；成功返回新激活版本。

    新版本落库即激活，同 mode 旧激活行同事务去激活（恰一个 active，
    原子性由服务层单事务保证）。
    """
    try:
        row = prompt_service.create_prompt_version(
            session, payload.mode, payload.template_text)
    except ValueError as exc:
        # 服务层正文校验失败（空文本/缺占位符/未知 mode）→ 422
        raise HTTPException(status_code=422, detail=str(exc))
    return PromptOut(id=row.id, mode=row.mode, version=row.version,
                     is_active=row.is_active, created_at=row.created_at)


@router.post("/prompts/{prompt_id}/activate", response_model=PromptActivateOut)
def activate_prompt(
    prompt_id: int,
    _admin: UserAccount = Depends(require_admin),
    session: Session = Depends(get_session),
) -> PromptActivateOut:
    """激活指定版本（仅 admin）：未知 id 404；幂等（已激活原样返回）。

    切换为单事务（服务层保证）：同 mode 其它行同事务去激活。
    """
    row = prompt_service.activate_prompt_version(session, prompt_id)
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"未知 Prompt 模板 id：{prompt_id}")
    return PromptActivateOut(mode=row.mode, version=row.version,
                             is_active=row.is_active)
