"""用户账号服务：建号、按用户名查询与首管理员启动引导。

分层红线：本模块不得 import fastapi（设计文档 §9.4）——登录路由与
会话依赖属 api 层（Task 2）。user_id（`user_`+uuid4().hex[:8]）与
created_at（UTC）均显式赋值，沿用平台通行约定；role 取值 admin|reviewer
由调用方保证（表无 CHECK 约束）。
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from tagplatform.auth.password import hash_password
from tagplatform.db.models import UserAccount

# 首管理员固定用户名与角色（bootstrap_admin 落库用）
ADMIN_USERNAME = "admin"
ADMIN_ROLE = "admin"


def create_user(session: Session, username: str, password: str,
                role: str) -> UserAccount:
    """创建用户账号并提交；重名抛 ValueError（唯一性由服务层先行校验）。"""
    if get_user_by_username(session, username) is not None:
        raise ValueError(f"用户名已存在：{username}")
    user = UserAccount(
        user_id=f"user_{uuid.uuid4().hex[:8]}",
        username=username,
        password_hash=hash_password(password),
        role=role,
        created_at=datetime.now(timezone.utc),
    )
    session.add(user)
    session.commit()
    return user


def get_user_by_username(session: Session, username: str) -> UserAccount | None:
    """按用户名精确查询账号；不存在返回 None。"""
    return (
        session.query(UserAccount)
        .filter(UserAccount.username == username)
        .one_or_none()
    )


def bootstrap_admin(session: Session, password: str) -> bool:
    """首管理员启动引导：user_account 表为空时创建 admin 账号。

    返回是否创建（True = 本次落库 admin；False = 表已有任何用户，跳过）。
    幂等：应用每次启动都会调用，仅空表才建——首个账号可以是后来创建的
    reviewer，同样视为"表非空"而不再引导。
    """
    has_any_user = session.query(UserAccount.user_id).first() is not None
    if has_any_user:
        return False
    create_user(session, ADMIN_USERNAME, password, ADMIN_ROLE)
    return True
