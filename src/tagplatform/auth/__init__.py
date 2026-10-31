"""认证域核心包：密码哈希/校验与用户账号服务（阶段 3 B1）。

分层红线：本包不得 import fastapi（设计文档 §9.4）——登录路由、
HMAC 会话 cookie 与 get_current_user 依赖在 api 层（Task 2），本包
只提供核心密码学与账号管理逻辑，供 api 层与 lifespan 引导复用。
"""

from tagplatform.auth.password import hash_password, verify_password
from tagplatform.auth.service import (
    bootstrap_admin,
    create_user,
    get_user_by_username,
)

__all__ = [
    "hash_password",
    "verify_password",
    "create_user",
    "get_user_by_username",
    "bootstrap_admin",
]
