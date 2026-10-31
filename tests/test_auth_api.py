"""会话认证与 API 保护（B2）测试。

契约咬合面：
- 令牌：`base64url(payload JSON)` + "." + HMAC-SHA256 签名 hex；载荷
  {"user_id","role","exp"}；parse 对 篡改/错密钥/过期（ttl_days=0）/坏格式
  返回 None 不抛。
- 路由：login（错 → 401 {"detail": "用户名或密码错误"}，成功下发 HttpOnly
  cookie `tagplatform_session`）、logout（清 cookie）、me（登录态摘要）。
- 全 API 保护：/api/v1/* 无 cookie 401（/health 例外开放）；users 路由仅
  admin（非 admin 403），role 取值 admin|reviewer 之外 422。
- 既有 1/2 阶段 API 测试断言零改动：conftest client fixture 登录后全绿
  （本文件用同一 fixture，即默认处于已登录 admin 上下文）。
"""

import pytest
from fastapi.testclient import TestClient

from tagplatform.auth.session import create_session_token, parse_session_token

# conftest client fixture 的 Settings 未覆盖 session_secret，默认即此值
# （仅测试环境；生产经 TAGPLT_SESSION_SECRET 覆盖）
_DEV_SECRET = "dev-secret"


# ---------- 令牌编解码（纯函数层） ----------

def test_token_roundtrip():
    """签发→解析往返：载荷 user_id/role 原样返回，exp 为数值。"""
    token = create_session_token("user_abc123", "reviewer", _DEV_SECRET)
    payload = parse_session_token(token, _DEV_SECRET)
    assert payload is not None
    assert payload["user_id"] == "user_abc123"
    assert payload["role"] == "reviewer"
    assert isinstance(payload["exp"], (int, float))


def test_token_default_ttl_is_7_days():
    """默认有效期 7 天：exp 距现在约 7*86400 秒（留分钟级容差）。"""
    import time

    token = create_session_token("u", "admin", _DEV_SECRET)
    exp = parse_session_token(token, _DEV_SECRET)["exp"]
    delta = exp - time.time()
    assert 7 * 86400 - 60 < delta <= 7 * 86400


def test_token_wrong_secret_none():
    """错密钥签名不匹配 → None（不抛）。"""
    token = create_session_token("u1", "admin", "secret-a")
    assert parse_session_token(token, "secret-b") is None


def test_token_expired_ttl_zero_none():
    """ttl_days=0 签发即过期 → None。"""
    token = create_session_token("u1", "admin", _DEV_SECRET, ttl_days=0)
    assert parse_session_token(token, _DEV_SECRET) is None


def test_token_tampered_signature_none():
    """篡改签名一位 → None（签名比对不通过）。"""
    token = create_session_token("u1", "admin", _DEV_SECRET)
    payload_b64, sig = token.split(".")
    # 翻转签名末位 hex 字符（0 ↔ f 保证必然改变）
    tampered_char = "f" if sig[-1] != "f" else "0"
    tampered = f"{payload_b64}.{sig[:-1]}{tampered_char}"
    assert tampered != token
    assert parse_session_token(tampered, _DEV_SECRET) is None


def test_token_tampered_payload_none():
    """篡改载荷（换用户）→ 签名不符 → None。"""
    token = create_session_token("u1", "admin", _DEV_SECRET)
    payload_b64, sig = token.split(".")
    other_payload = payload_b64[:-4] + "AAAA"  # 破坏 base64 载荷内容
    assert parse_session_token(f"{other_payload}.{sig}", _DEV_SECRET) is None


@pytest.mark.parametrize("bad", [
    "",
    "no-dot-token",
    "a.b.c",  # 段数超过 2
    "!!!.@@@",  # 载荷非 base64
])
def test_token_bad_format_none(bad):
    """坏格式令牌一律 None 不抛（与未认证同路径）。"""
    assert parse_session_token(bad, _DEV_SECRET) is None


# ---------- 登录 / 登出 / me ----------

def test_login_success_sets_httponly_cookie(client):
    """登录成功：200 + {user_id,username,role}，下发 HttpOnly 会话 cookie。"""
    resp = client.post("/api/v1/auth/login",
                       json={"username": "admin", "password": "admin123"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["username"] == "admin"
    assert body["role"] == "admin"
    assert body["user_id"].startswith("user_")
    set_cookie = resp.headers["set-cookie"]
    assert "tagplatform_session=" in set_cookie
    assert "httponly" in set_cookie.lower()


def test_login_wrong_password_401(client):
    """密码错 → 401，detail 固定文案（不区分用户名/密码哪个错）。"""
    resp = client.post("/api/v1/auth/login",
                       json={"username": "admin", "password": "wrong"})
    assert resp.status_code == 401
    assert resp.json() == {"detail": "用户名或密码错误"}


def test_login_unknown_user_401(client):
    """用户名不存在 → 401 同一文案（不泄漏账号存在性）。"""
    resp = client.post("/api/v1/auth/login",
                       json={"username": "nobody", "password": "whatever"})
    assert resp.status_code == 401
    assert resp.json() == {"detail": "用户名或密码错误"}


def test_login_empty_password_401(client):
    """空密码输入 → 401（B1 遗留守卫：verify_password 对空串可正常比对
    通过，不守卫则空密码 admin 可登录）。"""
    resp = client.post("/api/v1/auth/login",
                       json={"username": "admin", "password": ""})
    assert resp.status_code == 401
    assert resp.json() == {"detail": "用户名或密码错误"}


def test_me_returns_current_user(client):
    """me：登录态返回当前用户摘要（含角色）。"""
    resp = client.get("/api/v1/auth/me")
    assert resp.status_code == 200
    body = resp.json()
    assert body["username"] == "admin"
    assert body["role"] == "admin"
    assert body["user_id"].startswith("user_")


def test_me_without_cookie_401(client):
    """me：无 cookie → 401。"""
    client.cookies.delete("tagplatform_session")
    resp = client.get("/api/v1/auth/me")
    assert resp.status_code == 401


def test_logout_clears_session(client):
    """登出：清 cookie，之后再访问受保护 API → 401。"""
    resp = client.post("/api/v1/auth/logout")
    assert resp.status_code == 200
    assert not client.cookies.get("tagplatform_session")  # 已清除/过期均为假值
    assert client.get("/api/v1/tags").status_code == 401


# ---------- 全 API 保护（/health 除外） ----------

def test_protected_api_without_cookie_401_health_open(client):
    """/api/v1/* 无 cookie → 401；/health 无需登录（开放例外）。"""
    client.cookies.delete("tagplatform_session")
    assert client.get("/api/v1/tags").status_code == 401
    assert client.get("/api/v1/import/batches").status_code == 401
    assert client.get("/api/v1/assets").status_code == 401
    assert client.get("/api/v1/generation/tasks").status_code == 401
    assert client.get("/api/v1/prompts").status_code == 401
    # health 例外：无 cookie 仍 200
    assert client.get("/health").status_code == 200


def test_tampered_session_cookie_401(client):
    """cookie 内容被篡改/伪造 → 401（签名校验在 API 路径同样生效）。

    先删后设：登录态 cookie 在 jar 内是 host-only 键，直接 set(domain=...)
    会并存另一条目而非覆盖。
    """
    client.cookies.delete("tagplatform_session")
    client.cookies.set("tagplatform_session", "forged.payload.deadbeef",
                       domain="testserver")
    assert client.get("/api/v1/tags").status_code == 401


def test_expired_session_cookie_401(client):
    """过期令牌（ttl_days=0 签发）作 cookie → 401。"""
    token = create_session_token("user_admin", "admin", _DEV_SECRET, ttl_days=0)
    client.cookies.delete("tagplatform_session")
    client.cookies.set("tagplatform_session", token, domain="testserver")
    assert client.get("/api/v1/tags").status_code == 401


# ---------- 用户管理（仅 admin） ----------

def test_admin_creates_reviewer_account(client):
    """admin 建号：返回账号视图（不含 password_hash），列表可见，新号可登录。"""
    resp = client.post("/api/v1/users", json={
        "username": "alice", "password": "pw-alice-1", "role": "reviewer"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["username"] == "alice"
    assert body["role"] == "reviewer"
    assert body["user_id"].startswith("user_")
    assert "password_hash" not in body

    listing = client.get("/api/v1/users").json()["items"]
    names = {u["username"] for u in listing}
    assert names == {"admin", "alice"}
    assert all("password_hash" not in u for u in listing)

    # 新号端到端可登录（密码以哈希落库），me 显示 reviewer 角色
    login = client.post("/api/v1/auth/login",
                        json={"username": "alice", "password": "pw-alice-1"})
    assert login.status_code == 200
    assert client.get("/api/v1/auth/me").json()["role"] == "reviewer"


def test_reviewer_forbidden_on_user_management(client):
    """非 admin（reviewer）访问用户管理 → 403；普通 API 仍可用。"""
    assert client.post("/api/v1/users", json={
        "username": "bob", "password": "pw-bob-1", "role": "reviewer"}
    ).status_code == 200  # admin 先建 reviewer 账号
    client.post("/api/v1/auth/logout")
    client.post("/api/v1/auth/login",
                json={"username": "bob", "password": "pw-bob-1"})

    assert client.get("/api/v1/users").status_code == 403
    assert client.post("/api/v1/users", json={
        "username": "carol", "password": "pw-c-1", "role": "reviewer"}
    ).status_code == 403
    # reviewer 是合法登录用户：普通业务 API 放行
    assert client.get("/api/v1/tags").status_code == 200
    assert client.get("/api/v1/auth/me").json()["role"] == "reviewer"


def test_create_user_invalid_role_422(client):
    """role 取值 admin|reviewer 之外 → 422。"""
    resp = client.post("/api/v1/users", json={
        "username": "mallory", "password": "pw-m-1", "role": "superadmin"})
    assert resp.status_code == 422


def test_create_user_duplicate_username_409(client):
    """重名建号 → 409（服务层 ValueError 映射冲突）。"""
    payload = {"username": "dup", "password": "pw-dup-1", "role": "reviewer"}
    assert client.post("/api/v1/users", json=payload).status_code == 200
    assert client.post("/api/v1/users", json=payload).status_code == 409


def test_create_user_empty_fields_422(client):
    """空 username / 空 password 建号 → 422（空密码账号过不了登录守卫）。"""
    assert client.post("/api/v1/users", json={
        "username": "", "password": "pw-x-1", "role": "reviewer"}
    ).status_code == 422
    assert client.post("/api/v1/users", json={
        "username": "empty-pw", "password": "", "role": "reviewer"}
    ).status_code == 422


# ---------- 会话密钥来自 Settings（TAGPLT_SESSION_SECRET 生效） ----------

def test_custom_session_secret_is_honored(tmp_path):
    """app 配置了自定义密钥时：dev-secret 签的令牌被拒，自签令牌通过——
    证明签发与校验都取 Settings.session_secret（生产 TAGPLT_SESSION_SECRET
    生效的依据）。自建 app（不经 conftest client，密钥是 app 级配置）。"""
    from tagplatform.api import create_app
    from tagplatform.db import create_engine_for
    from tagplatform.db.models import Base
    from tagplatform.settings import Settings

    db_path = tmp_path / "tagplatform.db"
    settings = Settings(database_path=str(db_path), use_mock_embedder=True,
                        use_mock_llm=True, generation_workers=0,
                        session_secret="prod-ish-secret")
    engine = create_engine_for(str(db_path))
    Base.metadata.create_all(engine)
    engine.dispose()

    app = create_app(settings)
    with TestClient(app) as custom_client:
        # 登录 cookie 以自定义密钥签发 → 校验通过
        login = custom_client.post("/api/v1/auth/login", json={
            "username": "admin", "password": "admin123"})
        assert login.status_code == 200
        assert custom_client.get("/api/v1/tags").status_code == 200
        # dev-secret（默认值）签发的合法格式令牌 → 401（密钥不匹配）
        stale = create_session_token("user_x", "admin", _DEV_SECRET)
        custom_client.cookies.delete("tagplatform_session")
        custom_client.cookies.set("tagplatform_session", stale,
                                  domain="testserver")
        assert custom_client.get("/api/v1/tags").status_code == 401
    app.state.engine.dispose()
