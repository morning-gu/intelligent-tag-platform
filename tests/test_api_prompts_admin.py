"""Prompt 只读契约锁定测试（四层重构 Task 3 本地化后）。

prompt 本地化（capabilities/prompts/templates.py 常量，git 即版本）后
DB 版本机制退役：POST /api/v1/prompts 与 POST /api/v1/prompts/{id}/activate
端点**删除**，GET /api/v1/prompts 收敛为只读契约视图。本文件把删除本身
钉死为契约（写路径不存在 = 405/404，且任何请求形态都不能复活写路径），
并锁定只读 GET 的响应结构与权限口径——原有 POST/activate 行为用例
（版本新建/激活切换/422 校验/403 门槛/事务原子性）随端点一起退役，
其校验语义由 test_prompts_local 的常量断言承接（模板为代码常量后，
非法文案在测试红而非运行时拦）。
"""

import sqlite3

import pytest

from tagplatform.capabilities.prompts import get_prompt

# 旧文件遗留的合法新模板正文样例：保留在本契约锁定测试中作为"即使请求
# 合法，写路径也已不存在"的反证输入
CLOSED_V2_TEXT = "内容：{content}\n从候选标签分组中选出相关标签，只输出 JSON 数组。"


# ---------- 写端点删除：405 / 404 ----------


def test_post_prompts_removed_405(client):
    """POST /api/v1/prompts → 405：路径只剩 GET（只读资源）。"""
    assert client.post("/api/v1/prompts", json={
        "mode": "closed", "template_text": CLOSED_V2_TEXT}).status_code == 405
    # GET 不受影响（读路径健在）
    assert client.get("/api/v1/prompts").status_code == 200


def test_post_prompts_valid_payload_still_405(client):
    """合法 admin 载荷也无法复活写路径（405 而非 200/422）。"""
    resp = client.post("/api/v1/prompts", json={
        "mode": "open", "template_text": "内容：{content}"})
    assert resp.status_code == 405
    rows = client.get("/api/v1/prompts", params={"mode": "open"}).json()
    assert [r["mode"] for r in rows] == ["open"]


def test_post_prompts_malformed_body_still_405(client):
    """载荷非法（mode 取值外 / 缺字段）也 405：路由匹配先于校验，写路径
    整体不存在——旧的 422 校验语义随端点退役。"""
    assert client.post("/api/v1/prompts",
                       json={"mode": "hybrid", "template_text": ""}).status_code == 405
    assert client.post("/api/v1/prompts", json={}).status_code == 405


def test_activate_endpoint_removed_404(client):
    """POST /api/v1/prompts/{id}/activate → 404：端点整体删除。"""
    assert client.post("/api/v1/prompts/1/activate").status_code == 404


@pytest.mark.parametrize("prompt_id", [1, 99999])
def test_activate_unknown_ids_all_404(client, prompt_id):
    """激活切换对任意 id 都不可达（历史种子 id 与未知 id 同为 404）。"""
    resp = client.post(f"/api/v1/prompts/{prompt_id}/activate")
    assert resp.status_code == 404


# ---------- GET 只读契约视图 ----------


def test_get_prompts_readonly_contract_structure(client):
    """逐项四键契约：mode/template_text/allowed_fields/required_placeholders，
    无 id/version/is_active/created_at（git 即版本，DB 版本机制已删）。"""
    resp = client.get("/api/v1/prompts")
    assert resp.status_code == 200
    rows = resp.json()
    assert [r["mode"] for r in rows] == ["closed", "open"]
    for row in rows:
        assert set(row.keys()) == {"mode", "template_text",
                                   "allowed_fields", "required_placeholders"}
        assert isinstance(row["template_text"], str)


def test_get_prompts_template_text_matches_local_constants(client):
    """正文与本地常量逐字一致（契约视图即常量的只读投影）。"""
    rows = {r["mode"]: r for r in client.get("/api/v1/prompts").json()}
    assert rows["closed"]["template_text"] == get_prompt("closed")
    assert rows["open"]["template_text"] == get_prompt("open")


def test_get_prompts_placeholder_contract(client):
    """占位符契约：白名单只含 content、必需项 {content}（与渲染契约同源）。"""
    for row in client.get("/api/v1/prompts").json():
        assert row["allowed_fields"] == ["content"]
        assert row["required_placeholders"] == ["{content}"]


def test_get_prompts_mode_filter_semantics(client):
    """mode 精确过滤沿用既有语义：命中单项、未知 mode 空列表。"""
    closed = client.get("/api/v1/prompts", params={"mode": "closed"}).json()
    assert [r["mode"] for r in closed] == ["closed"]
    assert client.get("/api/v1/prompts",
                      params={"mode": "hybrid"}).json() == []


def test_get_prompts_template_text_renders_with_whitelisted_field(client):
    """GET 契约与渲染契约咬合：正文可用白名单字段直接渲染（前端展示与
    流水线消费同源）。"""
    rows = client.get("/api/v1/prompts", params={"mode": "closed"}).json()
    rendered = rows[0]["template_text"].format(content="内容X")
    assert "内容X" in rendered and "{" not in rendered.replace("{{", "")


def test_prompt_table_absent_in_app_db(client, tmp_path):
    """DB 契约锁定：应用库无 prompt_template 表（0018 drop 后 ORM 同删，
    create_all 亦不再建表）。"""
    conn = sqlite3.connect(tmp_path / "tagplatform.db")
    try:
        tables = {
            row[0] for row in
            conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()
    assert "prompt_template" not in tables


# ---------- 权限口径：只读放行合法登录用户 ----------


def test_non_admin_can_read_prompts_contract(client):
    """reviewer 访问只读契约视图 → 200（与旧 GET 口径一致：登录即可读）。"""
    assert client.post("/api/v1/users", json={
        "username": "bob", "password": "pw-bob-1", "role": "reviewer"}
    ).status_code == 200
    client.post("/api/v1/auth/logout")
    client.post("/api/v1/auth/login",
                json={"username": "bob", "password": "pw-bob-1"})
    resp = client.get("/api/v1/prompts")
    assert resp.status_code == 200
    assert [r["mode"] for r in resp.json()] == ["closed", "open"]
