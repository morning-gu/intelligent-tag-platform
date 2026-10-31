"""HMAC 会话令牌编解码（阶段 3 B2，纯函数层）。

分层红线：本模块不得 import fastapi（设计文档 §9.4）——令牌如何从 cookie
取出、解析出的身份如何变成 UserAccount、cookie 如何下发，全部由 api 层
（api/depends.py 与登录路由）负责，本模块只做令牌字符串 ↔ 载荷 dict 的
编解码与校验。

令牌格式：`base64url(payload_json)` + "." + `hex(hmac_sha256(payload_b64, secret))`；
载荷 JSON：{"user_id": ..., "role": ..., "exp": <Unix 秒>}，exp 由 ttl_days
（默认 7 天）在签发时折算。解析按同密钥重算签名并以 hmac.compare_digest
比对（防时序侧信道），签名不符 / 载荷损坏 / 已过期（now >= exp）一律返回
None 不抛——调用方（get_current_user）统一按"未认证"处理，不向请求方
区分失败原因。
密钥由调用方传入（api 层从 Settings.session_secret 取，环境变量
TAGPLT_SESSION_SECRET，默认 "dev-secret"，生产必改），本模块不读环境。
"""

import base64
import hashlib
import hmac
import json
import time

# 默认有效期（天）：契约 7 天；exp = 签发时刻 + ttl_days * 86400 秒
DEFAULT_TTL_DAYS = 7


def create_session_token(user_id: str, role: str, secret: str,
                         ttl_days: int = DEFAULT_TTL_DAYS) -> str:
    """为已认证身份签发会话令牌：载荷 JSON base64url + "." + HMAC-SHA256 签名 hex。

    载荷含 user_id/role/exp 三键（exp 为 Unix 秒）；JSON 紧凑序列化
    （sort_keys 确定性输出，同输入必得同令牌前缀，便于测试比对）。
    """
    payload = {
        "user_id": user_id,
        "role": role,
        "exp": time.time() + ttl_days * 86400,
    }
    payload_b64 = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).decode("ascii")
    sig = hmac.new(secret.encode("utf-8"), payload_b64.encode("ascii"),
                   hashlib.sha256).hexdigest()
    return f"{payload_b64}.{sig}"


def parse_session_token(token: str, secret: str) -> dict | None:
    """解析并校验会话令牌；签名不符/载荷损坏/已过期返回 None（不抛）。

    签名对 base64 载荷原文重算并 compare_digest 比对（时序安全）；过期判定
    now >= exp（ttl_days=0 签发即过期）。非 dict 载荷、缺/非数值 exp 同样
    视为无效——返回的 dict 只保证"由本服务签发且未过期"，字段完整性由
    调用方按需取用。
    """
    try:
        payload_b64, sig = token.split(".")
        expected = hmac.new(secret.encode("utf-8"), payload_b64.encode("ascii"),
                            hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(base64.urlsafe_b64decode(payload_b64.encode("ascii")))
    except (ValueError, TypeError, AttributeError):
        # 覆盖：段数不为 2（拆包 ValueError）、base64 非法字符/补位
        # （binascii.Error，ValueError 子类）、JSON 损坏（JSONDecodeError，
        # ValueError 子类）、非 str 输入（AttributeError）
        return None
    if not isinstance(payload, dict):
        return None
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)) or time.time() >= exp:
        return None
    return payload
