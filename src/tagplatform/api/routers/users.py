"""`/api/v1/users` 路由：账号列表与建号（仅 admin）。

薄路由层：直接调用 tagplatform.auth 服务（create_user / UserAccount 查询），
router 只做协议转换——role 取值与字段合法性由 pydantic 声明校验
（Literal/非空 → 422），重名冲突（服务层 ValueError）映射 409，权限门槛
require_admin（非 admin 403）。登录门槛（get_current_user）由 create_app
include 时与其余业务 router 统一挂载。
"""

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from tagplatform.api import get_session
from tagplatform.api.depends import require_admin
from tagplatform.auth import create_user as create_user_account
from tagplatform.db.models import UserAccount

router = APIRouter(prefix="/api/v1/users")


class UserOut(BaseModel):
    """账号视图：绝不含 password_hash（哈希不外泄）。"""

    user_id: str
    username: str
    role: str
    created_at: datetime | None = None


class UserListResponse(BaseModel):
    """账号列表响应（沿用平台 items 包裹约定）。"""

    items: list[UserOut]


class CreateUserRequest(BaseModel):
    """建号请求：username/password 非空（空密码账号永远过不了登录守卫，
    直接在入口 422）；role 取值 admin|reviewer 之外 422。"""

    username: str = Field(min_length=1)
    password: str = Field(min_length=1)
    role: Literal["admin", "reviewer"]


@router.get("", response_model=UserListResponse)
def list_users(
    _admin: UserAccount = Depends(require_admin),
    session: Session = Depends(get_session),
) -> UserListResponse:
    """全部账号列表（仅 admin）：按 user_id 排序，不含 password_hash。"""
    rows = session.query(UserAccount).order_by(UserAccount.user_id).all()
    return UserListResponse(items=[
        UserOut(user_id=u.user_id, username=u.username, role=u.role,
                created_at=u.created_at)
        for u in rows
    ])


@router.post("", response_model=UserOut)
def create_user(
    payload: CreateUserRequest,
    _admin: UserAccount = Depends(require_admin),
    session: Session = Depends(get_session),
) -> UserOut:
    """建号（仅 admin）：重名 409；成功返回账号视图（含 user_id 前缀主键）。"""
    try:
        user = create_user_account(session, payload.username, payload.password,
                                   payload.role)
    except ValueError:
        # 服务层重名校验（表级 UNIQUE 兜底）→ 409 冲突
        raise HTTPException(status_code=409,
                            detail=f"用户名已存在：{payload.username}")
    return UserOut(user_id=user.user_id, username=user.username, role=user.role,
                   created_at=user.created_at)
