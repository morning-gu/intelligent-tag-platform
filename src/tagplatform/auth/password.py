"""密码哈希与校验（全 stdlib，零新增依赖）。

算法契约：`hashlib.pbkdf2_hmac("sha256", pwd.encode(), salt, 200_000)`，
salt 为 os.urandom 生成的 16 字节随机数；存储格式
`pbkdf2$200000${salt_hex}${hash_hex}`（轮数写入存储串，校验按串内轮数
展开，未来提轮数时旧哈希仍可校验）。
verify_password 对任何坏格式返回 False 而不抛异常（坏输入与错密码同路径，
避免格式异常泄漏为 500）；摘要比较用 hmac.compare_digest 防时序侧信道。
"""

import hashlib
import hmac
import os

# pbkdf2 迭代轮数与盐字节数：与存储格式前缀 `pbkdf2$200000$` 咬合
_PBKDF2_ROUNDS = 200_000
_SALT_BYTES = 16


def hash_password(pwd: str) -> str:
    """哈希明文密码，返回 `pbkdf2$200000$salt_hex$hash_hex` 存储串。"""
    salt = os.urandom(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        "sha256", pwd.encode("utf-8"), salt, _PBKDF2_ROUNDS)
    return f"pbkdf2${_PBKDF2_ROUNDS}${salt.hex()}${digest.hex()}"


def verify_password(pwd: str, stored: str) -> bool:
    """校验明文密码与存储串；坏格式返回 False 不抛。

    拆分段数不足/轮数非数字/十六进制非法/未知 scheme 一律 False；
    hash 段含非 ASCII 字符时 compare_digest 抛 TypeError，同样按坏格式
    归入 False（损坏/手改的库值不得炸成 500）；None 等非字符串输入
    （AttributeError）一并归入捕获。
    """
    try:
        scheme, rounds_str, salt_hex, hash_hex = stored.split("$")
        if scheme != "pbkdf2":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", pwd.encode("utf-8"),
            bytes.fromhex(salt_hex), int(rounds_str))
        return hmac.compare_digest(digest.hex(), hash_hex)
    except (ValueError, TypeError, AttributeError):
        return False
