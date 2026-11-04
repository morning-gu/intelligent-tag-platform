"""GET /api/v1/config（admin，只读运行配置）端点测试。

契约：9 个字段齐且不多不少；机密不外泄（无 session_secret/admin_password
键，序列化结果中无默认密钥值）；reviewer 403、未登录 401。
"""

import json

# 契约字段集合（精确相等断言：既不缺字段，也不得多余键——多余键可能泄密）
EXPECTED_KEYS = {
    "llm_model", "llm_temperature", "recall_top_k_per_type",
    "open_match_threshold", "semantic_similar_threshold",
    "generation_workers", "use_mock_llm", "use_mock_embedder",
    "database_path",
}


def test_config_returns_all_fields(client):
    """字段集合精确相等，取值与 conftest client fixture 的 Settings 一致。"""
    resp = client.get("/api/v1/config")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == EXPECTED_KEYS
    # 默认值（fixture 只覆盖 mock 开关/workers/库路径）
    assert body["llm_model"] == "qwen3.7-plus"
    assert body["llm_temperature"] == 0.7
    assert body["recall_top_k_per_type"] == 50
    assert body["open_match_threshold"] == 0.85
    assert body["semantic_similar_threshold"] == 0.85
    assert body["generation_workers"] == 0  # fixture 钉死同步执行
    assert body["use_mock_llm"] is True
    assert body["use_mock_embedder"] is True
    assert body["database_path"].endswith("tagplatform.db")  # fixture 临时库


def test_config_contains_no_secrets(client):
    """机密不外泄：无 session_secret/admin_password 键，值中无默认密钥串。"""
    body = client.get("/api/v1/config").json()
    assert "session_secret" not in body
    assert "admin_password" not in body
    # 兜底：默认会话密钥值（dev-secret）不得出现在任何序列化值中
    assert "dev-secret" not in json.dumps(body)


def test_config_requires_admin(client):
    """reviewer → 403（已登录用户被权限门槛挡下）；未登录 → 401。"""
    assert client.post("/api/v1/users", json={
        "username": "bob", "password": "pw-bob-1", "role": "reviewer"}
    ).status_code == 200
    client.post("/api/v1/auth/logout")
    client.post("/api/v1/auth/login",
                json={"username": "bob", "password": "pw-bob-1"})
    assert client.get("/api/v1/config").status_code == 403

    client.cookies.delete("tagplatform_session")
    assert client.get("/api/v1/config").status_code == 401
