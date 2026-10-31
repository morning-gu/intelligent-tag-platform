"""API 层认证依赖：会话解析（get_current_user）与管理员门槛（require_admin）。

属 api 层（可 import fastapi）；令牌编解码在 auth/session.py（纯函数层），
本模块只负责"cookie → UserAccount"的装配与两级权限门槛：
- get_current_user：无 cookie / 签名无效 / 已过期 / 账号已不存在 统一 401
  （不向请求方区分原因——避免向未登录方泄漏令牌细节）；角色以库内现值为准
  （令牌载荷仅承载身份，不承载授权——建号后改角色无需重登录）。
- require_admin：非 admin 角色 403（未登录仍由内层 get_current_user 401）。
"""

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from tagplatform.api import get_session
from tagplatform.auth.session import parse_session_token
from tagplatform.db.models import UserAccount

# 会话 cookie 名（登录下发、依赖解析、登出清除共用同一常量）
SESSION_COOKIE_NAME = "tagplatform_session"


def get_current_user(request: Request,
                     session: Session = Depends(get_session)) -> UserAccount:
    """会话依赖：cookie 令牌解析 → 按载荷 user_id 回库取账号；任何失败 401。"""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    payload = (parse_session_token(token, request.app.state.settings.session_secret)
               if token else None)
    user_id = payload.get("user_id") if payload else None
    user = session.get(UserAccount, user_id) if isinstance(user_id, str) else None
    if user is None:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    return user


def require_admin(user: UserAccount = Depends(get_current_user)) -> UserAccount:
    """管理员门槛依赖：非 admin 角色 403（未登录由内层 401，先行短路）。"""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user
