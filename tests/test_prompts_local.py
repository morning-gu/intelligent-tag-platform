"""capabilities/prompts 本地化契约测试。

覆盖：未知 mode 抛 ValueError；模板常量占位符白名单与语法合法性；
GET /api/v1/prompts 只读契约视图的结构、过滤与非管理员可读权限。
"""

import string

import pytest

from tagplatform.capabilities.prompts import (
    ALLOWED_FIELDS,
    REQUIRED_PLACEHOLDERS,
    get_prompt,
)


def test_get_prompt_unknown_mode_raises_value_error():
    """未知 mode 显式失败（ValueError），不静默回退。"""
    with pytest.raises(ValueError):
        get_prompt("nope")


# ---------- 模板常量契约（原 _validate_template_text 语义转测试断言） ----------


@pytest.mark.parametrize("mode", ["closed"])
def test_template_constant_required_placeholders(mode):
    """模板含该 mode 全部必需占位符（closed 为 {content}）。"""
    text = get_prompt(mode)
    for placeholder in REQUIRED_PLACEHOLDERS[mode]:
        assert placeholder in text


@pytest.mark.parametrize("mode", ["closed"])
def test_template_constant_no_illegal_fields(mode):
    """占位符白名单：format 解析出的字段只允许 content（字面花括号须双写
    转义——parse 对转义段返回 None，不受白名单约束）；format 语法合法
    （花括号配对，parse 失败即红）且 strip 后非空。"""
    text = get_prompt(mode)
    assert text.strip() == text and text  # 落库即 strip 形态的语义延续
    fields = [field for _, field, _, _ in string.Formatter().parse(text)]
    for field in fields:
        assert field is None or field in ALLOWED_FIELDS, field


# ---------- GET /api/v1/prompts 只读契约视图 ----------


def test_contract_endpoint_readonly_structure(client):
    """GET 契约：逐项 {mode, template_text, allowed_fields,
    required_placeholders}，与白名单导出一致，正文与本地常量逐字一致，
    无版本字段（git 即版本）。"""
    resp = client.get("/api/v1/prompts")
    assert resp.status_code == 200
    rows = resp.json()
    assert [r["mode"] for r in rows] == ["closed"]
    for row in rows:
        assert set(row.keys()) == {"mode", "template_text",
                                   "allowed_fields", "required_placeholders"}
        assert row["template_text"] == get_prompt(row["mode"])
        assert row["allowed_fields"] == ["content"]
        assert row["required_placeholders"] == ["{content}"]


def test_contract_endpoint_mode_filter(client):
    """mode 精确过滤沿用既有语义：命中返回单项，未知 mode 返回空列表。"""
    closed = client.get("/api/v1/prompts", params={"mode": "closed"})
    assert closed.status_code == 200
    assert [r["mode"] for r in closed.json()] == ["closed"]
    empty = client.get("/api/v1/prompts", params={"mode": "hybrid"})
    assert empty.status_code == 200 and empty.json() == []


def test_non_admin_can_read_prompts_contract(client):
    """reviewer 访问只读契约视图 → 200（登录即可读）。"""
    assert client.post("/api/v1/users", json={
        "username": "bob", "password": "pw-bob-1", "role": "reviewer"}
    ).status_code == 200
    client.post("/api/v1/auth/logout")
    client.post("/api/v1/auth/login",
                json={"username": "bob", "password": "pw-bob-1"})
    resp = client.get("/api/v1/prompts")
    assert resp.status_code == 200
    assert [r["mode"] for r in resp.json()] == ["closed"]
