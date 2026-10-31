"""`/api/v1/auth` 路由：登录、登出与当前用户摘要。

薄路由层：密码校验复用 tagplatform.auth（verify_password），会话令牌经
auth.session 纯函数签发；本 router 不在 create_app 统一挂会话保护——
login 是"取会话"的入口必须开放，/me 经端点级 get_current_user 自行保护，
logout 仅清 cookie（无 cookie 时幂等，无需认证）。

登录失败（用户不存在/密码错/空密码输入）统一 401 {"detail": "用户名或密码
错误"}——不区分具体原因（不泄漏账号存在性）；空密码输入必须先于
verify_password 拦下（B1 遗留注意：bootstrap 可能经 TAGPLT_ADMIN_PASSWORD=""
建出空密码 admin，verify_password 对空串可正常比对通过，不守卫则空密码
可登录）。
"""

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from tagplatform.api import get_session, get_settings_dep
from tagplatform.api.depends import SESSION_COOKIE_NAME, get_current_user
from tagplatform.auth import get_user_by_username, verify_password
from tagplatform.auth.session import create_session_token
from tagplatform.db.models import UserAccount
from tagplatform.settings import Settings

router = APIRouter(prefix="/api/v1/auth")


class LoginRequest(BaseModel):
    """登录表单：用户名与明文密码（missing 字段由 FastAPI 校验 422）。"""

    username: str
    password: str


class UserBrief(BaseModel):
    """登录态用户摘要（login 响应与 /me 共用，绝不含 password_hash）。"""

    user_id: str
    username: str
    role: str


@router.post("/login", response_model=UserBrief)
def login(
    payload: LoginRequest,
    response: Response,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> UserBrief:
    """登录：校验用户名密码，成功签发 HttpOnly 会话 cookie 并返回用户摘要。"""
    user = get_user_by_username(session, payload.username)
    if (not payload.password or user is None
            or not verify_password(payload.password, user.password_hash)):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    token = create_session_token(user.user_id, user.role,
                                 settings.session_secret)
    # HttpOnly：脚本不可读（XSS 防护）；有效期内服务端校验（exp 在令牌内）
    response.set_cookie(SESSION_COOKIE_NAME, token, httponly=True)
    return UserBrief(user_id=user.user_id, username=user.username,
                     role=user.role)


@router.post("/logout")
def logout(response: Response) -> dict[str, bool]:
    """登出：清会话 cookie（幂等；令牌本身不维护服务端黑名单，登出即弃凭据）。"""
    response.delete_cookie(SESSION_COOKIE_NAME)
    return {"ok": True}


@router.get("/me", response_model=UserBrief)
def me(user: UserAccount = Depends(get_current_user)) -> UserBrief:
    """当前登录用户摘要：未登录/会话过期 → 401（get_current_user）。"""
    return UserBrief(user_id=user.user_id, username=user.username,
                     role=user.role)
