"""GET /api/v1/config（admin，只读运行配置）端点测试。

契约：13 个字段齐且不多不少；机密不外泄（无 session_secret/admin_password
键，序列化结果中无默认密钥值）；reviewer 403、未登录 401。
（semantic_similar_threshold 随手动导入取消余弦相似控制退役。）
"""

import json

# 契约字段集合（精确相等断言：既不缺字段，也不得多余键——多余键可能泄密）
EXPECTED_KEYS = {
    "llm_model", "llm_temperature", "mtg_judge_model", "mtg_judge_enabled",
    "embedding_backend", "embedding_model", "open_eval_review_model",
    "recall_top_k_per_type", "open_library_suspect_cosine",
    "open_library_name_overlap", "generation_workers", "use_mock_llm",
    "use_mock_embedder", "database_path",
}


def test_config_reports_effective_remote_embedding_model():
    """embedding_backend=remote 时，embedding_model 返回实际生效的 embedding_api_model。"""
    from tagplatform.orchestration.config import get_config
    from tagplatform.settings import Settings

    remote = get_config(Settings(
        embedding_backend="remote",
        embedding_model="local/ignored",
        embedding_api_model="bge-m3-remote",
    ))
    assert remote.embedding_backend == "remote"
    assert remote.embedding_model == "bge-m3-remote"


def test_config_contains_no_secrets(client):
    """只读字段集合精确匹配契约，机密不外泄（无密钥键且无默认密钥值）。"""
    resp = client.get("/api/v1/config")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == EXPECTED_KEYS
    assert "session_secret" not in body
    assert "admin_password" not in body
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
