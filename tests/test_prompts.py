"""Prompt 模板契约与只读注册表测试。

覆盖：
- capabilities/prompts 本地模板常量：未知 mode 抛 ValueError，必需占位符齐备，
  format 占位符严格遵守白名单；
- prompt registry 完备性守门：registry 各组条目与对应常量模块公开 str 常量一致，
  closed 组组装说明（含置信度三环节携带资产标注）按特征串对齐 services/generation
  实际组装行为的守门；
- GET /api/v1/prompts 与 GET /api/v1/prompts/registry 端点契约、mode 过滤与权限。
"""

import string

import pytest

from tagplatform.capabilities.prompts import (
    ALLOWED_FIELDS,
    REQUIRED_PLACEHOLDERS,
    assets as assets_prompts,
    get_prompt,
    mtg as mtg_prompts,
    open_review as open_review_prompts,
    registry as registry_mod,
    templates as templates_prompts,
)


def _public_str_constants(module) -> set[str]:
    """模块级公开 str 常量名集合（dunder/下划线开头排除）。"""
    return {
        name
        for name, value in vars(module).items()
        if not name.startswith("_") and isinstance(value, str)
    }


def test_get_prompt_unknown_mode_raises_value_error():
    """未知 mode 显式失败（ValueError），不静默回退。"""
    with pytest.raises(ValueError):
        get_prompt("nope")


@pytest.mark.parametrize("mode", ["closed"])
def test_template_constant_placeholders_and_syntax(mode):
    """模板含必需占位符，且 format 解析出的字段严格限于白名单。"""
    text = get_prompt(mode)
    assert text.strip() == text and text
    for placeholder in REQUIRED_PLACEHOLDERS[mode]:
        assert placeholder in text
    fields = [field for _, field, _, _ in string.Formatter().parse(text)]
    for field in fields:
        assert field is None or field in ALLOWED_FIELDS, field


def test_registry_covers_all_central_constants():
    """完备性守门：registry 各组条目名集合必须与对应常量模块公开 str 常量一致。"""
    expected = {
        "closed": _public_str_constants(templates_prompts),
        "mtg": _public_str_constants(mtg_prompts),
        "assets": _public_str_constants(assets_prompts),
        "open_review": _public_str_constants(open_review_prompts),
    }
    actual = {
        g["module"]: {i["name"] for i in g["items"]}
        for g in registry_mod.list_prompt_registry()
    }
    assert set(actual) == set(expected)
    for module in expected:
        assert actual[module] == expected[module], (
            f"registry '{module}' 组与常量模块不同步："
            f"缺 {sorted(expected[module] - actual[module])} /"
            f" 多 {sorted(actual[module] - expected[module])}"
        )


def test_registry_endpoint_contract(client):
    resp = client.get("/api/v1/prompts/registry")
    assert resp.status_code == 200
    groups = resp.json()
    assert {g["module"] for g in groups} == {"closed", "mtg", "assets", "open_review"}
    for group in groups:
        assert group["title"]
        assert group["items"]
        for item in group["items"]:
            assert item["name"]
            assert item["text"].strip()


def test_prompts_endpoint_contract_filter_and_non_admin_read(client):
    """GET /api/v1/prompts：只读结构、mode 精确过滤，reviewer 登录可读。"""
    resp = client.get("/api/v1/prompts")
    assert resp.status_code == 200
    rows = resp.json()
    assert [r["mode"] for r in rows] == ["closed"]
    assert set(rows[0].keys()) == {
        "mode", "template_text", "allowed_fields", "required_placeholders",
    }
    assert rows[0]["template_text"] == get_prompt("closed")

    assert [r["mode"] for r in client.get("/api/v1/prompts", params={"mode": "closed"}).json()] == ["closed"]
    assert client.get("/api/v1/prompts", params={"mode": "hybrid"}).json() == []

    assert client.post("/api/v1/users", json={
        "username": "bob", "password": "pw-bob-1", "role": "reviewer",
    }).status_code == 200
    client.post("/api/v1/auth/logout")
    client.post("/api/v1/auth/login", json={"username": "bob", "password": "pw-bob-1"})
    reviewer_resp = client.get("/api/v1/prompts")
    assert reviewer_resp.status_code == 200
    assert [r["mode"] for r in reviewer_resp.json()] == ["closed"]


def test_registry_closed_assembly_notes(client):
    """组装说明常量（置信度配套展示）：closed 组 5 条说明逐一按特征串
    钉死——说明文案与 services/generation 的实际组装行为对齐的守门。"""
    groups = {g["module"]: g for g in client.get("/api/v1/prompts/registry").json()}
    items = {i["name"]: i["text"] for i in groups["closed"]["items"]}
    assert "原始资产插入位置" in items["CLOSED_ASSEMBLY_NOTE"]
    assert "{content}" in items["CLOSED_ASSEMBLY_NOTE"]              # 插入位置标记
    assert "关键帧" in items["CLOSED_ASSEMBLY_NOTE"]
    # 置信度三环节携带资产总标注：Ccons/Cver 带资产 + 帧、Cmod 纯本地
    assert "携带资产" in items["CLOSED_CONFIDENCE_ASSETS_NOTE"]
    assert "{content}" in items["CLOSED_CONFIDENCE_ASSETS_NOTE"]     # 插入位置标记
    assert "关键帧" in items["CLOSED_CONFIDENCE_ASSETS_NOTE"]
    assert "spec.prompt_text" in items["CLOSED_CONFIDENCE_ASSETS_NOTE"]
    assert "_render_content_text" in items["CLOSED_CONFIDENCE_ASSETS_NOTE"]
    assert "不调用 LLM" in items["CLOSED_CONFIDENCE_ASSETS_NOTE"]
    assert "spec.prompt_text" in items["CLOSED_CONFIDENCE_CCONS_NOTE"]
    assert "重复调用 2 次" in items["CLOSED_CONFIDENCE_CCONS_NOTE"]
    assert "CLOSED_VERIFICATION_PROMPT" in items["CLOSED_CONFIDENCE_CVER_NOTE"]
    assert "_render_content_text" in items["CLOSED_CONFIDENCE_CVER_NOTE"]
    assert "不复用生成契约正文" in items["CLOSED_CONFIDENCE_CVER_NOTE"]
    assert "标签描述与别名" in items["CLOSED_CONFIDENCE_CVER_NOTE"]
    assert "关键帧" in items["CLOSED_CONFIDENCE_CVER_NOTE"]
    assert "不调用 LLM" in items["CLOSED_CONFIDENCE_CMOD_NOTE"]
    assert "0.5 占位" in items["CLOSED_CONFIDENCE_CMOD_NOTE"]
